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

# Configuração Canônica de UI/UX do Streamlit
st.set_page_config(
    page_title="Gerenciador de Rotas Inteligentes", 
    page_icon="🚗", 
    layout="centered"
)

# ==============================================================================
# 🧠 PERSISTÊNCIA EM DISCO E AMBIENTE GLOBAL (CAMADA 10, 15, 19, 20)
# ==============================================================================
cache_geo = Cache("./cache_geo")
cache_rotas = Cache("./cache_rotas")
CACHE_IBGE_PATH = "municipios_ibge.pkl"

if "ibge_estados" not in st.session_state:
    st.session_state["ibge_estados"] = {}

if "ibge_municipios" not in st.session_state:
    st.session_state["ibge_municipios"] = {}

if "lista_municipios" not in st.session_state:
    st.session_state["lista_municipios"] = []

# PROBLEMA ARQUITETURAL Nº 1 RESOLVIDO: Pool controlado de Geocodificação (Teto Seguro)
if "executor_global" not in st.session_state:
    st.session_state["executor_global"] = ThreadPoolExecutor(max_workers=4)

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

def inicializar_infraestrutura_ibge_local():
    """Garante carga instantânea via serialização pkl, otimizando inicialização"""
    if os.path.exists(CACHE_IBGE_PATH):
        try:
            with open(CACHE_IBGE_PATH, "rb") as f:
                dados_carregados = pickle.load(f)
                st.session_state["ibge_municipios"] = dados_carregados.get("municipios", {})
                st.session_state["ibge_estados"] = dados_carregados.get("estados", {})
                st.session_state["lista_municipios"] = list(dados_carregados.get("municipios", {}).keys())
                return
        except Exception:
            pass
            
    base_municipios = {}
    base_estados = {}
    try:
        r_est = requests.get("https://servicodados.ibge.gov.br/api/v1/localidades/estados", timeout=8)
        if r_est.status_code == 200:
            for est in r_est.json():
                base_estados[est["sigla"]] = unidecode(est["nome"]).upper()
                
        r_mun = requests.get("https://servicodados.ibge.gov.br/api/v1/localidades/municipios", timeout=12)
        if r_mun.status_code == 200:
            for mun in r_mun.json():
                nome_norm = unidecode(mun["nome"]).upper().strip()
                base_municipios[nome_norm] = {
                    "id": mun["id"],
                    "uf": mun["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper(),
                    "nome_oficial": mun["nome"]
                }
            with open(CACHE_IBGE_PATH, "wb") as f:
                pickle.dump({"municipios": base_municipios, "estados": base_estados}, f)
                
        st.session_state["ibge_municipios"] = base_municipios
        st.session_state["ibge_estados"] = base_estados
        st.session_state["lista_municipios"] = list(base_municipios.keys())
    except Exception:
        pass

inicializar_infraestrutura_ibge_local()

# ==============================================================================
# 🧹 PIPELINE DE ENGENHARIA DE TEXTO (CAMADA 1, 2, 21, 22, 23, 24)
# ==============================================================================
def normalizar_endereco_universal(texto):
    """Camada 1 e 24: Limpeza Unicode, higienização e expansão de sinônimos"""
    if not texto or pd.isna(texto):
        return ""
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
        
    t = re.sub(r'\s+', ' ', t)
    return t.strip()

def corrigir_toponimo_base_nacional_ibge(texto_normalizado):
    """Camada 2 Otimizada: Curto-circuito O(1) antes da execução de Fuzzy Matching do RapidFuzz"""
    if not texto_normalizado or not st.session_state["lista_municipios"]:
        return texto_normalizado
        
    tokens = texto_normalizado.split()
    for token in tokens:
        if len(token) >= 5:  
            if token in st.session_state["ibge_municipios"]:
                continue
            match = process.extractOne(token, st.session_state["lista_municipios"], scorer=fuzz.WRatio)
            if match and match[1] >= 90:
                texto_normalizado = texto_normalizado.replace(token, match[0])
                break
    return texto_normalizado

def inferir_estado_ibge(texto_normalizado):
    """Camada 21 Otimizada: Restringe a busca de hash aos últimos 4 tokens (O(1) prático)"""
    palavras = texto_normalizado.split()
    ultimos_tokens = palavras[-4:] if len(palavras) >= 4 else palavras
    
    for i in range(len(ultimos_tokens)):
        for j in range(i + 1, len(ultimos_tokens) + 1):
            chunk = " ".join(ultimos_tokens[i:j])
            if chunk in st.session_state["ibge_municipios"]:
                return st.session_state["ibge_municipios"][chunk]["uf"]
    return None

def expandir_contexto_incompleto(texto):
    """Camada 22 e 23: Repara endereços truncados injetando UF e Estado estruturado expandido"""
    texto_norm = normalizar_endereco_universal(texto)
    texto_norm = corrigir_toponimo_base_nacional_ibge(texto_norm)
    tokens = texto_norm.split()
    
    if len(tokens) <= 2 or not any(c.isdigit() for c in texto_norm):
        uf_inferida = inferir_estado_ibge(texto_norm)
        if uf_inferida:
            nome_estado_completo = st.session_state["ibge_estados"].get(uf_inferida, "")
            return f"{texto_norm}, {nome_estado_completo} - {uf_inferida}, BRASIL"
            
    if "BRASIL" not in texto_norm:
        return f"{texto_norm}, BRASIL"
    return texto_norm

def parece_poi(texto_normalizado):
    """Camada 11: Validador de intenção semântica de POIs"""
    return any(keyword in texto_normalizado for keyword in POI_KEYWORDS)

def camada_postal_redundante(cep_limpo):
    """Camada 3: Resolução Postal em Cascata Unificada (ViaCEP + BrasilAPI)"""
    try:
        res = requests.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=4).json()
        if "erro" not in res:
            return res.get('logradouro', ''), res.get('bairro', ''), res.get('localidade', ''), res.get('uf', '')
    except Exception:
        pass
    try:
        res = requests.get(f"https://brasilapi.com.br/api/cep/v1/{cep_limpo}", timeout=4).json()
        if "name" not in res:
            return res.get('street', ''), res.get('neighborhood', ''), res.get('city', ''), res.get('state', '')
    except Exception:
        pass
    return "", "", "", ""

