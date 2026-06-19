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

# ==============================================================================
# CONFIGURAÇÃO DE UI/UX E AMBIENTE
# ==============================================================================
st.set_page_config(page_title="Gerenciador de Rotas Inteligentes", page_icon="🚗", layout="centered")

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

session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)

lock_nominatim = Lock()
CACHE_IBGE_PATH = "municipios_ibge.pkl"

# Teto defensivo fixado em 8 workers para respeitar a Acceptable Use Policy das APIs
WORKERS_DISPONIVEIS = 8

if "executor_global" not in st.session_state:
    st.session_state["executor_global"] = ThreadPoolExecutor(max_workers=WORKERS_DISPONIVEIS)

# ==============================================================================
# 🎛️ DADOS GLOBAIS THREAD-SAFE (EXPANSÃO IBGE E CONTEXTO FUZZY)
# ==============================================================================
@st.cache_data
def carregar_dados_ibge():
    if os.path.exists(CACHE_IBGE_PATH):
        if time.time() - os.path.getmtime(CACHE_IBGE_PATH) > (30 * 86400):
            os.remove(CACHE_IBGE_PATH)
        else:
            try:
                with open(CACHE_IBGE_PATH, "rb") as f:
                    d = pickle.load(f)
                    return d.get("municipios", {}), d.get("estados", {}), d.get("distritos", {}), list(d.get("municipios", {}).keys()) + list(d.get("distritos", {}).keys())
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
                base_mun[nome_norm] = {"uf": mun["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper(), "municipio": nome_norm}
                
        r_dist = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/distritos", timeout=12)
        if r_dist.status_code == 200:
            for dist in r_dist.json():
                nome_dist = unidecode(dist["nome"]).upper().strip()
                nome_muni = unidecode(dist["municipio"]["nome"]).upper().strip()
                uf_dist = dist["municipio"]["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper()
                if nome_dist not in base_mun:
                    base_dist[nome_dist] = {"uf": uf_dist, "municipio": nome_muni}

            with open(CACHE_IBGE_PATH, "wb") as f:
                pickle.dump({"municipios": base_mun, "estados": base_est, "distritos": base_dist}, f)
    except Exception: pass
    
    lista_completa = list(base_mun.keys()) + list(base_dist.keys())
    return base_mun, base_est, base_dist, lista_completa

IBGE_MUNICIPIOS, IBGE_ESTADOS, IBGE_DISTRITOS, LISTA_TOPONIMOS = carregar_dados_ibge()

# Criação do dicionário Fuzzy Ponderado com Contexto (Cidade + UF)
LISTA_CONTEXTO_FUZZY = []
for k, v in IBGE_MUNICIPIOS.items(): LISTA_CONTEXTO_FUZZY.append(f"{k} {v['uf']}")
for k, v in IBGE_DISTRITOS.items(): LISTA_CONTEXTO_FUZZY.append(f"{k} {v['uf']}")
LISTA_CONTEXTO_FUZZY = list(set(LISTA_CONTEXTO_FUZZY))

SINONIMOS_SEMANTICOS = {
    "UNB": "UNIVERSIDADE DE BRASILIA", "CATOLICA": "UNIVERSIDADE CATOLICA",
    "JK": "JUSCELINO KUBITSCHEK", "HBDF": "HOSPITAL DE BASE DO DISTRITO FEDERAL",
    "HRAN": "HOSPITAL REGIONAL DA ASA NORTE", "RODOVIARIA": "TERMINAL RODOVIARIO"
}

POI_KEYWORDS = [
    "AEROPORTO", "HOSPITAL", "UNIVERSIDADE", "FACULDADE", "ESCOLA", "SHOPPING", 
    "HOTEL", "RODOVIARIA", "ESTADIO", "MINISTERIO", "AGENCIA", "BANCO", 
    "IGREJA", "FORUM", "TRIBUNAL", "DELEGACIA", "PREFEITURA", "CLINICA"
]

# ==============================================================================
# 🧹 ENGINE DE RESOLUÇÃO UNIVERSAL E ENDEREÇAMENTO CANÔNICO
# ==============================================================================
class ParserGeograficoBR:
    @staticmethod
    def extrair_componentes(texto):
        componentes = {"cep": "", "numero": "", "resto": texto}
        cep_match = re.search(r'\b\d{5}-?\d{3}\b', componentes["resto"])
        if cep_match:
            componentes["cep"] = cep_match.group(0).replace("-", "")
            componentes["resto"] = componentes["resto"].replace(cep_match.group(0), "").strip(" ,-")
        
        num_match = re.search(r'\b(?:N|NO|NUMERO|NUM)?\s*(\d{1,5})\b', componentes["resto"], re.IGNORECASE)
        if num_match: componentes["numero"] = num_match.group(1)
        return componentes

class MotorEnderecoCanônico:
    def __init__(self):
        self.rural_keys = ["FAZENDA", "SITIO", "ASSENTAMENTO", "CHACARA", "GLEBA", "NUCLEO RURAL"]
        self.bairro_keys = ["BAIRRO", "VILA", "JARDIM", "PARQUE", "RESIDENCIAL", "SETOR", "ASA SUL", "ASA NORTE", "LAGO SUL", "LAGO NORTE"]
        self.via_keys = ["RUA", "AVENIDA", "TRAVESSA", "ALAMEDA", "RODOVIA", "ESTRADA", "QUADRA", "SQN", "SQS", "SHIS", "QSC", "QS"]

    def normalizar(self, texto):
        if not texto or pd.isna(texto): return ""
        t = str(texto).strip()
        t = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', t)
        t = unidecode(t).upper()
        
        # Normalização Canônica de Rodovias Brasileiras
        def padronizar_rodovia(match):
            sigla = match.group(1)
            numero = match.group(2).zfill(3)
            return f"{sigla}-{numero}"
            
        padrao_rodovia = r'\b(BR|AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\s*[-]?\s*(\d{1,3})\b'
        t = re.sub(padrao_rodovia, padronizar_rodovia, t)
        
        abreviacoes = {
            r'\bAV\b': 'AVENIDA', r'\bR\b': 'RUA', r'\bQD\b': 'QUADRA', r'\bLT\b': 'LOTE',
            r'\bCJ\b': 'CONJUNTO', r'\bCONJ\b': 'CONJUNTO', r'\bBL\b': 'BLOCO', r'\bAPT\b': 'APARTAMENTO',
            r'\bST\b': 'SETOR', r'\bCH\b': 'CHACARA', r'\bROD\b': 'RODOVIA', r'\bKM\b': 'QUILOMETRO', 
            r'\bAL\b': 'ALAMEDA', r'\bTR\b': 'TRAVESSA', r'\bTV\b': 'TRAVESSA', 
            r'\bPCA\b': 'PRACA', r'\bPQ\b': 'PARQUE', r'\bSQN\b': 'SUPERQUADRA NORTE', 
            r'\bSQS\b': 'SUPERQUADRA SUL', r'\bCLN\b': 'COMERCIO LOCAL NORTE', r'\bCLS\b': 'COMERCIO LOCAL SUL'
        }
        for padrao, expansao in abreviacoes.items(): t = re.sub(padrao, expansao, t)
        for chave, valor in SINONIMOS_SEMANTICOS.items(): t = re.sub(r'\b' + chave + r'\b', valor, t)
        return re.sub(r'\s+', ' ', t).strip()

    def classificar_entrada(self, texto_norm):
        if texto_norm in cache_classificacao: return cache_classificacao[texto_norm]
        tipo = "LOGRADOURO"
        if re.search(r'\b\d{5}-?\d{3}\b', texto_norm): tipo = "CEP"
        elif any(k in texto_norm for k in POI_KEYWORDS): tipo = "POI"
        elif any(k in texto_norm for k in self.rural_keys): tipo = "RURAL"
        elif any(k in texto_norm for k in self.via_keys) and bool(re.search(r'\d+', texto_norm)): tipo = "ENDERECO_COMPLETO"
        elif any(k in texto_norm for k in self.bairro_keys): tipo = "BAIRRO"
        elif texto_norm in IBGE_MUNICIPIOS: tipo = "MUNICIPIO"
        elif texto_norm in IBGE_DISTRITOS: tipo = "DISTRITO"
        cache_classificacao.set(texto_norm, tipo, expire=2592000)
        return tipo

    def aplicar_fuzzy_multidimensional(self, texto_norm):
        if texto_norm in cache_fuzzy: return cache_fuzzy[texto_norm]
        tokens = texto_norm.split()
        for token in tokens:
            if len(token) >= 5 and token not in IBGE_MUNICIPIOS and token not in IBGE_DISTRITOS:
                top_matches = process.extract(token, LISTA_CONTEXTO_FUZZY, scorer=fuzz.WRatio, limit=5)
                if top_matches and top_matches[0][1] >= 85:
                    melhor_match = max(top_matches, key=lambda m: fuzz.token_set_ratio(texto_norm, m[0]))
                    if melhor_match[1] >= 85 and fuzz.token_set_ratio(texto_norm, melhor_match[0]) >= 90:
                        cidade_corrigida = melhor_match[0].rsplit(' ', 1)[0]
                        texto_norm = texto_norm.replace(token, cidade_corrigida)
                        break
        cache_fuzzy.set(texto_norm, texto_norm, expire=2592000)
        return texto_norm

    def inferir_ancora_geografica(self, texto_norm):
        palavras = texto_norm.split()
        for i in range(len(palavras)):
            for j in range(i + 1, len(palavras) + 1):
                chunk = " ".join(palavras[i:j])
                if chunk in IBGE_MUNICIPIOS: return IBGE_MUNICIPIOS[chunk]["uf"], chunk, ""
                if chunk in IBGE_DISTRITOS: return IBGE_DISTRITOS[chunk]["uf"], IBGE_DISTRITOS[chunk]["municipio"], chunk
        return None, None, None

    def construir_endereco_canonico(self, texto_cru):
        texto_norm = self.normalizar(texto_cru)
        parsed = ParserGeograficoBR.extrair_componentes(texto_norm)
        
        if parsed["cep"]:
            logr, bair, loca, uf, lat_cep, lon_cep = cascata_postal_tripla(parsed["cep"])
            if loca:
                num_str = f", {parsed['numero']}" if parsed["numero"] else ""
                if parsed["numero"]: lat_cep, lon_cep = 0.0, 0.0 # Força interpolação porta-a-porta se houver número
                return f"{logr}{num_str}, {bair}, {loca}, {uf}, BRASIL", "CEP", parsed["cep"], lat_cep, lon_cep

        texto_fuzzy = self.aplicar_fuzzy_multidimensional(texto_norm)
        tipo = self.classificar_entrada(texto_fuzzy)
        
        uf, municipio, distrito = self.inferir_ancora_geografica(texto_fuzzy)
        
        componentes = [texto_fuzzy]
        if distrito and distrito not in texto_fuzzy: componentes.append(distrito)
        if municipio and municipio not in texto_fuzzy: componentes.append(municipio)
        if uf and uf not in texto_fuzzy: componentes.append(uf)
        if "BRASIL" not in texto_fuzzy: componentes.append("BRASIL")
        
        endereco_canonico = ", ".join(componentes)
        endereco_canonico = re.sub(r',\s*,', ',', endereco_canonico).strip()
        
        return endereco_canonico, tipo, "", 0.0, 0.0

semantica = MotorEnderecoCanônico()

# ==============================================================================
# 🧮 LÓGICA GEODÉSICA, LIMITES DO BRASIL E CONTINGÊNCIA POSTAL (CAMADA 3)
# ==============================================================================
def validar_coordenada_brasil(lat, lon):
    """Bounding Box estendido do Território Brasileiro (inclui ilhas oceânicas)"""
    try:
        lat, lon = float(lat), float(lon)
        return (-35.0 <= lat <= 6.0) and (-75.0 <= lon <= -28.0)
    except (ValueError, TypeError):
        return False

def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    if not (-90 <= lat1 <= 90) or not (-90 <= lat2 <= 90) or not (-180 <= lon1 <= 180) or not (-180 <= lon2 <= 180): return 0.0
    if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0: return 0.0
    if lat1 == lat2 and lon1 == lon2: return 0.0
    try:
        a, b, f = 6378137.0, 6356752.314245, 1 / 298.257223563
        L = math.radians(lon2 - lon1)
        U1, U2 = math.atan((1 - f) * math.tan(math.radians(lat1))), math.atan((1 - f) * math.tan(math.radians(lat2)))
        sinU1, cosU1 = math.sin(U1), math.cos(U1)
        sinU2, cosU2 = math.sin(U2), math.cos(U2)
        lam = L
        for _ in range(100):
            sinLam, cosLam = math.sin(lam), math.cos(lam)
            sinSigma = math.sqrt((cosU2 * sinLam) ** 2 + (cosU1 * sinU2 - sinU1 * cosU2 * cosLam) ** 2)
            if sinSigma == 0: return 0.0
            cosSigma = sinU1 * sinU2 + cosU1 * cosU2 * cosLam
            sigma = math.atan2(sinSigma, cosSigma)
            sinAlpha = cosU1 * cosU2 * sinLam / sinSigma
            cosSqAlpha = 1 - sinAlpha ** 2
            cos2SigmaM = cosSigma - 2 * sinU1 * sinU2 / cosSqAlpha if cosSqAlpha != 0 else 0
            C = f / 16 * cosSqAlpha * (4 + f * (4 - 3 * cosSqAlpha))
            lambdaPrev = lam
            lam = L + (1 - f) * C * sinAlpha * (sigma + f * sinAlpha * (cos2SigmaM + C * cosSigma * (-1 + 2 * cos2SigmaM ** 2)))
            if abs(lam - lambdaPrev) < 1e-12: break
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
    if cep_limpo in cache_cep:
        d = cache_cep[cep_limpo]
        if len(d) == 4: return d[0], d[1], d[2], d[3], 0.0, 0.0
        return d
    lat, lon = 0.0, 0.0
    try:
        r = session.get(f"https://brasilapi.com.br/api/cep/v2/{cep_limpo}", timeout=4).json()
        if "city" in r:
            loc = r.get("location", {}).get("coordinates", {})
            if loc and "latitude" in loc and "longitude" in loc:
                try: lat, lon = float(loc["latitude"]), float(loc["longitude"])
                except (ValueError, TypeError): pass
            d = (r.get('street', ''), r.get('neighborhood', ''), r.get('city', ''), r.get('state', ''), lat, lon)
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception: pass
    try:
        with lock_nominatim:
            time.sleep(1.1)
            url_nom = f"https://nominatim.openstreetmap.org/search?format=json&postalcode={cep_limpo}&countrycodes=br&limit=1"
            r_nom = session.get(url_nom, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
            if r_nom: lat, lon = float(r_nom[0]['lat']), float(r_nom[0]['lon'])
    except Exception: pass
    try:
        r = session.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=4).json()
        if "erro" not in r:
            d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon)
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception: pass
    try:
        r = session.get(f"https://opencep.com/v1/{cep_limpo}", timeout=4).json()
        if "error" not in r:
            d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon)
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception: pass
    return "", "", "", "", 0.0, 0.0

# ==============================================================================
# 🗺️ GEOCODIFICAÇÃO MULTIMOTOR E REVERSE
# ==============================================================================
def API_Google_Geocoding_Scraper(query):
    try:
        url = f"https://www.google.com/maps/search/{requests.utils.quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = session.get(url, headers=headers, timeout=5, allow_redirects=True)
        match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.url)
        if not match: match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.text)
        if match: return {"lat": float(match.group(1)), "lon": float(match.group(2)), "fonte": "GOOGLE_MAPS", "score_base": 40, "cidade": "", "estado": "", "bairro": ""}
    except Exception: pass
    return None

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

