import streamlit as st
import pandas as pd
import requests
import time
import math
import io
import re
import os
import pickle
from unidecode import unidecode
from rapidfuzz import process, fuzz
from diskcache import Cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from threading import Lock

# Configuração Canônica de UI/UX do Streamlit
st.set_page_config(
    page_title="Gerenciador de Rotas Inteligentes", 
    page_icon="🚗", 
    layout="centered"
)

# ==============================================================================
# 🧠 PERSISTÊNCIA EM DISCO E HIGIENIZAÇÃO DE AMBIENTE (GARBAGE COLLECTION)
# ==============================================================================
cache_classificacao = Cache("./cache_classificacao")
cache_fuzzy = Cache("./cache_fuzzy")
cache_geo = Cache("./cache_geo")
cache_rotas = Cache("./cache_rotas")
cache_poi = Cache("./cache_poi")
cache_cep = Cache("./cache_cep")
cache_google = Cache("./cache_google")
cache_reverse = Cache("./cache_reverse")

# Purga automática de chaves expiradas na inicialização para economizar disco
for c in [cache_classificacao, cache_fuzzy, cache_geo, cache_rotas, cache_poi, cache_cep, cache_google, cache_reverse]:
    c.cull()

def realizar_manutencao_logs_google():
    diretorio_logs = "logs_google"
    os.makedirs(diretorio_logs, exist_ok=True)
    limite_tempo = time.time() - (30 * 86400)
    try:
        for arquivo in os.listdir(diretorio_logs):
            caminho_completo = os.path.join(diretorio_logs, arquivo)
            if os.path.isfile(caminho_completo) and os.path.getmtime(caminho_completo) < limite_tempo:
                os.remove(caminho_completo)
    except Exception: pass

realizar_manutencao_logs_google()

# Conexão HTTP Persistente com Backoff Exponencial
session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)

lock_nominatim = Lock()
CACHE_IBGE_PATH = "municipios_ibge.pkl"

# Alocação fixa e controlada do Pool de Threads Global do Aplicativo
if "executor_global" not in st.session_state:
    st.session_state["executor_global"] = ThreadPoolExecutor(max_workers=5)

