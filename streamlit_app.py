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
from concurrent.futures import ThreadPoolExecutor

# Configuração canônica de UI/UX do Streamlit
st.set_page_config(
    page_title="Gerenciador de Rotas Inteligentes", 
    page_icon="🚗", 
    layout="centered"
)

# Definição dos arquivos de persistência local em disco
CACHE_IBGE_PATH = "municipios_ibge.pkl"

if "cache_geocodificacao" not in st.session_state:
    st.session_state["cache_geocodificacao"] = {}

SINONIMOS_SEMANTICOS = {
    "UNB": "UNIVERSIDADE DE BRASILIA",
    "CATOLICA": "UNIVERSIDADE CATOLICA",
    "JK": "JUSCELINO KUBITSCHEK",
    "HBDF": "HOSPITAL DE BASE",
    "HRAN": "HOSPITAL REGIONAL DA ASA NORTE",
    "RODOVIARIA": "TERMINAL RODOVIARIO"
}

LOCALIDADES_FUZZY_BASE = [
    "TAGUATINGA", "CEILANDIA", "SAMAMBAIA", "RECANTO DAS EMAS", "GUARA", 
    "GAMA", "SOBRADINHO", "PLANALTINA", "ASA NORTE", "ASA SUL", "PONTE ALTA"
]

# ==============================================================================
# 🎛️ CAMADA DE SEGURANÇA E CACHE PERSISTENTE IBGE (OTIMIZAÇÃO DE INICIALIZAÇÃO)
# ==============================================================================
def carregar_base_municipios_ibge():
    """Garante carga instantânea via serialização pkl local, mitigando requisições massivas"""
    if os.path.exists(CACHE_IBGE_PATH):
        try:
            with open(CACHE_IBGE_PATH, "rb") as f:
                return pickle.load(f)
        except Exception:
            pass
            
    base_municipios = {}
    try:
        r = requests.get("https://servicodados.ibge.gov.br/api/v1/localidades/municipios", timeout=10)
        if r.status_code == 200:
            for mun in r.json():
                nome_norm = unidecode(mun["nome"]).upper().strip()
                base_municipios[nome_norm] = {
                    "id": mun["id"],
                    "uf": mun["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper(),
                    "nome_oficial": mun["nome"]
                }
            with open(CACHE_IBGE_PATH, "wb") as f:
                pickle.dump(base_municipios, f)
    except Exception:
        pass
    return base_municipios

# Carrega o catálogo de cidades de forma performática na RAM
IBGE_MUNICIPIOS = carregar_base_municipios_ibge()

# ==============================================================================
# 🧹 PIPELINE DE ENGENHARIA DE STRINGS E RESOLUÇÃO SEMÂNTICA (CAMADA 1, 2, 2.5)
# ==============================================================================
def normalizar_endereco_universal(texto):
    """Camada 1 e 24: Limpeza Unicode, higienização e expansão estruturada de sinônimos"""
    if not texto or pd.isna(texto):
        return ""
    t = str(texto).strip()
    t = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', t)  # Expruga caracteres invisíveis
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
        
    return t.strip()

def corrigir_toponimo_fuzzy(texto_normalizado):
    """Camada 2: Aplica similaridade via RapidFuzz para reparar grafias truncadas"""
    if not texto_normalizado:
        return texto_normalizado
    match = process.extractOne(texto_normalizado, LOCALIDADES_FUZZY_BASE, scorer=fuzz.WRatio)
    if match and match[1] >= 88:
        return match[0]
    return texto_normalizado

def inferir_estado_ibge(texto_normalizado):
    """Deduz dinamicamente a UF baseando-se no dicionário serializado do IBGE"""
    palavras = texto_normalizado.split()
    for i in range(len(palavras)):
        for j in range(i + 1, len(palavras) + 1):
            chunk = " ".join(palavras[i:j])
            if chunk in IBGE_MUNICIPIOS:
                return IBGE_MUNICIPIOS[chunk]["uf"]
    return None

def expandir_contexto_incompleto(texto):
    """Garante amarração contextual mínima para inputs parciais/bairros sem UF"""
    texto_norm = normalizar_endereco_universal(texto)
    texto_norm = corrigir_toponimo_fuzzy(texto_norm)
    tokens = texto_norm.split()
    
    if len(tokens) <= 2 or not any(c.isdigit() for c in texto_norm):
        uf_inferida = inferir_estado_ibge(texto_norm)
        if uf_inferida:
            return f"{texto_norm}, {uf_inferida}, BRASIL"
            
    if "BRASIL" not in texto_norm:
        return f"{texto_norm}, BRASIL"
    return texto_norm

# ==============================================================================
# 🗺️ RESOLUÇÃO MULTI-FONTE, PARALELISMO ASSÍNCRONO E CONCORDÂNCIA SEMÂNTICA
# ==============================================================================
def extrair_dados_reais_google(origem_raw, destino_raw, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=True):
    """
    CAMADA BRUTA RETIDA - FALLBACK SECUNDÁRIO DO MOTOR DE ROTEAMENTO
    """
    if usar_coordenadas and lat_o and lon_o and lat_d and lon_d and lat_o != 0.0 and lat_d != 0.0:
        origem_param = f"{lat_o},{lon_o}"
        destino_param = f"{lat_d},{lon_d}"
        url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
    else:
        origem_param = requests.utils.quote(f"{origem_raw}".strip())
        destino_param = requests.utils.quote(f"{destino_raw}".strip())
        url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
    
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(str(origem_raw).strip())}&destination={requests.utils.quote(str(destino_raw).strip())}&travelmode=driving"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.google.com/maps", "Accept": "*/*"
    }
    try:
        resposta = requests.get(url_api, headers=headers, timeout=8)
        texto_resposta = resposta.text
        match_km = re.findall(r'\"(\d+[\.,]?\d*)\s*km\"', texto_resposta)
        match_tempo = re.findall(r'\"(\d+\s*h\s*\d+\s*min|\d+\s*h|\d+\s*min)\"', texto_resposta)
        if match_km and match_tempo:
            km_puro = float(match_km[0].replace('.', '').replace(',', '.'))
            envolve_balsa = "Sim" if any(re.search(p, texto_resposta.lower()) for p in [r'\"utilizar\s+balsa\b', r'\"pegar\s+balsa\b']) else "Não"
            return km_puro, match_tempo[0], link_maps, envolve_balsa
    except Exception:
        pass
    return None

