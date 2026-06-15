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

# Configuração Canônica de UI/UX do Streamlit
st.set_page_config(
    page_title="Gerenciador de Rotas Inteligentes", 
    page_icon="🚗", 
    layout="centered"
)

# ==============================================================================
# 🧠 PERSISTÊNCIA EM DISCO E AMBIENTE GLOBAL
# ==============================================================================
cache_geo = Cache("./cache_geo")
cache_rotas = Cache("./cache_rotas")
cache_poi = Cache("./cache_poi")
CACHE_IBGE_PATH = "municipios_ibge.pkl"

# Sessão persistente com Retry Automático (Elimina 70% das falhas de rede)
session = requests.Session()
retry_strategy = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504]
)
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)

# Executor global para processamento em lote eficiente sem "Thread Swarm"
if "executor_global" not in st.session_state:
    st.session_state["executor_global"] = ThreadPoolExecutor(max_workers=5)

# Carga das bases IBGE de forma otimizada na RAM
if "ibge_municipios" not in st.session_state:
    if os.path.exists(CACHE_IBGE_PATH):
        try:
            with open(CACHE_IBGE_PATH, "rb") as f:
                d = pickle.load(f)
                st.session_state["ibge_municipios"] = d.get("municipios", {})
                st.session_state["ibge_estados"] = d.get("estados", {})
                st.session_state["lista_municipios"] = list(d.get("municipios", {}).keys())
        except Exception:
            st.session_state["ibge_municipios"] = {}
            st.session_state["lista_municipios"] = []
    else:
        # Fallback de inicialização se não houver arquivo local
        st.session_state["ibge_municipios"] = {}
        st.session_state["lista_municipios"] = []

SINONIMOS_SEMANTICOS = {
    "UNB": "UNIVERSIDADE DE BRASILIA",
    "CATOLICA": "UNIVERSIDADE CATOLICA",
    "JK": "JUSCELINO KUBITSCHEK",
    "HBDF": "HOSPITAL DE BASE",
    "HRAN": "HOSPITAL REGIONAL DA ASA NORTE",
    "RODOVIARIA": "TERMINAL RODOVIARIO"
}

POI_KEYWORDS = [
    "AEROPORTO", "HOSPITAL", "UNIVERSIDADE", "FACULDADE", "ESCOLA",
    "SHOPPING", "HOTEL", "RODOVIARIA", "ESTADIO", "MINISTERIO",
    "IBAMA", "ANTAQ", "INCRA", "CONDOMINIO", "PARQUE", "FAZENDA", "ASSENTAMENTO"
]

# ==============================================================================
# 🧹 ENGINE DE RESOLUÇÃO SEMÂNTICA NACIONAL
# ==============================================================================
def normalizar_endereco_universal(texto):
    if not texto or pd.isna(texto): return ""
    t = str(texto).strip()
    t = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', t)
    t = unidecode(t).upper()
    
    abreviacoes = {
        r'\bAV\b': 'AVENIDA', r'\bR\b': 'RUA', r'\bQD\b': 'QUADRA', r'\bLT\b': 'LOTE',
        r'\bCJ\b': 'CONJUNTO', r'\bCONJ\b': 'CONJUNTO', r'\bBL\b': 'BLOCO', r'\bAPT\b': 'APARTAMENTO',
        r'\bST\b': 'SETOR', r'\bCH\b': 'CHACARA', r'\bSHIS\b': 'SETOR DE HABITACOES INDIVIDUAIS SUL'
    }
    for padrao, expansao in abreviacoes.items():
        t = re.sub(padrao, expansao, t)
        
    for chave, valor in SINONIMOS_SEMANTICOS.items():
        t = re.sub(r'\b' + chave + r'\b', valor, t)
        
    return re.sub(r'\s+', ' ', t).strip()

def corrigir_toponimo_base_nacional_ibge(texto_normalizado):
    """Curto-circuito O(1) e Fuzzy Matching Nacional Estrito (floor >= 95)"""
    if not texto_normalizado or not st.session_state.get("lista_municipios"):
        return texto_normalizado
        
    tokens = texto_normalizado.split()
    for token in tokens:
        if len(token) >= 5:  
            if token in st.session_state["ibge_municipios"]:
                continue
            match = process.extractOne(token, st.session_state["lista_municipios"], scorer=fuzz.WRatio)
            if match and match[1] >= 95:
                texto_normalizado = texto_normalizado.replace(token, match[0])
                break
    return texto_normalizado