# ==============================================================================
# 🎛️ DADOS GLOBAIS THREAD-SAFE (EXPANSÃO MULTIVARIÁVEL IBGE)
# ==============================================================================
@st.cache_data
def carregar_dados_ibge():
    """Carrega Estados, Municípios e Distritos de forma imutável e visível para worker threads"""
    if os.path.exists(CACHE_IBGE_PATH):
        if time.time() - os.path.getmtime(CACHE_IBGE_PATH) > (30 * 86400):
            os.remove(CACHE_IBGE_PATH)
        else:
            try:
                with open(CACHE_IBGE_PATH, "rb") as f:
                    d = pickle.load(f)
                    return d.get("municipios", {}), d.get("estados", {}), d.get("distritos", {}), list(d.get("municipios", {}).keys())
            except Exception: pass

    base_mun, base_est, base_dist = {}, {}, {}
    try:
        r_est = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/estados", timeout=8)
        if r_est.status_code == 200:
            for est in r_est.json():
                base_est[est["sigla"]] = unidecode(est["nome"]).upper()
                
        r_mun = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/municipios", timeout=12)
        if r_mun.status_code == 200:
            for mun in r_mun.json():
                nome_norm = unidecode(mun["nome"]).upper().strip()
                base_mun[nome_norm] = {"uf": mun["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper(), "tipo": "MUNICIPIO"}
                
        r_dist = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/distritos", timeout=12)
        if r_dist.status_code == 200:
            for dist in r_dist.json():
                nome_dist = unidecode(dist["nome"]).upper().strip()
                uf_dist = dist["municipio"]["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper()
                if nome_dist not in base_mun:
                    base_dist[nome_dist] = {"uf": uf_dist, "tipo": "DISTRITO"}

            with open(CACHE_IBGE_PATH, "wb") as f:
                pickle.dump({"municipios": base_mun, "estados": base_est, "distritos": base_dist}, f)
    except Exception: pass
    
    return base_mun, base_est, base_dist, list(base_mun.keys())

IBGE_MUNICIPIOS, IBGE_ESTADOS, IBGE_DISTRITOS, LISTA_MUNICIPIOS = carregar_dados_ibge()

SINONIMOS_SEMANTICOS = {
    "UNB": "UNIVERSIDADE DE BRASILIA", "CATOLICA": "UNIVERSIDADE CATOLICA",
    "JK": "JUSCELINO KUBITSCHEK", "HBDF": "HOSPITAL DE BASE DO DISTRITO FEDERAL",
    "HRAN": "HOSPITAL REGIONAL DA ASA NORTE", "RODOVIARIA": "TERMINAL RODOVIARIO"
}

POI_KEYWORDS = [
    "AEROPORTO", "HOSPITAL", "UNIVERSIDADE", "FACULDADE", "ESCOLA", "SHOPPING", 
    "HOTEL", "RODOVIARIA", "ESTADIO", "MINISTERIO", "AGENCIA", "BANCO", 
    "IGREJA", "FORUM", "TRIBUNAL", "DELEGACIA", "PREFEITURA"
]

# ==============================================================================
# 🧹 ENGINE DE RESOLUÇÃO UNIVERSAL E CLASSIFICAÇÃO (CAMADAS 1, 2, 11)
# ==============================================================================
class ClassificadorSemantico:
    def __init__(self):
        self.rural_keywords = ["FAZENDA", "SITIO", "ASSENTAMENTO", "CHACARA", "GLEBA", "NUCLEO RURAL"]
        self.bairro_keywords = ["BAIRRO", "VILA", "JARDIM", "PARQUE", "RESIDENCIAL", "SETOR", "ASA SUL", "ASA NORTE", "LAGO SUL", "LAGO NORTE"]
        self.via_keywords = ["RUA", "AVENIDA", "TRAVESSA", "ALAMEDA", "RODOVIA", "ESTRADA", "QUADRA", "SQN", "SQS", "SHIS", "QSC", "QS"]

    def normalizar(self, texto):
        if not texto or pd.isna(texto): return ""
        t = str(texto).strip()
        t = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', t)
        t = unidecode(t).upper()
        
        abreviacoes = {
            r'\bAV\b': 'AVENIDA', r'\bR\b': 'RUA', r'\bQD\b': 'QUADRA', r'\bLT\b': 'LOTE',
            r'\bCJ\b': 'CONJUNTO', r'\bCONJ\b': 'CONJUNTO', r'\bBL\b': 'BLOCO', r'\bAPT\b': 'APARTAMENTO',
            r'\bST\b': 'SETOR', r'\bCH\b': 'CHACARA', r'\bROD\b': 'RODOVIA', r'\bKM\b': 'QUILOMETRO', 
            r'\bBR\b': 'BR', r'\bAL\b': 'ALAMEDA', r'\bTR\b': 'TRAVESSA', r'\bTV\b': 'TRAVESSA', 
            r'\bPCA\b': 'PRACA', r'\bPQ\b': 'PARQUE', r'\bSQN\b': 'SUPERQUADRA NORTE', 
            r'\bSQS\b': 'SUPERQUADRA SUL', r'\bCLN\b': 'COMERCIO LOCAL NORTE', r'\bCLS\b': 'COMERCIO LOCAL SUL'
        }
        for padrao, expansao in abreviacoes.items(): t = re.sub(padrao, expansao, t)
        for chave, valor in SINONIMOS_SEMANTICOS.items(): t = re.sub(r'\b' + chave + r'\b', valor, t)
        return re.sub(r'\s+', ' ', t).strip()

    def classificar_entrada(self, texto_norm):
        """Camada 2: Atribuição Taxonômica de Tipologia do Input"""
        if texto_norm in cache_classificacao: return cache_classificacao[texto_norm]
        
        tipo = "LOGRADOURO"
        if re.search(r'\b\d{5}-?\d{3}\b', texto_norm): tipo = "CEP"
        elif any(k in texto_norm for k in POI_KEYWORDS): tipo = "POI"
        elif any(k in texto_norm for k in self.rural_keywords): tipo = "RURAL"
        elif any(k in texto_norm for k in self.via_keywords) and bool(re.search(r'\d+', texto_norm)): tipo = "ENDERECO_COMPLETO"
        elif any(k in texto_norm for k in self.bairro_keywords): tipo = "BAIRRO"
        elif texto_norm in IBGE_MUNICIPIOS: tipo = "MUNICIPIO"
        elif texto_norm in IBGE_DISTRITOS: tipo = "DISTRITO"
        
        cache_classificacao.set(texto_norm, tipo, expire=2592000)
        return tipo

    def aplicar_fuzzy_multidimensional(self, texto_norm):
        """Camada 11: Machine Learning Assistido Ponderado via RapidFuzz"""
        if texto_norm in cache_fuzzy: return cache_fuzzy[texto_norm]
        tokens = texto_norm.split()
        for token in tokens:
            if len(token) >= 5 and token not in IBGE_MUNICIPIOS and token not in IBGE_DISTRITOS:
                match_w = process.extractOne(token, LISTA_MUNICIPIOS, scorer=fuzz.WRatio)
                match_set = process.extractOne(token, LISTA_MUNICIPIOS, scorer=fuzz.token_set_ratio)
                if match_w and match_set and match_w[0] == match_set[0] and match_w[1] >= 95:
                    texto_norm = texto_norm.replace(token, match_w[0])
                    break
        cache_fuzzy.set(texto_norm, texto_norm, expire=2592000)
        return texto_norm

    def inferir_ancora_geografica(self, texto_norm):
        palavras = texto_norm.split()
        for i in range(len(palavras)):
            for j in range(i + 1, len(palavras) + 1):
                chunk = " ".join(palavras[i:j])
                if chunk in IBGE_MUNICIPIOS: return IBGE_MUNICIPIOS[chunk]["uf"], chunk
                if chunk in IBGE_DISTRITOS: return IBGE_DISTRITOS[chunk]["uf"], chunk
        return None, None

semantica = ClassificadorSemantico()

# ==============================================================================
# 🧮 LÓGICA GEODÉSICA E CONTINGÊNCIA POSTAL (CAMADA 3)
# ==============================================================================
def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    if not (-90 <= lat1 <= 90) or not (-90 <= lat2 <= 90) or not (-180 <= lon1 <= 180) or not (-180 <= lon2 <= 180): return 0.0
    if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0: return 0.0
    if lat1 == lat2 and lon1 == lon2: return 0.0
    try:
        a, b, f = 6378137.0, 6356752.314245, 1 / 298.257223563
        L = math.radians(lon2 - lon1)
        U1 = math.atan((1 - f) * math.tan(math.radians(lat1)))
        U2 = math.atan((1 - f) * math.tan(math.radians(lat2)))
        sinU1, cosU1 = math.sin(U1), math.cos(U1)
        sinU2, cosU2 = math.sin(U2), math.cos(U2)
        lambda_lon = L
        for _ in range(100):
            sinLambda, cosLambda = math.sin(lambda_lon), math.cos(lambda_lon)
            sinSigma = math.sqrt((cosU2 * sinLambda) ** 2 + (cosU1 * sinU2 - sinU1 * cosU2 * cosLambda) ** 2)
            if sinSigma == 0: return 0.0
            cosSigma = sinU1 * sinU2 + cosU1 * cosU2 * cosLambda
            sigma = math.atan2(sinSigma, cosSigma)
            sinAlpha = cosU1 * cosU2 * sinLambda / sinSigma
            cosSqAlpha = 1 - sinAlpha ** 2
            cos2SigmaM = cosSigma - 2 * sinU1 * sinU2 / cosSqAlpha if cosSqAlpha != 0 else 0
            C = f / 16 * cosSqAlpha * (4 + f * (4 - 3 * cosSqAlpha))
            lambdaPrev = lambda_lon
            lambda_lon = L + (1 - f) * C * sinAlpha * (sigma + f * sinAlpha * (cos2SigmaM + C * cosSigma * (-1 + 2 * cos2SigmaM ** 2)))
            if abs(lambda_lon - lambdaPrev) < 1e-12: break
        uSq = cosSqAlpha * (a ** 2 - b ** 2) / (b ** 2)
        A = 1 + uSq / 16384 * (4096 + uSq * (-768 + uSq * (320 - 175 * uSq)))
        B = uSq / 1024 * (256 + uSq * (-128 + uSq * (74 - 47 * uSq)))
        deltaSigma = B * sinSigma * (cos2SigmaM + B / 4 * (cosSigma * (-1 + 2 * cos2SigmaM ** 2) - B / 6 * cos2SigmaM * (-3 + 4 * sinSigma ** 2) * (-3 + 4 * cos2SigmaM ** 2)))
        s = b * A * (sigma - deltaSigma)
        return round(s / 1000, 2)
    except Exception:
        dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        m_a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
        return round(6371.0 * 2 * math.atan2(math.sqrt(m_a), math.sqrt(1 - m_a)), 2)

def cascata_postal_tripla(cep_limpo):
    if cep_limpo in cache_cep: return cache_cep[cep_limpo]
    try:
        r = session.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=4).json()
        if "erro" not in r:
            d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''))
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception: pass
    try:
        r = session.get(f"https://brasilapi.com.br/api/cep/v1/{cep_limpo}", timeout=4).json()
        if "name" not in r:
            d = (r.get('street', ''), r.get('neighborhood', ''), r.get('city', ''), r.get('state', ''))
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception: pass
    try:
        r = session.get(f"https://opencep.com/v1/{cep_limpo}", timeout=4).json()
        if "error" not in r:
            d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''))
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception: pass
    return "", "", "", ""