def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    """Cálculo Matemático da Linha Reta Geodésica Teórica Perfeita (WGS-84)"""
    if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0: return 0.0
    try:
        a, b, f = 6378137.0, 6356752.314245, 1 / 298.257223563
        L = math.radians(lon2 - lon1)
        U1, U2 = math.atan((1 - f) * math.tan(math.radians(lat1))), math.atan((1 - f) * math.tan(math.radians(lat2)))
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
        return round((b * A * (sigma - deltaSigma)) / 1000, 2)
    except Exception: return 0.0

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

# --- ENGINES INDEPENDENTES PARA O EXECUTOR PARALELO (CAMADA 4 & 5) ---
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
    try:
        url = f"https://photon.komoot.io/api/?q={requests.utils.quote(query)}&limit=1"
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
    """Camada 5: Busca direcionada e qualificada a estruturas de POIs chaves do OpenStreetMap"""
    try:
        query_osm = f"""
        [out:json][timeout:8];
        (
          node["name"~"{texto_norm}",i]["amenity"];way["name"~"{texto_norm}",i]["amenity"];
          node["name"~"{texto_norm}",i]["building"];way["name"~"{texto_norm}",i]["building"];
          node["name"~"{texto_norm}",i]["healthcare"];way["name"~"{texto_norm}",i]["healthcare"];
          node["name"~"{texto_norm}",i]["education"];way["name"~"{texto_norm}",i]["education"];
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

def processar_consenso_e_pontuacao(candidatos, texto_cru):
    """Camada 8 e 9: Avaliação por concordância de atributos político-administrativos"""
    if not candidatos: return None
    
    for c1 in candidatos:
        score_centesimal = c1["score_base"]
        consenso_espacial = 0
        
        for c2 in candidatos:
            if c1["fonte"] != c2["fonte"]:
                dist = calcular_distancia_vincenty(c1["lat"], c1["lon"], c2["lat"], c2["lon"])
                if dist <= 10.0: consensus_weight = 25; consenso_espacial += 1
                
                # Validação Semântica Multivariável Cruzada (Corta homônimos de estado)
                if c1["cidade"] and c1["cidade"] == c2["cidade"]: score_centesimal += 20
                if c1["estado"] and c1["estado"] == c2["estado"]: score_centesimal += 15
                if c1["bairro"] and c1["bairro"] == c2["bairro"]: score_centesimal += 10
                
        c1["score_final"] = score_centesimal + (consenso_espacial * 25)
        
    candidatos.sort(key=lambda x: x["score_final"], reverse=True)
    vencedor = candidatos[0]
    
    # Executa o enriquecimento do campeão da linha
    m = executar_reverse_geocoding_enrichment(vencedor["lat"], vencedor["lon"])
    if m["cep"]: vencedor["score_final"] += 10
    
    confianca = "BAIXA"
    if vencedor["score_final"] >= 95: confianca = "ALTISSIMA"
    elif vencedor["score_final"] >= 80: confianca = "ALTA"
    elif vencedor["score_final"] >= 65: confianca = "MEDIA"
    
    rua_f = m["logradouro"] if m["logradouro"] else texto_cru.upper()
    endereco_f = ", ".join([c for c in [rua_f, m["bairro"], m["cidade"], m["estado"]] if c.strip()]) + ", BRASIL"
    
    return vencedor["lat"], vencedor["lon"], endereco_f, confianca, m["municipio"], m["distrito"]

def obter_coordenadas_e_endereco_oficial(localidade):
    """ORQUESTRADOR GERAL AS SÍNCRONO DA RESOLUÇÃO MULTI-FONTE PARALELIZADA"""
    texto_cru = str(localidade).strip()
    if not texto_cru or texto_cru.lower() == 'nan': return 0.0, 0.0, "", "BAIXA", "", ""
    
    if texto_cru in st.session_state["cache_geocodificacao"]:
        c = st.session_state["cache_geocodificacao"][texto_cru]
        return c["lat"], c["lon"], c["endereco"], c["confianca"], c["municipio"], c["distrito"]
        
    # Camada 3: Filtro estrito de CEP Completo
    digits_cep = re.sub(r'\D', '', texto_cru)
    if len(digits_cep) == 8 and (texto_cru.isdigit() or "-" in texto_cru):
        logr, bair, loca, uf = camada_postal_redundante(digits_cep)
        if loca:
            addr_c = f"{logr}, {bair}, {loca}, {uf}, CEP {digits_cep}, BRASIL"
            res_arc = API_ArcGIS(addr_c)
            lat, lon = (res_arc["lat"], res_arc["lon"]) if res_arc else (0.0, 0.0)
            return lat, lon, addr_c, "ALTISSIMA", loca, bair

    texto_expandido = expandir_contexto_incompleto(texto_cru)
    texto_norm = normalizar_endereco_universal(texto_cru)
    
    # --- PROCESSAMENTO PARALELO ASSÍNCRONO EM THREADPOOL (CAMADA 4) ---
    candidatos_validos = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        f_arc = executor.submit(API_ArcGIS, texto_expandido)
        f_osm = executor.submit(API_Nominatim, texto_expandido)
        f_pho = executor.submit(API_Photon, texto_expandido)
        f_poi = executor.submit(API_Overpass_POIs, texto_norm)
        
        for f in [f_arc, f_osm, f_pho, f_poi]:
            res = f.result()
            if res: candidatos_validos.append(res)
            
    res_final = processar_consenso_e_pontuacao(candidatos_validos, texto_cru)
    if res_final:
        st.session_state["cache_geocodificacao"][texto_cru] = {"lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], "confianca": res_final[3], "municipio": res_final[4], "distrito": res_final[5]}
        return res_final
        
    return 0.0, 0.0, texto_expandido, "BAIXA", "", ""

# ==============================================================================
# 🚗 MOTORES DE ROTEAMENTO INTEGRAIS COM REDUNDÂNCIA (CAMADA 2 & 4 ROTA)
# ==============================================================================
def rota_osrm(lat_o, lon_o, lat_d, lon_d):
    """CAMADA PRINCIPAL ESTÁVEL DE ROTEAMENTO (OSRM ENGINE CORPORATIVO)"""
    try:
        url = f"https://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=false"
        r = requests.get(url, timeout=6).json()
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
    """Pipeline Central de Roteamento com Inversão Hierárquica Baseada em Estabilidade"""
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    lat_o, lon_o, o_oficial, conf_o, mun_o, dist_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    lat_d, lon_d, d_oficial, conf_d, mun_d, dist_d = obter_coordenadas_e_endereco_oficial(destino_clean)
    
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)
    
    # Trava Antialucinação Geodésica de Segurança
    usar_coords = True if (lat_o != 0.0 and lat_d != 0.0) else False
    if usar_coords and dist_linha_reta > 150.0:
        siglas_originais = re.findall(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM|PA|MT)\b', origem_clean.upper() + " " + destino_clean.upper())
        if len(set(siglas_originais)) <= 1: usar_coords = False

    # --- EXECUÇÃO DO FLUXO DO MOTOR DE ROTAS EM CASCATA ---
    # Provedor Líder e Principal (OSRM - Estabilidade e SLA Total)
    if usar_coords:
        res_osrm = rota_osrm(lat_o, lon_o, lat_d, lon_d)
        if res_osrm:
            link_m = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(o_oficial)}&destination={requests.utils.quote(d_oficial)}&travelmode=driving"
            return res_osrm[0], res_osrm[1], link_m, "Não", dist_linha_reta, res_osrm[2], res_osrm[3], conf_o, dist_o, mun_o, conf_d, dist_d, mun_d

    # Fallback Secundário (Google Preview Scraper API)
    res_google = extrair_dados_reais_google(o_oficial, d_oficial, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=usar_coords)
    if res_google and res_google[0] < (dist_linha_reta * 4.0):
        return res_google[0], res_google[1], res_google[2], res_google[3], dist_linha_reta, "Google Preview", 100, conf_o, dist_o, mun_o, conf_d, dist_d, mun_d

    # Contingência Crítica de Fechamento (Modelo Geodésico Adaptativo - Erro Zero)
    link_m = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(o_oficial)}&destination={requests.utils.quote(d_oficial)}&travelmode=driving"
    km_terrestre = round(dist_linha_reta * obter_fator_desvio_rodoviario(dist_linha_reta), 2)
    v_comercial = 45.0 if km_terrestre < 50.0 else 65.0
    minutos_est = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0 else 0
    tempo_geo = f"{minutos_est} min" if minutos_est < 60 else f"{minutos_est // 60} h {minutos_est % 60} min"
    
    # RETORNO RETIFICADO DA EXPRESSÃO: Sem quebras de SyntaxError
    return km_terrestre, tempo_geo, link_m, "Não", dist_linha_reta, "Geodésico Adaptativo", 70, conf_o, dist_o, mun_o, conf_d, dist_d, mun_d

# --- INTERFACE VISUAL NO STREAMLIT ---
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
                'Fonte da Rota', 'Score da Rota', 'Confianca Origem', 
                'Distrito Origem', 'Municipio Origem', 'Confianca Destino', 
                'Distrito Destino', 'Municipio Destino'
            ]
            for col in novas_colunas: df[col] = None
                
            total_linhas = len(df)
            barra_progresso = st.progress(0)
            container_status = st.empty()
            
            for index, linha in df.iterrows():
                origem, destino = str(linha['Origem']).strip(), str(linha['Destino']).strip()
                
                if origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan':
                    container_status.text(f"🔢 Processando linha {index + 1} de {total_linhas}: {origem} ➔ {destino}")
                    
                    res_pipeline = calcular_pipeline_logistico(origem, destino)
                    
                    df.at[index, 'Distancia'] = res_pipeline[0]
                    df.at[index, 'Tempo'] = res_pipeline[1]
                    df.at[index, 'Link da Rota'] = res_pipeline[2]
                    df.at[index, 'Balsas'] = res_pipeline[3]
                    df.at[index, 'Linha Reta'] = res_pipeline[4]
                    df.at[index, 'Fonte da Rota'] = res_pipeline[5]
                    df.at[index, 'Score da Rota'] = res_pipeline[6]
                    df.at[index, 'Confianca Origem'] = res_pipeline[7]
                    df.at[index, 'Distrito Origem'] = res_pipeline[8]
                    df.at[index, 'Municipio Origem'] = res_pipeline[9]
                    df.at[index, 'Confianca Destino'] = res_pipeline[10]
                    df.at[index, 'Distrito Destino'] = res_pipeline[11]
                    df.at[index, 'Municipio Destino'] = res_pipeline[12]
                    
                    time.sleep(0.4)
                barra_progresso.progress((index + 1) / total_linhas)
                
            container_status.empty(); barra_progresso.empty()
            st.success("✨ Processamento em lote concluído com sucesso!")
            
            ordem_finais = [
                'Origem', 'Destino', 'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta',
                'Fonte da Rota', 'Score da Rota', 'Confianca Origem', 'Distrito Origem', 'Municipio Origem',
                'Confianca Destino', 'Distrito Destino', 'Municipio Destino'
            ]
            df = df.reindex(columns=ordem_finais)
            
            output_buffer = io.BytesIO()
            with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer: df.to_excel(writer, index=False)
            
            st.write("---"); st.balloons()
            st.download_button(
                label="📥 Baixar Planilha Logística Processada", data=output_buffer.getvalue(),
                file_name="planilha_rotas_calculada.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