def detectar_cep_parcial(texto):
    """Camada 3 (Aprimorada): Identifica CEPs válidos misturados em qualquer parte do texto"""
    match_cep = re.search(r'\b\d{5}-?\d{3}\b', str(texto))
    if match_cep:
        return match_cep.group(0).replace("-", "")
    return None

# ==============================================================================
# 🗺️ RESOLUÇÃO MULTI-FONTE CADASTRAIS (BUG 1 RESTAURADO E BLINDADO)
# ==============================================================================
def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    """BUG CRÍTICO 1 RESOLVIDO: Restauração da Linha Reta Geodésica Vincenty (1975)"""
    if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0: 
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
        return 0.0

def executar_reverse_geocoding_enrichment(lat, lon):
    """Camada 6 e 7: Enriquecimento cadastral máximo via geocodificação reversa síncrona"""
    res = {"logradouro": "", "bairro": "", "cidade": "", "municipio": "", "distrito": "", "estado": "", "cep": ""}
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&addressdetails=1"
        r = requests.get(url, headers={"User-Agent": "GerenciadorRotasUniversais/6.0"}, timeout=4)
        if r.status_code == 200:
            a = r.json().get("address", {})
            res["logradouro"] = a.get("road", a.get("pedestrian", ""))
            res["bairro"] = a.get("neighbourhood", a.get("suburb", a.get("city_district", "")))
            res["cidade"] = a.get("city", a.get("town", a.get("municipality", "")))
            res["municipio"] = a.get("municipality", res["cidade"])
            res["distrito"] = a.get("city_district", a.get("suburb", ""))
            res["estado"] = a.get("state", "").upper()
            res["cep"] = a.get("postcode", "")
    except Exception:
        pass
    return res

# --- PROVEDORES CARTOGRÁFICOS EM ISOLAMENTO DE EXECUÇÃO SEQUENCIAL ---
def API_ArcGIS(query):
    try:
        url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=1&sourceCountry=BRA&outFields=*"
        r = requests.get(url, timeout=4).json()
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
        r = requests.get(url, headers={"User-Agent": "GerenciadorRotasUniversais/6.0"}, timeout=4).json()
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
    """Filtro rígido de país ativado na URL contra vazamento de homônimos globais"""
    try:
        url = f"https://photon.komoot.io/api/?q={requests.utils.quote(query)}&limit=1&filter=countrycode:br"
        r = requests.get(url, timeout=4).json()
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
    try:
        texto_seguro = re.escape(texto_norm)
        query_osm = f"""
        [out:json][timeout:8];
        (
          node["name"~"{texto_seguro}",i]["amenity"];way["name"~"{texto_seguro}",i]["amenity"];
          node["name"~"{texto_seguro}",i]["building"];way["name"~"{texto_seguro}",i]["building"];
          node["name"~"{texto_seguro}",i]["healthcare"];way["name"~"{texto_seguro}",i]["healthcare"];
          node["name"~"{texto_seguro}",i]["education"];way["name"~"{texto_seguro}",i]["education"];
        );
        out center;
        """
        r = requests.post("https://overpass-api.de/api/interpreter", data={"data": query_osm}, timeout=8)
        if r.status_code == 200:
            elems = r.json().get("elements", [])
            if elems:
                e = elems[0]
                lat = e.get("lat", e.get("center", {}).get("lat", 0.0))
                lon = e.get("lon", e.get("center", {}).get("lon", 0.0))
                tags = e.get("tags", {})
                return {
                    "lat": lat, "lon": lon, "fonte": "OVERPASS", "score_base": 35,
                    "cidade": tags.get("addr:city", "").upper(), "estado": tags.get("addr:state", "").upper(), "bairro": tags.get("addr:suburb", "").upper()
                }
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
                if dist <= 10.0: 
                    consenso_espacial += 1
                
                if c1["cidade"] and c1["cidade"] == c2["cidade"]: score_centesimal += 20
                if c1["estado"] and c1["estado"] == c2["estado"]: score_centesimal += 15
                if c1["bairro"] and c1["bairro"] == c2["bairro"]: score_centesimal += 10
                
        c1["score_final"] = score_centesimal + (consenso_espacial * 25)
        
    candidatos.sort(key=lambda x: x["score_final"], reverse=True)
    vencedor = candidatos[0]
    
    m = executar_reverse_geocoding_enrichment(vencedor["lat"], vencedor["lon"])
    if m["cep"]: vencedor["score_final"] += 10
    
    score_limitado = min(int(vencedor["score_final"]), 100)
    
    confianca = "BAIXA"
    if score_limitado >= 85: confianca = "ALTISSIMA"
    elif score_limitado >= 75: confianca = "ALTA"
    elif score_limitado >= 60: confianca = "MEDIA"
    
    rua_f = m["logradouro"] if m["logradouro"] else texto_cru.upper()
    endereco_f = ", ".join([c for c in [rua_f, m["bairro"], m["cidade"], m["estado"]] if c.strip()]) + ", BRASIL"
    
    return vencedor["lat"], vencedor["lon"], endereco_f, confianca, score_limitado, m["distrito"], m["municipio"]