# ==============================================================================
# 🗺️ GEOCODIFICAÇÃO MULTIMOTOR E REVERSE (CAMADAS 6, 7, 8, 10)
# ==============================================================================
def executar_reverse_geocoding_multimotor(lat, lon):
    rev_key = f"{round(lat,5)}|{round(lon,5)}"
    if rev_key in cache_reverse: return cache_reverse[rev_key]
    res = {"logradouro": "", "bairro": "", "cidade": "", "municipio": "", "distrito": "", "estado": "", "cep": ""}
    try:
        url_nom = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&addressdetails=1"
        r_nom = session.get(url_nom, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4)
        if r_nom.status_code == 200:
            a = r_nom.json().get("address", {})
            res.update({"logradouro": a.get("road", a.get("pedestrian", "")), "bairro": a.get("neighbourhood", a.get("suburb", a.get("city_district", ""))), "cidade": a.get("city", a.get("town", a.get("municipality", ""))), "estado": a.get("state", "").upper(), "cep": a.get("postcode", "")})
            cache_reverse.set(rev_key, res, expire=2592000); return res
    except Exception: pass
    try:
        url_arc = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/reverseGeocode?location={lon},{lat}&f=json"
        r_arc = session.get(url_arc, timeout=4).json()
        if 'address' in r_arc:
            addr = r_arc['address']
            res.update({"logradouro": addr.get('Address', ''), "bairro": addr.get('Neighborhood', ''), "cidade": addr.get('City', ''), "estado": addr.get('RegionAbbr', '').upper(), "cep": addr.get('Postal', '')})
            cache_reverse.set(rev_key, res, expire=2592000)
    except Exception: pass
    return res