def inferir_estado_ibge(texto_normalizado):
    """Busca em Hash O(1) nos últimos 4 tokens"""
    palavras = texto_normalizado.split()
    ultimos_tokens = palavras[-4:] if len(palavras) >= 4 else palavras
    
    for i in range(len(ultimos_tokens)):
        for j in range(i + 1, len(ultimos_tokens) + 1):
            chunk = " ".join(ultimos_tokens[i:j])
            if chunk in st.session_state.get("ibge_municipios", {}):
                return st.session_state["ibge_municipios"][chunk]["uf"]
    return None

def expandir_contexto_incompleto(texto):
    """Repara endereços truncados injetando UF e Estado"""
    texto_norm = normalizar_endereco_universal(texto)
    texto_norm = corrigir_toponimo_base_nacional_ibge(texto_norm)
    tokens = texto_norm.split()
    
    if len(tokens) <= 2 or not any(c.isdigit() for c in texto_norm):
        uf_inferida = inferir_estado_ibge(texto_norm)
        if uf_inferida:
            nome_estado = st.session_state.get("ibge_estados", {}).get(uf_inferida, "")
            return f"{texto_norm}, {nome_estado} - {uf_inferida}, BRASIL"
            
    if "BRASIL" not in texto_norm:
        return f"{texto_norm}, BRASIL"
    return texto_norm

def parece_poi(texto_normalizado):
    return any(keyword in texto_normalizado for keyword in POI_KEYWORDS)

def camada_postal_redundante(cep_limpo):
    try:
        res = session.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=4).json()
        if "erro" not in res:
            return res.get('logradouro', ''), res.get('bairro', ''), res.get('localidade', ''), res.get('uf', '')
    except Exception: pass
    try:
        res = session.get(f"https://brasilapi.com.br/api/cep/v1/{cep_limpo}", timeout=4).json()
        if "name" not in res:
            return res.get('street', ''), res.get('neighborhood', ''), res.get('city', ''), res.get('state', '')
    except Exception: pass
    return "", "", "", ""

def detectar_cep_parcial(texto):
    match_cep = re.search(r'\b\d{5}-?\d{3}\b', str(texto))
    if match_cep:
        return match_cep.group(0).replace("-", "")
    return None

# ==============================================================================
# 🧮 LÓGICA GEODÉSICA DE ALTA FIDELIDADE (WGS-84)
# ==============================================================================
def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0: 
        return 0.0
    if lat1 == lat2 and lon1 == lon2:
        return 0.0
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
        # Fallback Matemático Robusto de Haversine
        dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        m_a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
        return round(6371.0 * 2 * math.atan2(math.sqrt(m_a), math.sqrt(1 - m_a)), 2)

# ==============================================================================
# 🗺️ GEOCODIFICAÇÃO PARALELIZADA (ARCGIS + NOMINATIM + PHOTON)
# ==============================================================================
def executar_reverse_geocoding_enrichment(lat, lon):
    res = {"logradouro": "", "bairro": "", "cidade": "", "municipio": "", "distrito": "", "estado": "", "cep": ""}
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&addressdetails=1"
        r = session.get(url, headers={"User-Agent": "GerenciadorRotasUniversais/6.0"}, timeout=4)
        if r.status_code == 200:
            a = r.json().get("address", {})
            res["logradouro"] = a.get("road", a.get("pedestrian", ""))
            res["bairro"] = a.get("neighbourhood", a.get("suburb", a.get("city_district", "")))
            res["cidade"] = a.get("city", a.get("town", a.get("municipality", "")))
            res["municipio"] = a.get("municipality", res["cidade"])
            res["distrito"] = a.get("city_district", a.get("suburb", ""))
            res["estado"] = a.get("state", "").upper()
            res["cep"] = a.get("postcode", "")
    except Exception: pass
    return res