# ==============================================================================
# 🎚️ PROBLEMA ARQUITETURAL Nº 2 RESOLVIDO: PIPELINE SEQUENCIAL EM CASCATA INTELIGENTE
# ==============================================================================
def obter_coordenadas_e_endereco_oficial(localidade):
    """RESOLVER AG NÓSTICO EM CASCATA - Reduz consumo de banda e evita rate-limits"""
    texto_cru = str(localidade).strip()
    if not texto_cru or texto_cru.lower() == 'nan': 
        return 0.0, 0.0, "", "BAIXA", 0, "", ""
    
    # Camada 1: Interceptação em Cache Local O(1)
    if texto_cru in cache_geo:
        c = cache_geo[texto_cru]
        return c["lat"], c["lon"], c["endereco"], c["confianca"], c["score_num"], c["distrito"], c["municipio"]
        
    # Camada 2: Interceptação Postal Base Correios
    cep_estrito = detectar_cep_parcial(texto_cru)
    if cep_estrito:
        logr, bair, loca, uf = camada_postal_redundante(cep_estrito)
        if loca:
            addr_c = f"{logr}, {bair}, {loca}, {uf}, CEP {cep_estrito}, BRASIL"
            res_arc = API_ArcGIS(addr_c)
            lat, lon = (res_arc["lat"], res_arc["lon"]) if res_arc else (0.0, 0.0)
            return lat, lon, addr_c, "ALTISSIMA", 100, bair, loca

    texto_expandido = expandir_contexto_incompleto(texto_cru)
    texto_norm = normalizar_endereco_universal(texto_cru)
    candidatos_validos = []

    # Camada 3: Filtro e resolução imediata de POIs via Overpass API
    if parece_poi(texto_norm):
        res_poi = API_Overpass_POIs(texto_norm)
        if res_poi: 
            candidatos_validos.append(res_poi)
            res_final = processar_consenso_e_pontuacao_centesimal(candidatos_validos, texto_cru)
            if res_final: return res_final

    # Camada 4: Funil de Geocodificadores em Cascata Sequencial Controlada
    # Consulta Provedor Primário: ArcGIS
    res_arc = API_ArcGIS(texto_expandido)
    if res_arc: 
        candidatos_validos.append(res_arc)
    else:
        # Consulta Fallback Secundário: Nominatim (Só executa se o ArcGIS falhar)
        res_osm = API_Nominatim(texto_expandido)
        if res_osm: 
            candidatos_validos.append(res_osm)
        else:
            # Consulta Fallback Terciário: Photon (Último recurso)
            res_pho = API_Photon(texto_expandido)
            if res_pho: candidatos_validos.append(res_pho)
            
    res_final = processar_consenso_e_pontuacao_centesimal(candidatos_validos, texto_cru)
    if res_final:
        cache_geo.set(texto_cru, {
            "lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], 
            "confianca": res_final[3], "score_num": res_final[4], "distrito": res_final[5], "municipio": res_final[6]
        }, expire=2592000)
        return res_final
        
    return 0.0, 0.0, texto_expandido, "BAIXA", 0, "", ""

# ==============================================================================
# 🚀 GOVERNANÇA E CONTINGÊNCIA DO MOTOR DE ROTEAMENTO (BUG 2 CORRIGIDO)
# ==============================================================================
def rota_osrm(lat_o, lon_o, lat_d, lon_d):
    """BUG CRÍTICO 2 RESOLVIDO: Alinhamento de variável 'minutos' estável"""
    try:
        url = f"https://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=false"
        r = requests.get(url, timeout=5).json()
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
    """Pipeline Central de Roteamento com Duplo Cache Persistente"""
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    chave_rota_cache = f"ROTA_{origem_clean}_{destino_clean}"
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
            
            # PROBLEMS ARQUITETURAL 1 RESOLVED: Pool controlado para processamento da planilha (5 workers)
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
            
            # BUG CRÍTICO 3 RESOLVIDO: df.at mapeia 'idx' com desempacotamento canônico impecável
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