def API_Overpass_POIs(texto_norm):
    if len(texto_norm) < 10: return None
    if texto_norm in cache_poi: return cache_poi[texto_norm]
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
                    res_poi = {"lat": lat, "lon": lon, "fonte": "OVERPASS", "score_base": 40, "cidade": tags.get("addr:city", "").upper(), "estado": tags.get("addr:state", "").upper(), "bairro": tags.get("addr:suburb", "").upper()}
                    cache_poi.set(texto_norm, res_poi, expire=86400)
                    return res_poi
        except Exception: continue
    return None

def processar_consenso_dinamico(candidatos, tipo_entrada, texto_cru):
    candidatos_nacionais = [c for c in candidatos if validar_coordenada_brasil(c["lat"], c["lon"])]
    if not candidatos_nacionais: return None
    
    tolerancia_km = 0.5 if tipo_entrada in ["ENDERECO_COMPLETO", "POI", "CEP"] else 2.0 if tipo_entrada in ["BAIRRO", "RURAL"] else 10.0
    
    for c1 in candidatos_nacionais:
        score_centesimal = c1["score_base"]
        consenso_espacial = 0
        for c2 in candidatos_nacionais:
            if c1["fonte"] != c2["fonte"]:
                dist = calcular_distancia_vincenty(c1["lat"], c1["lon"], c2["lat"], c2["lon"])
                if dist <= tolerancia_km: consenso_espacial += 1
                if c1["cidade"] and c1["cidade"] == c2["cidade"]: score_centesimal += 20
                if c1["estado"] and c1["estado"] == c2["estado"]: score_centesimal += 15
                if c1["bairro"] and c1["bairro"] == c2["bairro"]: score_centesimal += 10
        c1["score_final"] = score_centesimal + (consenso_espacial * 25)
        
    candidatos_nacionais.sort(key=lambda x: x["score_final"], reverse=True)
    vencedor = candidatos_nacionais[0]
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
# 🎚️ ORQUESTRADOR HIERÁRQUICO (RESOLUÇÃO DO ENDEREÇO CANÔNICO NACIONAL)
# ==============================================================================
def obter_coordenadas_e_endereco_oficial(localidade):
    texto_cru = str(localidade).strip()
    if not texto_cru or texto_cru.lower() == 'nan': return 0.0, 0.0, "", "BAIXA", 0, "", "", "N/A"
    
    endereco_canonico, tipo_entrada, cep_detectado, lat_cep, lon_cep = semantica.construir_endereco_canonico(texto_cru)
    
    cache_key = f"{tipo_entrada}_{endereco_canonico}"
    if cache_key in cache_geo:
        c = cache_geo[cache_key]
        return c["lat"], c["lon"], c["endereco"], c["confianca"], c["score_num"], c["distrito"], c["municipio"], c["fonte"]

    candidatos_validos = []

    if tipo_entrada == "CEP":
        cep_estrito = re.search(r'\b\d{5}-?\d{3}\b', texto_cru)
        if cep_estrito:
            cep_limpo = cep_estrito.group(0).replace("-", "")
            logr, bair, loca, uf, lat_c, lon_c = cascata_postal_tripla(cep_limpo)
            
            if loca:
                addr_c = f"{logr}, {bair}, {loca}, {uf}, CEP {cep_estrito.group(0)}, BRASIL"
                addr_c = re.sub(r',\s*,', ',', addr_c).strip(' ,')
                
                if lat_c != 0.0 and lon_c != 0.0 and validar_coordenada_brasil(lat_c, lon_c):
                    res_final = (lat_c, lon_c, addr_c, "ALTISSIMA", 100, bair, loca, "BrasilAPI/OSM Postal")
                    cache_geo.set(cache_key, {"lat": lat_c, "lon": lon_c, "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca, "fonte": "BrasilAPI/OSM Postal"}, expire=2592000)
                    return res_final
                
                res_arc = API_ArcGIS(addr_c)
                if res_arc and validar_coordenada_brasil(res_arc["lat"], res_arc["lon"]):
                    res_final = (res_arc["lat"], res_arc["lon"], addr_c, "ALTISSIMA", 100, bair, loca, "ViaCEP/ArcGIS")
                    cache_geo.set(cache_key, {"lat": res_arc["lat"], "lon": res_arc["lon"], "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca, "fonte": "ViaCEP/ArcGIS"}, expire=2592000)
                    return res_final

    res_google_geo = API_Google_Geocoding_Scraper(endereco_canonico)
    if res_google_geo: candidatos_validos.append(res_google_geo)

    if tipo_entrada == "POI" and not res_google_geo:
        res_poi = API_Overpass_POIs(semantica.normalizar(texto_cru))
        if res_poi: candidatos_validos.append(res_poi)

    res_arc = API_ArcGIS(endereco_canonico)
    if res_arc: candidatos_validos.append(res_arc)

    res_pho = API_Photon(endereco_canonico)
    if res_pho: candidatos_validos.append(res_pho)
            
    res_final = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru)
    
    if not res_final:
        res_nom = API_Nominatim(endereco_canonico)
        if res_nom:
            candidatos_validos.append(res_nom)
            res_final = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru)

    if res_final:
        cache_geo.set(cache_key, {"lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], "confianca": res_final[3], "score_num": res_final[4], "distrito": res_final[5], "municipio": res_final[6], "fonte": res_final[7]}, expire=2592000)
        return res_final
        
    return 0.0, 0.0, endereco_canonico, "BAIXA", 0, "", "", "N/A"

# ==============================================================================
# 🚀 MOTOR DE ROTEAMENTO CORPORATIVO (OSRM LÍDER -> GOOGLE FALLBACK)
# ==============================================================================
def extrair_dados_reais_google(origem_raw, destino_raw, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=True):
    cache_key = f"{origem_raw}|{destino_raw}"
    if cache_key in cache_google: return cache_google[cache_key]

    origem_param = f"{lat_o},{lon_o}" if usar_coordenadas else requests.utils.quote(origem_raw)
    destino_param = f"{lat_d},{lon_d}" if usar_coordenadas else requests.utils.quote(destino_raw)
    url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(origem_raw)}&destination={requests.utils.quote(destino_raw)}&travelmode=driving"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": "https://www.google.com/maps"}
    
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

def obter_fator_desvio_rodoviario(linha_reta):
    return 1.45 if linha_reta < 5.0 else 1.35 if linha_reta < 20.0 else 1.25 if linha_reta < 100.0 else 1.18

def calcular_pipeline_logistico(origem, destino):
    start_total = time.time()
    origem_clean, destino_clean = str(origem).strip(), str(destino).strip()
    
    chave_rota_cache = f"ROTA_{semantica.normalizar(origem_clean)}->{semantica.normalizar(destino_clean)}"
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

    if usar_coords:
        res_osrm = rota_osrm(lat_o, lon_o, lat_d, lon_d)
        if res_osrm:
            tempo_roteamento = round(time.time() - start_rot, 2)
            tempo_total = round(time.time() - start_total, 2)
            retorno = (res_osrm[0], res_osrm[1], link_fallback, "Não", dist_linha_reta, res_osrm[2], res_osrm[3], conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total)
            cache_rotas.set(chave_rota_cache, retorno, expire=2592000); return retorno

    res_google = extrair_dados_reais_google(end_oficial_o, end_oficial_d, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=usar_coords)
    if res_google:
        tempo_roteamento = round(time.time() - start_rot, 2)
        tempo_total = round(time.time() - start_total, 2)
        retorno = (res_google[0], res_google[1], res_google[2], res_google[3], dist_linha_reta, "Google Preview", res_google[4], conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total)
        cache_rotas.set(chave_rota_cache, retorno, expire=2592000); return retorno

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
    par_id, orig, dest = item
    try: return par_id, calcular_pipeline_logistico(orig, dest)
    except Exception as e:
        print(f"Erro no par {par_id}: {e}")
        return par_id, None

# ==============================================================================
# 🚗 INTERFACE VISUAL NO STREAMLIT (LOTE DEDUPLICADO O(U))
# ==============================================================================
st.title("🚗 Gerenciador de Rotas Inteligentes")
st.subheader("Engine de Resolução Espacial Nacional — Operação Corporativa")
st.write("Insira uma planilha Excel (.xlsx) contendo as colunas **Origem** e **Destino**.")

arquivo_carregado = st.file_uploader("Selecionar Arquivo Excel", type=["xlsx"])

if arquivo_carregado is not None:
    df = pd.read_excel(arquivo_carregado)
    df.columns = df.columns.str.strip().str.title()
    
    if 'Origem' not in df.columns or 'Destino' not in df.columns:
        st.error("Erro de Validação: A planilha deve possuir as colunas 'Origem' e 'Destino'.")
    else:
        MAX_LINHAS = 5000
        if len(df) > MAX_LINHAS:
            st.error(f"⚠️ Limite arquitetural de {MAX_LINHAS} linhas excedido. Fracione o arquivo.")
            st.stop()
            
        st.success(f"Tabela com {len(df)} registros mapeada! Pronto para processar.")
        
        if st.button("Iniciar Processamento em Lote"):
            novas_colunas = [
                'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta', 'Fonte da Rota', 'Score da Rota', 
                'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem', 'Fonte Geocoding Origem', 'Endereco Oficial Origem',
                'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino', 'Fonte Geocoding Destino', 'Endereco Oficial Destino',
                'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)', 'Score Final Global', 'Status da Rota'
            ]
            for col in novas_colunas: df[col] = None
                
            pares_unicos = set()
            mapeamento_linhas = []
            
            for index, linha in df.iterrows():
                origem = str(linha['Origem']).strip() if pd.notna(linha['Origem']) else ""
                destino = str(linha['Destino']).strip() if pd.notna(linha['Destino']) else ""
                if origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan':
                    par = (origem, destino)
                    pares_unicos.add(par)
                    mapeamento_linhas.append((index, origem, destino))
            
            if not pares_unicos:
                st.warning("Nenhuma linha contendo endereços válidos detectada.")
                st.stop()
                
            st.info(f"Otimização Ativa: Detectadas {len(pares_unicos)} rotas únicas em {len(mapeamento_linhas)} linhas válidas.")
                
            resultados_unicos = {}
            executor_lote = st.session_state["executor_global"]
            tarefas_unicas = [(par, par[0], par[1]) for par in pares_unicos]
            futuros = {executor_lote.submit(embrulhar_task_paralela, t): t for t in tarefas_unicas}
            
            concluidos = 0
            barra_progresso = st.progress(0)
            container_status = st.empty()
            
            for f in as_completed(futuros):
                par_id, res = f.result()
                resultados_unicos[par_id] = res
                    
                concluidos += 1
                container_status.text(f"🚀 Roteamento Assíncrono (Rotas Únicas): {concluidos} / {len(pares_unicos)}")
                barra_progresso.progress(concluidos / len(pares_unicos))
                
            container_status.text("✨ Distribuindo resultados na matriz principal...")
            
            for idx, origem, destino in mapeamento_linhas:
                par = (origem, destino)
                res = resultados_unicos.get(par)
                
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

            container_status.empty(); barra_progresso.empty()
            st.success("✨ Processamento em lote corporativo concluído!")
            
            ordem_finais = ['Origem', 'Destino'] + novas_colunas
            df = df.reindex(columns=ordem_finais)
            
            output_buffer = io.BytesIO()
            with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer: df.to_excel(writer, index=False)
            st.session_state['planilha_pronta'] = output_buffer.getvalue()

    if 'planilha_pronta' in st.session_state:
        st.write("---"); st.balloons()
        st.download_button(label="📥 Baixar Planilha Logística Processada", data=st.session_state['planilha_pronta'], file_name="planilha_rotas_calculada.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