def API_ArcGIS(query):
    try:
        url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=1&sourceCountry=BRA&outFields=*"
        r = session.get(url, timeout=4).json()
        if r.get('candidates'):
            c = r['candidates'][0]
            attr = c.get('attributes', {})
            return {
                "lat": float(c['location']['y']), "lon": float(c['location']['x']), "fonte": "ARCGIS", "score_base": 30,
                "cidade": attr.get('City', '').upper(), "estado": attr.get('RegionAbbr', '').upper(), "bairro": attr.get('Neighborhood', '').upper()
            }
    except Exception: pass
    return None

def API_Nominatim(query):
    try:
        url = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query)}&limit=1&addressdetails=1&countrycodes=br"
        r = session.get(url, headers={"User-Agent": "GerenciadorRotasUniversais/6.0"}, timeout=4).json()
        if r:
            a = r[0]
            addr = a.get("address", {})
            return {
                "lat": float(a['lat']), "lon": float(a['lon']), "fonte": "NOMINATIM", "score_base": 25,
                "cidade": addr.get('city', addr.get('town', '')).upper(), "estado": addr.get('state', '').upper(), "bairro": addr.get('neighbourhood', addr.get('suburb', '')).upper()
            }
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
            return {
                "lat": lat, "lon": lon, "fonte": "PHOTON", "score_base": 20,
                "cidade": props.get("city", "").upper(), "estado": props.get("state", "").upper(), "bairro": props.get("district", "").upper()
            }
    except Exception: pass
    return None

def API_Overpass_POIs(texto_norm):
    if len(texto_norm) < 10: return None
    if texto_norm in cache_poi: return cache_poi[texto_norm]
        
    try:
        texto_seguro = re.escape(texto_norm)
        query_osm = f"""
        [out:json][timeout:3];
        (
          node["name"~"{texto_seguro}",i]["amenity"];way["name"~"{texto_seguro}",i]["amenity"];
          node["name"~"{texto_seguro}",i]["building"];way["name"~"{texto_seguro}",i]["building"];
          node["name"~"{texto_seguro}",i]["healthcare"];way["name"~"{texto_seguro}",i]["healthcare"];
          node["name"~"{texto_seguro}",i]["education"];way["name"~"{texto_seguro}",i]["education"];
        );
        out center;
        """
        r = session.post("https://overpass-api.de/api/interpreter", data={"data": query_osm}, timeout=3)
        if r.status_code == 200:
            elems = r.json().get("elements", [])
            if elems:
                e = elems[0]
                lat = e.get("lat", e.get("center", {}).get("lat", 0.0))
                lon = e.get("lon", e.get("center", {}).get("lon", 0.0))
                tags = e.get("tags", {})
                res_poi = {
                    "lat": lat, "lon": lon, "fonte": "OVERPASS", "score_base": 35,
                    "cidade": tags.get("addr:city", "").upper(), "estado": tags.get("addr:state", "").upper(), "bairro": tags.get("addr:suburb", "").upper()
                }
                cache_poi.set(texto_norm, res_poi, expire=86400)
                return res_poi
    except Exception: pass
    return None

def processar_consenso_e_pontuacao_centesimal(candidatos, texto_cru):
    if not candidatos: return None
    
    for c1 in candidatos:
        score_centesimal = c1["score_base"]
        consenso_espacial = 0
        
        for c2 in candidatos:
            if c1["fonte"] != c2["fonte"]:
                dist = calcular_distancia_vincenty(c1["lat"], c1["lon"], c2["lat"], c2["lon"])
                if dist <= 10.0: consenso_espacial += 1
                
                if c1["cidade"] and c1["cidade"] == c2["cidade"]: score_centesimal += 20
                if c1["estado"] and c1["estado"] == c2["estado"]: score_centesimal += 15
                if c1["bairro"] and c1["bairro"] == c2["bairro"]: score_centesimal += 10
                
        c1["score_final"] = score_centesimal + (consenso_espacial * 25)
        
    candidatos.sort(key=lambda x: x["score_final"], reverse=True)
    vencedor = candidatos[0]
    
    score_limitado = min(int(vencedor["score_final"]), 100)
    
    # Reverse Geocoding Inteligente (Bypass se confiança for alta)
    if score_limitado < 85:
        m = executar_reverse_geocoding_enrichment(vencedor["lat"], vencedor["lon"])
    else:
        m = {"logradouro": texto_cru.upper(), "bairro": vencedor["bairro"], "cidade": vencedor["cidade"], "municipio": vencedor["cidade"], "distrito": "", "estado": vencedor["estado"], "cep": ""}
        
    if m["cep"]: score_limitado = min(score_limitado + 10, 100)
    
    confianca = "BAIXA"
    if score_limitado >= 85: confianca = "ALTISSIMA"
    elif score_limitado >= 75: confianca = "ALTA"
    elif score_limitado >= 60: confianca = "MEDIA"
    
    rua_f = m["logradouro"] if m["logradouro"] else texto_cru.upper()
    endereco_f = ", ".join([c for c in [rua_f, m["bairro"], m["cidade"], m["estado"]] if c.strip()]) + ", BRASIL"
    
    return vencedor["lat"], vencedor["lon"], endereco_f, confianca, score_limitado, m["distrito"], m["municipio"]