def API_ArcGIS(query):
    try:
        url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=1&sourceCountry=BRA&outFields=*"
        r = session.get(url, timeout=4).json()
        if r.get('candidates'):
            c = r['candidates'][0]
            attr = c.get('attributes', {})
            return {"lat": float(c['location']['y']), "lon": float(c['location']['x']), "fonte": "ARCGIS", "score_base": 30, "cidade": attr.get('City', '').upper(), "estado": attr.get('RegionAbbr', '').upper(), "bairro": attr.get('Neighborhood', '').upper()}
    except Exception: pass
    return None

def API_Nominatim(query):
    with lock_nominatim:
        time.sleep(1.1)
        try:
            url = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query)}&limit=1&addressdetails=1&countrycodes=br"
            r = session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
            if r:
                a = r[0]
                addr = a.get("address", {})
                return {"lat": float(a['lat']), "lon": float(a['lon']), "fonte": "NOMINATIM", "score_base": 25, "cidade": addr.get('city', addr.get('town', '')).upper(), "estado": addr.get('state', '').upper(), "bairro": addr.get('neighbourhood', addr.get('suburb', '')).upper()}
        except Exception: pass
        return None

def API_Photon(query):
    try:
        url = f"https://photon.komoot.io/api/?q={requests.utils.quote(query)}&limit=1&filter=countrycode:br"
        r = session.get(url, timeout=4).json()
        if r.get("features"):
            f = r["features"][0]
            lon, lat = f["geometry"]["coordinates"]
            props = f.get("properties", {})
            return {"lat": lat, "lon": lon, "fonte": "PHOTON", "score_base": 20, "cidade": props.get("city", "").upper(), "estado": props.get("state", "").upper(), "bairro": props.get("district", "").upper()}
    except Exception: pass
    return None

def API_Pelias(query):
    try:
        url = f"https://api.geocode.earth/v1/search?text={requests.utils.quote(query)}&boundary.country=BRA&size=1"
        r = session.get(url, timeout=4).json()
        if r.get("features"):
            f = r["features"][0]
            lon, lat = f["geometry"]["coordinates"]
            props = f.get("properties", {})
            return {"lat": lat, "lon": lon, "fonte": "PELIAS", "score_base": 15, "cidade": props.get("locality", "").upper(), "estado": props.get("region_a", "").upper(), "bairro": props.get("neighbourhood", "").upper()}
    except Exception: pass
    return None