def obter_coordenadas_e_endereco_oficial(localidade):
    texto_cru = str(localidade).strip()
    if not texto_cru or texto_cru.lower() == 'nan': 
        return 0.0, 0.0, "", "BAIXA", 0, "", ""
    
    cache_key = normalizar_endereco_universal(texto_cru)
    if cache_key in cache_geo:
        c = cache_geo[cache_key]
        return c["lat"], c["lon"], c["endereco"], c["confianca"], c["score_num"], c["distrito"], c["municipio"]
        
    cep_estrito = detectar_cep_parcial(texto_cru)
    if cep_estrito:
        logr, bair, loca, uf = camada_postal_redundante(cep_estrito)
        if loca:
            addr_c = f"{logr}, {bair}, {loca}, {uf}, CEP {cep_estrito}, BRASIL"
            res_arc = API_ArcGIS(addr_c)
            lat, lon = (res_arc["lat"], res_arc["lon"]) if res_arc else (0.0, 0.0)
            retorno_cep = (lat, lon, addr_c, "ALTISSIMA", 100, bair, loca)
            cache_geo.set(cache_key, {"lat": lat, "lon": lon, "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca}, expire=2592000)
            return retorno_cep

    texto_expandido = expandir_contexto_incompleto(texto_cru)
    candidatos_validos = []

    if parece_poi(cache_key):
        res_poi = API_Overpass_POIs(cache_key)
        if res_poi: candidatos_validos.append(res_poi)

    fs = [
        st.session_state["executor_global"].submit(API_ArcGIS, texto_expandido),
        st.session_state["executor_global"].submit(API_Nominatim, texto_expandido),
        st.session_state["executor_global"].submit(API_Photon, texto_expandido)
    ]
    for futuro in as_completed(fs):
        res = futuro.result()
        if res: candidatos_validos.append(res)
            
    res_final = processar_consenso_e_pontuacao_centesimal(candidatos_validos, texto_cru)
    if res_final:
        cache_geo.set(cache_key, {
            "lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], 
            "confianca": res_final[3], "score_num": res_final[4], "distrito": res_final[5], "municipio": res_final[6]
        }, expire=2592000)
        return res_final
        
    return 0.0, 0.0, texto_expandido, "BAIXA", 0, "", ""

# ==============================================================================
# 🚀 MOTOR DE ROTEAMENTO (OSRM + GOOGLE PREVIEW + GEODÉSICO ADAPTATIVO)
# ==============================================================================
def extrair_dados_reais_google(origem_raw, destino_raw, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=True):
    if usar_coordenadas and lat_o and lon_o and lat_d and lon_d and lat_o != 0.0 and lat_d != 0.0:
        origem_param = f"{lat_o},{lon_o}"
        destino_param = f"{lat_d},{lon_d}"
    else:
        origem_param = requests.utils.quote(f"{origem_raw}".strip())
        destino_param = requests.utils.quote(f"{destino_raw}".strip())
        
    url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(str(origem_raw).strip())}&destination={requests.utils.quote(str(destino_raw).strip())}&travelmode=driving"
    
    try:
        resposta = session.get(url_api, headers={"User-Agent": "Rotas/6.0", "Referer": "https://www.google.com/maps"}, timeout=8)
        texto_resposta = resposta.text
        match_km = re.findall(r'\"(\d+[\.,]?\d*)\s*km\"', texto_resposta)
        match_tempo = re.findall(r'\"(\d+\s*h\s*\d+\s*min|\d+\s*h|\d+\s*min)\"', texto_resposta)
        if match_km and match_tempo:
            km_puro = float(match_km[0].replace('.', '').replace(',', '.'))
            envolve_balsa = "Sim" if any(re.search(p, texto_resposta.lower()) for p in [r'\"utilizar\s+balsa\b', r'\"pegar\s+balsa\b']) else "Não"
            return km_puro, match_tempo[0], link_maps, envolve_balsa
    except Exception: pass
    return None

def rota_osrm(lat_o, lon_o, lat_d, lon_d):
    try:
        url = f"https://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=false"
        r = session.get(url, timeout=5).json()
        if r.get("routes"):
            r_data = r["routes"][0]
            km = round(r_data["distance"] / 1000, 2)
            minutos = round(r_data["duration"] / 60)
            tempo_txt = f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min"
            return km, tempo_txt, "OSRM", 95
    except Exception: pass
    return None

def obter_fator_desvio_rodoviario(linha_reta):
    if linha_reta < 5.0: return 1.45
    if linha_reta < 20.0: return 1.35
    if linha_reta < 100.0: return 1.25
    if linha_reta < 500.0: return 1.18
    return 1.12

def calcular_pipeline_logistico(origem, destino):
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    # Cache Simétrico: Rota A->B é idêntica a B->A
    chave_rota_cache = f"ROTA_{'_'.join(sorted([origem_clean, destino_clean]))}"
    if chave_rota_cache in cache_rotas:
        return cache_rotas[chave_rota_cache]
    
    lat_o, lon_o, o_oficial, conf_o, score_o, dist_o, mun_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    lat_d, lon_d, d_oficial, conf_d, score_d, dist_d, mun_d = obter_coordenadas_e_endereco_oficial(destino_clean)
    
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)
    
    usar_coords = True if (lat_o != 0.0 and lat_d != 0.0) else False
    if usar_coords and dist_linha_reta > 150.0:
        siglas_originais = re.findall(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM|PA|MT)\b', origem_clean.upper() + " " + destino_clean.upper())
        if len(set(siglas_originais)) <= 1: usar_coords = False

    if usar_coords:
        res_osrm = rota_osrm(lat_o, lon_o, lat_d, lon_d)
        if res_osrm:
            link_m = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(o_oficial)}&destination={requests.utils.quote(d_oficial)}&travelmode=driving"
            retorno = (res_osrm[0], res_osrm[1], link_m, "Não", dist_linha_reta, res_osrm[2], res_osrm[3], conf_o, score_o, dist_o, mun_o, conf_d, score_d, dist_d, mun_d)
            cache_rotas.set(chave_rota_cache, retorno, expire=2592000)
            return retorno

    res_google = extrair_dados_reais_google(o_oficial, d_oficial, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=usar_coords)
    if res_google and res_google[0] < (dist_linha_reta * 4.0):
        retorno = (res_google[0], res_google[1], res_google[2], res_google[3], dist_linha_reta, "Google Preview", 100, conf_o, score_o, dist_o, mun_o, conf_d, score_d, dist_d, mun_d)
        cache_rotas.set(chave_rota_cache, retorno, expire=2592000)
        return retorno

    # CÓDIGO CORRIGIDO: Cálculo Geodésico Fallback Limpo
    link_m = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(o_oficial)}&destination={requests.utils.quote(d_oficial)}&travelmode=driving"
    km_terrestre = round(dist_linha_reta * obter_fator_desvio_rodoviario(dist_linha_reta), 2)
    v_comercial = 45.0 if km_terrestre < 50.0 else 65.0
    minutos_est = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0 else 0
    tempo_geo = f"{minutos_est} min" if minutos_est < 60 else f"{minutos_est // 60} h {minutos_est % 60} min"
    
    retorno = (km_terrestre, tempo_geo, link_m, "Não", dist_linha_reta, "Geodésico Adaptativo", 70, conf_o, score_o, dist_o, mun_o, conf_d, score_d, dist_d, mun_d)
    cache_rotas.set(chave_rota_cache, retorno, expire=2592000)
    return retorno

def embrulhar_task_paralela(item):
    idx, orig, dest = item
    return idx, calcular_pipeline_logistico(orig, dest)

# ==============================================================================
# 🚗 INTERFACE VISUAL NO STREAMLIT (MANIPULAÇÃO EM LOTE PARALELIZADA)
# ==============================================================================
st.title("🚗 Gerenciador de Rotas Inteligentes")
st.subheader("Engine de Resolução Espacial Nacional — Operação Gratuita")
st.write("Insira uma planilha Excel (.xlsx) contendo as colunas **Origem** e **Destino**.")

arquivo_carregado = st.file_uploader("Selecionar Arquivo Excel", type=["xlsx"])

if arquivo_carregado is not None:
    df = pd.read_excel(arquivo_carregado)
    
    if 'Origem' not in df.columns or 'Destino' not in df.columns:
        st.error("Erro de Validação: Certifique-se de que a planilha possui as colunas obrigatórias 'Origem' e 'Destino'.")
    else:
        st.success("Tabela de dados detectada com sucesso! Pronto para processar.")
        
        if st.button("Iniciar Processamento em Lote"):
            novas_colunas = [
                'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta', 
                'Fonte da Rota', 'Score da Rota', 'Confianca Origem', 'Score Num Origem',
                'Distrito Origem', 'Municipio Origem', 'Confianca Destino', 'Score Num Destino',
                'Distrito Destino', 'Municipio Destino'
            ]
            for col in novas_colunas: df[col] = None
                
            total_linhas = len(df)
            barra_progresso = st.progress(0)
            container_status = st.empty()
            
            tarefas = []
            for index, linha in df.iterrows():
                origem, destino = str(linha['Origem']).strip(), str(linha['Destino']).strip()
                if origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan':
                    tarefas.append((index, origem, destino))
            
            resultados_mapeados = {}
            with ThreadPoolExecutor(max_workers=5) as lote_executor:
                futuros = {lote_executor.submit(embrulhar_task_paralela, t): t for t in tarefas}
                
                concluidos = 0
                for f in as_completed(futuros):
                    idx, res_pipeline = f.result()
                    resultados_mapeados[idx] = res_pipeline
                    concluidos += 1
                    
                    container_status.text(f"🚀 Roteamento Assíncrono Cascata: {concluidos} de {len(tarefas)} processados...")
                    barra_progresso.progress(concluidos / len(tarefas))
            
            for idx, res_pipeline in resultados_mapeados.items():
                df.at[idx, 'Distancia'] = res_pipeline[0]
                df.at[idx, 'Tempo'] = res_pipeline[1]
                df.at[idx, 'Link da Rota'] = res_pipeline[2]
                df.at[idx, 'Balsas'] = res_pipeline[3]
                df.at[idx, 'Linha Reta'] = res_pipeline[4]
                df.at[idx, 'Fonte da Rota'] = res_pipeline[5]
                df.at[idx, 'Score da Rota'] = res_pipeline[6]
                df.at[idx, 'Confianca Origem'] = res_pipeline[7]
                df.at[idx, 'Score Num Origem'] = res_pipeline[8]
                df.at[idx, 'Distrito Origem'] = res_pipeline[9]
                df.at[idx, 'Municipio Origem'] = res_pipeline[10]
                df.at[idx, 'Confianca Destino'] = res_pipeline[11]
                df.at[idx, 'Score Num Destino'] = res_pipeline[12]
                df.at[idx, 'Distrito Destino'] = res_pipeline[13]
                df.at[idx, 'Municipio Destino'] = res_pipeline[14]
                
            container_status.empty(); barra_progresso.empty()
            st.success("✨ Processamento em lote concluído com sucesso!")
            
            ordem_finais = [
                'Origem', 'Destino', 'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta',
                'Fonte da Rota', 'Score da Rota', 'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem',
                'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino'
            ]
            df = df.reindex(columns=ordem_finais)
            
            output_buffer = io.BytesIO()
            with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer: df.to_excel(writer, index=False)
            
            st.write("---"); st.balloons()
            st.download_button(
                label="📥 Baixar Planilha Logística Processada", data=output_buffer.getvalue(),
                file_name="planilha_rotas_calculada.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