def API_Overpass_POIs(texto_norm):
    if len(texto_norm) < 10: return None
    endpoints = ["https://overpass-api.de/api/interpreter", "https://lz4.overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
    texto_seguro = re.escape(texto_norm)
    query_osm = f'[out:json][timeout:3];(node["name"~"{texto_seguro}",i]["amenity"];way["name"~"{texto_seguro}",i]["amenity"];node["name"~"{texto_seguro}",i]["building"];way["name"~"{texto_seguro}",i]["building"];node["name"~"{texto_seguro}",i]["healthcare"];way["name"~"{texto_seguro}",i]["healthcare"];node["name"~"{texto_seguro}",i]["education"];way["name"~"{texto_seguro}",i]["education"];);out center;'
    for url in endpoints:
        try:
            r = session.post(url, data={"data": query_osm}, timeout=4)
            if r.status_code == 200:
                elems = r.json().get("elements", [])
                if elems:
                    e = elems[0]
                    lat, lon = e.get("lat", e.get("center", {}).get("lat", 0.0)), e.get("lon", e.get("center", {}).get("lon", 0.0))
                    tags = e.get("tags", {})
                    return {"lat": lat, "lon": lon, "fonte": "OVERPASS", "score_base": 40, "cidade": tags.get("addr:city", "").upper(), "estado": tags.get("addr:state", "").upper(), "bairro": tags.get("addr:suburb", "").upper()}
        except Exception: continue
    return None

def processar_consenso_dinamico(candidatos, tipo_entrada, texto_cru):
    """Camada 9: Votação e Consenso Espacial Multivariável Baseado em Tipologia de Input"""
    if not candidatos: return None
    tolerancia_km = 0.5 if tipo_entrada in ["ENDERECO_COMPLETO", "POI", "CEP"] else 2.0 if tipo_entrada in ["BAIRRO", "RURAL"] else 10.0
    
    for c1 in candidatos:
        score_centesimal = c1["score_base"]
        consenso_espacial = 0
        for c2 in candidatos:
            if c1["fonte"] != c2["fonte"]:
                dist = calcular_distancia_vincenty(c1["lat"], c1["lon"], c2["lat"], c2["lon"])
                if dist <= tolerancia_km: consenso_espacial += 1
                if c1["cidade"] and c1["cidade"] == c2["cidade"]: score_centesimal += 20
                if c1["estado"] and c1["estado"] == c2["estado"]: score_centesimal += 15
                if c1["bairro"] and c1["bairro"] == c2["bairro"]: score_centesimal += 10
        c1["score_final"] = score_centesimal + (consenso_espacial * 25)
        
    candidatos.sort(key=lambda x: x["score_final"], reverse=True)
    vencedor = candidatos[0]
    score_limitado = min(int(vencedor["score_final"]), 100)
    
    if score_limitado < 70:
        m = executar_reverse_geocoding_multimotor(vencedor["lat"], vencedor["lon"])
    else:
        m = {"logradouro": texto_cru.upper(), "bairro": vencedor["bairro"], "cidade": vencedor["cidade"], "municipio": vencedor["cidade"], "distrito": "", "estado": vencedor["estado"], "cep": ""}
        
    if m["cep"]: score_limitado = min(score_limitado + 10, 100)
    confianca = "ALTISSIMA" if score_limitado >= 85 else "ALTA" if score_limitado >= 75 else "MEDIA" if score_limitado >= 60 else "BAIXA"
    
    rua_f = m["logradouro"] if m["logradouro"] else texto_cru.upper()
    endereco_f = ", ".join([c for c in [rua_f, m["bairro"], m["cidade"], m["estado"]] if c.strip()]) + ", BRASIL"
    return vencedor["lat"], vencedor["lon"], endereco_f, confianca, score_limitado, m["distrito"], m["municipio"], vencedor["fonte"]

# ==============================================================================
# 🎛️ PIPELINE HIERÁRQUICO DE RESOLUÇÃO UNIVERSAL (CAMADA 2, 4, 5, 9, 13)
# ==============================================================================
def obter_coordenadas_e_endereco_oficial(localidade):
    texto_cru = str(localidade).strip()
    if not texto_cru or texto_cru.lower() == 'nan': return 0.0, 0.0, "", "BAIXA", 0, "", "", "N/A"
    
    texto_norm = semantica.normalizar(texto_cru)
    texto_fuzzy = semantica.aplicar_fuzzy_multidimensional(texto_norm)
    tipo_entrada = semantica.classificar_entrada(texto_fuzzy)
    
    cache_key = f"{tipo_entrada}_{texto_fuzzy}"
    if cache_key in cache_geo:
        c = cache_geo[cache_key]
        return c["lat"], c["lon"], c["endereco"], c["confianca"], c["score_num"], c["distrito"], c["municipio"], c["fonte"]

    candidatos_validos = []

    # Nível Hierárquico 1: CEP Short-Circuit
    if tipo_entrada == "CEP":
        cep_estrito = re.search(r'\b\d{5}-?\d{3}\b', texto_fuzzy).group(0).replace("-", "")
        logr, bair, loca, uf = cascata_postal_tripla(cep_estrito)
        if loca:
            addr_c = f"{logr}, {bair}, {loca}, {uf}, CEP {cep_estrito}, BRASIL"
            res_arc = API_ArcGIS(addr_c)
            if res_arc:
                res_final = (res_arc["lat"], res_arc["lon"], addr_c, "ALTISSIMA", 100, bair, loca, "ViaCEP/ArcGIS")
                cache_geo.set(cache_key, {"lat": res_arc["lat"], "lon": res_arc["lon"], "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca, "fonte": "ViaCEP/ArcGIS"}, expire=2592000)
                return res_final

    # Ancoragem Cadastral e Expansão Territorial
    uf_inferida, muni_inferido = semantica.inferir_ancora_geografica(texto_fuzzy)
    if uf_inferida and "BRASIL" not in texto_fuzzy:
        texto_expandido = f"{texto_fuzzy}, {muni_inferido} - {uf_inferida}, BRASIL" if muni_inferido else f"{texto_fuzzy}, {uf_inferida}, BRASIL"
    else:
        texto_expandido = f"{texto_fuzzy}, BRASIL" if "BRASIL" not in texto_fuzzy else texto_fuzzy

    # Nível Hierárquico 2: POI Short-Circuit via Overpass
    if tipo_entrada == "POI":
        res_poi = API_Overpass_POIs(texto_fuzzy)
        if res_poi: candidatos_validos.append(res_poi)

    # Nível Hierárquico 3: Concorrência de Geocodificadores Multi-Fonte Isolados
    with ThreadPoolExecutor(max_workers=4) as pool_api:
        fs = [
            pool_api.submit(API_ArcGIS, texto_expandido),
            pool_api.submit(API_Nominatim, texto_expandido),
            pool_api.submit(API_Photon, texto_expandido),
            pool_api.submit(API_Pelias, texto_expandido)
        ]
        for futuro in as_completed(fs):
            try:
                res = futuro.result(timeout=5)
                if res: candidatos_validos.append(res)
            except Exception: pass
            
    res_final = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru)
    if res_final:
        cache_geo.set(cache_key, {"lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], "confianca": res_final[3], "score_num": res_final[4], "distrito": res_final[5], "municipio": res_final[6], "fonte": res_final[7]}, expire=2592000)
        return res_final
        
    return 0.0, 0.0, texto_expandido, "BAIXA", 0, "", "" , "N/A"

# ==============================================================================
# 🚀 MOTOR DE ROTEAMENTO CORPORATIVO (GOOGLE PREVIEW PRIORITÁRIO)
# ==============================================================================
def extrair_dados_reais_google(origem_raw, destino_raw, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=True):
    cache_key = f"{normalizar_endereco_universal(origem_raw)}|{normalizar_endereco_universal(destino_raw)}"
    if cache_key in cache_google: return cache_google[cache_key]

    origem_param = f"{lat_o},{lon_o}" if usar_coordenadas else requests.utils.quote(origem_raw)
    destino_param = f"{lat_d},{lon_d}" if usar_coordenadas else requests.utils.quote(destino_raw)
    url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(origem_raw)}&destination={requests.utils.quote(destino_raw)}&travelmode=driving"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)", "Referer": "https://www.google.com/maps", "Accept": "*/*"}
    
    try:
        resposta = session.get(url_api, headers=headers, timeout=8)
        texto_resposta = resposta.text
        if len(texto_resposta) < 500 or "directions" not in texto_resposta.lower(): return None
        with open(f"logs_google/{hash(cache_key)}.txt", "w", encoding="utf-8") as f: f.write(texto_resposta)
            
        match_km = re.findall(r'\"(\d+[\.,]?\d*)\s*km\"', texto_resposta)
        match_tempo = re.findall(r'\"(\d+\s*h\s*\d+\s*min|\d+\s*h|\d+\s*min)\"', texto_resposta)
        if match_km and match_tempo:
            km_puro = float(match_km[0].replace('.', '').replace(',', '.'))
            if dist_linha_reta > 0 and (km_puro < dist_linha_reta * 0.8 or km_puro > dist_linha_reta * 4.0): return None
            envolve_balsa = "Sim" if any(re.search(p, texto_resposta.lower()) for p in [r'\"utilizar\s+balsa\b', r'\"ferry\b']) else "Não"
            score_google = 70 + (10 if km_puro > 0 else 0) + (10 if match_tempo[0] else 0) + (10 if km_puro >= dist_linha_reta else 0)
            res = (km_puro, match_tempo[0], link_maps, envolve_balsa, score_google)
            cache_google.set(cache_key, res, expire=2592000); return res
    except Exception: pass
    return None

def rota_osrm(lat_o, lon_o, lat_d, lon_d):
    try:
        url = f"https://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=false"
        r = session.get(url, timeout=5).json()
        if r.get("routes"):
            km = round(r["routes"][0]["distance"] / 1000, 2)
            minutos = round(r["routes"][0]["duration"] / 60)
            return km, f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min", "OSRM", 95
    except Exception: pass
    return None

def rota_openrouteservice_fallback(lat_o, lon_o, lat_d, lon_d):
    try:
        url = f"https://api.openrouteservice.org/v2/directions/driving-car?start={lon_o},{lat_o}&end={lon_d},{lat_d}"
        r = session.get(url, timeout=5).json()
        if r.get("features"):
            props = r["features"][0]["properties"]["summary"]
            km = round(props["distance"] / 1000, 2)
            minutos = round(props["duration"] / 60)
            return km, f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min", "OpenRouteService", 90
    except Exception: pass
    return None

def obter_fator_desvio_rodoviario(linha_reta):
    return 1.45 if linha_reta < 5.0 else 1.35 if linha_reta < 20.0 else 1.25 if linha_reta < 100.0 else 1.18

def calcular_pipeline_logistico(origem, destino):
    start_total = time.time()
    origem_clean, destino_clean = str(origem).strip(), str(destino).strip()
    
    chave_rota_cache = f"ROTA_{normalizar_endereco_universal(origem_clean)}->{normalizar_endereco_universal(destino_clean)}"
    if chave_rota_cache in cache_rotas: return cache_rotas[chave_rota_cache]
    
    start_geo = time.time()
    lat_o, lon_o, end_oficial_o, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    lat_d, lon_d, end_oficial_d, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d = obter_coordenadas_e_endereco_oficial(destino_clean)
    tempo_geocoding = round(time.time() - start_geo, 2)
    
    start_rot = time.time()
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)
    usar_coords = True if (lat_o != 0.0 and lat_d != 0.0) else False
    if usar_coords and dist_linha_reta > 150.0:
        siglas_originais = re.findall(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM|PA|MT|MS)\b', origem_clean.upper() + " " + destino_clean.upper())
        if len(set(siglas_originais)) <= 1: usar_coords = False

    link_fallback = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(end_oficial_o)}&destination={requests.utils.quote(end_oficial_d)}&travelmode=driving"

    # Prioridade de Roteamento 1: Google Preview
    res_google = extrair_dados_reais_google(end_oficial_o, end_oficial_d, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=usar_coords)
    if res_google:
        tempo_roteamento = round(time.time() - start_rot, 2)
        tempo_total = round(time.time() - start_total, 2)
        retorno = (res_google[0], res_google[1], res_google[2], res_google[3], dist_linha_reta, "Google Preview", res_google[4], conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total)
        cache_rotas.set(chave_rota_cache, retorno, expire=2592000); return retorno

    # Fallback 1: OSRM Engine por Coordenadas Canônicas
    if usar_coords:
        res_osrm = rota_osrm(lat_o, lon_o, lat_d, lon_d)
        if res_osrm:
            tempo_roteamento = round(time.time() - start_rot, 2)
            tempo_total = round(time.time() - start_total, 2)
            retorno = (res_osrm[0], res_osrm[1], link_fallback, "Não", dist_linha_reta, res_osrm[2], res_osrm[3], conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total)
            cache_rotas.set(chave_rota_cache, retorno, expire=2592000); return retorno
            
        # Fallback 2: OpenRouteService
        res_ors = rota_openrouteservice_fallback(lat_o, lon_o, lat_d, lon_d)
        if res_ors:
            tempo_roteamento = round(time.time() - start_rot, 2)
            tempo_total = round(time.time() - start_total, 2)
            retorno = (res_ors[0], res_ors[1], link_fallback, "Não", dist_linha_reta, res_ors[2], res_ors[3], conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total)
            cache_rotas.set(chave_rota_cache, retorno, expire=2592000); return retorno

    # Fallback 3 de Fechamento de Malha: Geodésico Adaptativo
    km_terrestre = round(dist_linha_reta * obter_fator_desvio_rodoviario(dist_linha_reta), 2)
    v_comercial = 45.0 if km_terrestre < 50.0 else 65.0
    minutos_est = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0 else 0
    tempo_geo_str = f"{minutos_est} min" if minutos_est < 60 else f"{minutos_est // 60} h {minutos_est % 60} min"
    tempo_roteamento = round(time.time() - start_rot, 2)
    tempo_total = round(time.time() - start_total, 2)
    
    retorno = (km_terrestre, tempo_geo_str, link_fallback, "Não", dist_linha_reta, "Geodésico Adaptativo", 70, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total)
    cache_rotas.set(chave_rota_cache, retorno, expire=2592000)
    return retorno

def embrulhar_task_paralela(item):
    idx, orig, dest = item
    try: return idx, calcular_pipeline_logistico(orig, dest)
    except Exception: return idx, None

# ==============================================================================
# 🚗 INTERFACE VISUAL NO STREAMLIT (MANIPULAÇÃO EM LOTE CORPORATIVA)
# ==============================================================================
st.title("🚗 Gerenciador de Rotas Inteligentes")
st.subheader("Engine de Resolução Espacial Nacional — Operação Corporativa")
st.write("Insira uma planilha Excel (.xlsx) contendo as colunas **Origem** e **Destino**.")

arquivo_carregado = st.file_uploader("Selecionar Arquivo Excel", type=["xlsx"])

if arquivo_carregado is not None:
    df = pd.read_excel(arquivo_carregado)
    if 'Origem' not in df.columns or 'Destino' not in df.columns:
        st.error("Erro de Validação: A planilha deve possuir as colunas 'Origem' e 'Destino'.")
    else:
        if len(df) > 5000:
            st.error("A planilha excede o limite de 5000 linhas. Fracione o arquivo.")
            st.stop()
            
        st.success(f"Tabela de dados detectada com sucesso! ({len(df)} registros mapeados). Pronto para processar.")
        
        if st.button("Iniciar Processamento em Lote"):
            novas_colunas = [
                'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta', 'Fonte da Rota', 'Score da Rota', 
                'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem', 'Fonte Geocoding Origem', 'Endereco Oficial Origem',
                'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino', 'Fonte Geocoding Destino', 'Endereco Oficial Destino',
                'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)', 'Score Final Global', 'Status da Rota'
            ]
            for col in novas_colunas: df[col] = None
                
            tarefas = []
            for linha in df.itertuples(index=True):
                origem, destino = str(getattr(linha, 'Origem', '')).strip(), str(getattr(linha, 'Destino', '')).strip()
                if origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan':
                    tarefas.append((linha.Index, origem, destino))
            
            # Executa o loop assíncrono consumindo o pool global de forma segura
            resultados_mapeados = {}
            executor_lote = st.session_state["executor_global"]
            futuros = {executor_lote.submit(embrulhar_task_paralela, t): t for t in tarefas}
            
            concluidos = 0
            barra_progresso = st.progress(0)
            container_status = st.empty()
            
            for f in as_completed(futuros):
                idx, res = f.result()
                if res:
                    df.at[idx, 'Distancia'] = res[0]; df.at[idx, 'Tempo'] = res[1]
                    df.at[idx, 'Link da Rota'] = res[2]; df.at[idx, 'Balsas'] = res[3]
                    df.at[idx, 'Linha Reta'] = res[4]; df.at[idx, 'Fonte da Rota'] = res[5]
                    df.at[idx, 'Score da Rota'] = res[6]; df.at[idx, 'Confianca Origem'] = res[7]
                    df.at[idx, 'Score Num Origem'] = res[8]; df.at[idx, 'Distrito Origem'] = res[9]
                    df.at[idx, 'Municipio Origem'] = res[10]; df.at[idx, 'Fonte Geocoding Origem'] = res[11]
                    df.at[idx, 'Endereco Oficial Origem'] = res[12]; df.at[idx, 'Confianca Destino'] = res[13]
                    df.at[idx, 'Score Num Destino'] = res[14]; df.at[idx, 'Distrito Destino'] = res[15]
                    df.at[idx, 'Municipio Destino'] = res[16]; df.at[idx, 'Fonte Geocoding Destino'] = res[17]
                    df.at[idx, 'Endereco Oficial Destino'] = res[18]; df.at[idx, 'Lat Origem'] = res[19]
                    df.at[idx, 'Lon Origem'] = res[20]; df.at[idx, 'Lat Destino'] = res[21]
                    df.at[idx, 'Lon Destino'] = res[22]; df.at[idx, 'Tempo Geocoding (s)'] = res[23]
                    df.at[idx, 'Tempo Roteamento (s)'] = res[24]; df.at[idx, 'Tempo Total (s)'] = res[25]
                    
                    score_o, score_d, score_r = res[8], res[14], res[6]
                    score_global = round((0.35 * score_o) + (0.35 * score_d) + (0.30 * score_r), 2)
                    df.at[idx, 'Score Final Global'] = score_global
                    df.at[idx, 'Status da Rota'] = "Excelente" if score_global >= 90 else "Boa" if score_global >= 80 else "Aceitável" if score_global >= 70 else "Revisar"
                else:
                    df.at[idx, 'Status da Rota'] = "Erro de Processamento"
                    
                concluidos += 1
                container_status.text(f"🚀 Roteamento Assíncrono Seguro: {concluidos} de {len(tarefas)} processados...")
                barra_progresso.progress(concluidos / len(tarefas))
                
            container_status.empty(); barra_progresso.empty()
            st.success("✨ Processamento em lote concluído com sucesso!")
            
            ordem_finais = ['Origem', 'Destino'] + novas_colunas
            df = df.reindex(columns=ordem_finais)
            
            output_buffer = io.BytesIO()
            with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer: df.to_excel(writer, index=False)
            
            st.write("---"); st.balloons()
            st.download_button(label="📥 Baixar Planilha Logística Processada", data=output_buffer.getvalue(), file_name="planilha_rotas_calculada.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
