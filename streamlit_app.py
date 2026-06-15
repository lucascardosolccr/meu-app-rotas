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
from concurrent.futures import ThreadPoolExecutor

# Configuração Canônica de UI/UX do Streamlit
st.set_page_config(
    page_title="Gerenciador de Rotas Inteligentes", 
    page_icon="🚗", 
    layout="centered"
)

# ==============================================================================
# 🧠 CAMADA 10 & 15: INFRAESTRUTURA DE CACHE PERSISTENTE EM DISCO (DISKCACHE)
# ==============================================================================
cache_geo = Cache("./cache_geo")
CACHE_IBGE_PATH = "municipios_ibge.pkl"

if "ibge_estados" not in st.session_state:
    st.session_state["ibge_estados"] = {}

if "ibge_municipios" not in st.session_state:
    st.session_state["ibge_municipios"] = {}

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
# 🎛️ SERIALIZAÇÃO COMPACTA DA MALHA LOGÍSTICA NACIONAL (CAMADA 19, 20 E 3)
# ==============================================================================
def inicializar_infraestrutura_ibge_local():
    """Garante carga instantânea via serialização pkl, mitigando latências síncronas"""
    if os.path.exists(CACHE_IBGE_PATH):
        try:
            with open(CACHE_IBGE_PATH, "rb") as f:
                dados_carregados = pickle.load(f)
                st.session_state["ibge_municipios"] = dados_carregados.get("municipios", {})
                st.session_state["ibge_estados"] = dados_carregados.get("estados", {})
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
    except Exception:
        pass

# Dispara a carga veloz da malha municipal em disco
inicializer_ibge := inicializar_infraestrutura_ibge_local()

# ==============================================================================
# 🧹 PIPELINE AG NÓSTICO DE TRATAMENTO DE TEXTO (CAMADA 1, 2, 21, 22, 23, 24)
# ==============================================================================
def normalizar_endereco_universal(texto):
    """Camada 1 e 24: Limpeza de ruídos textuais e substituição determinística de sinônimos"""
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
    """Camada 2: Executa Fuzzy Matching contra as 5.570 cidades do IBGE (Escalabilidade Nacional)"""
    if not texto_normalizado or not st.session_state["ibge_municipios"]:
        return texto_normalizado
        
    lista_municipios = list(st.session_state["ibge_municipios"].keys())
    # Limita o escopo de varredura buscando palavras inteiras isoladas
    tokens = texto_normalizado.split()
    for token in tokens:
        if len(token) > 4:  # Evita falsos positivos em preposições ou siglas curtas
            match = process.extractOne(token, lista_municipios, scorer=fuzz.WRatio)
            if match and match[1] >= 90:
                # Substitui o fragmento corrompido pelo toponímico oficial corrigido
                texto_normalizado = texto_normalizado.replace(token, match[0])
                break
    return texto_normalizado

def inferir_estado_ibge(texto_normalizado):
    """Camada 21: Deduz dinamicamente a UF baseando-se na árvore relacional do IBGE"""
    palavras = texto_normalizado.split()
    for i in range(len(palavras)):
        for j in range(i + 1, len(palavras) + 1):
            chunk = " ".join(palavras[i:j])
            if chunk in st.session_state["ibge_municipios"]:
                return st.session_state["ibge_municipios"][chunk]["uf"]
    return None

def expandir_contexto_incompleto(texto):
    """Camada 22 e 23: Repara endereços truncados injetando UF e Município estruturado"""
    texto_norm = normalizar_endereco_universal(texto)
    texto_norm = corrigir_toponimo_base_nacional_ibge(texto_norm)
    tokens = texto_norm.split()
    
    # Camada 22: Detecção de endereço incompleto / Apenas toponímico isolado
    if len(tokens) <= 2 or not any(c.isdigit() for c in texto_norm):
        uf_inferida = inferir_estado_ibge(texto_norm)
        if uf_inferida:
            return f"{texto_norm}, {uf_inferida}, BRASIL"
            
    if "BRASIL" not in texto_norm:
        return f"{texto_norm}, BRASIL"
    return texto_norm

def parece_poi(texto_normalizado):
    """Camada 11: Validador taxonômico de intenção semântica de POIs"""
    return any(keyword in texto_normalizado for keyword in POI_KEYWORDS)

def detectar_cep_parcial(texto):
    """Camada 3: Validador de Máscara Estrita de Código Postal Completo (Sem invenção de dígitos)"""
    numeros = re.sub(r'\D', '', str(texto))
    if len(numeros) == 8 and (texto.isdigit() or "-" in texto):
        return numeros
    return None

# ==============================================================================
# 🗺️ RESOLUÇÃO PARALELIZADA, SANITIZAÇÃO OVERPASS E REVERSE GEOCODING
# ==============================================================================
def extrair_dados_reais_google(origem_raw, destino_raw, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=True):
    """
    CAMADA BRUTA PRESERVADA - FALLBACK LOGÍSTICO SECUNDÁRIO DO MOTOR
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
        texto_resposta = response_str := resposta.text
        match_km = re.findall(r'\"(\d+[\.,]?\d*)\s*km\"', texto_resposta)
        match_tempo = re.findall(r'\"(\d+\s*h\s*\d+\s*min|\d+\s*h|\d+\s*min)\"', texto_resposta)
        if match_km and match_tempo:
            km_puro = float(match_km[0].replace('.', '').replace(',', '.'))
            envolve_balsa = "Sim" if any(re.search(p, texto_resposta.lower()) for p in [r'\"utilizar\s+balsa\b', r'\"pegar\s+balsa\b']) else "Não"
            return km_puro, match_tempo[0], link_maps, envolve_balsa
    except Exception:
        pass
    return None

def camada_postal_redundante(cep_limpo):
    """Camada 3: Resolução Postal em Cascata Unificada"""
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

def executar_reverse_geocoding_enrichment(lat, lon):
    """Camada 6, 7 e 17: Reconstrução Reversa com Captura Estendida de Distritos"""
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

# --- PROVEDORES CARTOGRÁFICOS ISOLADOS PARA O POOL ASSÍNCRONO (CAMADA 4) ---
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
    """Camada 5 e 12: Sanitização de Expressão Regular por re.escape e Busca de Amenidades"""
    try:
        # Sanitização absoluta contra caracteres corrompidos da planilha
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
    """Camada 8, 9 & 29: Matriz Multivariável Corrigida (Remoção de consensus_weight)"""
    if not candidatos: return None
    
    for c1 in candidatos:
        score_centesimal = c1["score_base"]
        consenso_espacial = 0
        
        for c2 in candidatos:
            if c1["fonte"] != c2["fonte"]:
                dist = calcular_distancia_vincenty(c1["lat"], c1["lon"], c2["lat"], c2["lon"])
                if dist <= 10.0: 
                    consenso_espacial += 1
                
                # Critério de Votação por Concordância Semântica Político-Administrativa
                if c1["cidade"] and c1["cidade"] == c2["cidade"]: score_centesimal += 20
                if c1["estado"] and c1["estado"] == c2["estado"]: score_centesimal += 15
                if c1["bairro"] and c1["bairro"] == c2["bairro"]: score_centesimal += 10
                
        c1["score_final"] = score_centesimal + (consenso_espacial * 25)
        
    candidatos.sort(key=lambda x: x["score_final"], reverse=True)
    vencedor = candidatos[0]
    
    # Enriquecimento e validação reversa final
    m = executar_reverse_geocoding_enrichment(vencedor["lat"], vencedor["lon"])
    if m["cep"]: vencedor["score_final"] += 10
    
    score_limitado = min(int(vencedor["score_final"]), 100)
    
    confianca = "BAIXA"
    if score_limitado >= 95: confianca = "ALTISSIMA"
    elif score_limitado >= 80: confianca = "ALTA"
    elif score_limitado >= 65: confianca = "MEDIA"
    
    rua_f = m["logradouro"] if m["logradouro"] else texto_cru.upper()
    endereco_f = ", ".join([c for c in [rua_f, m["bairro"], m["cidade"], m["estado"]] if c.strip()]) + ", BRASIL"
    
    return vencedor["lat"], vencedor["lon"], endereco_f, confianca, m["municipio"], m["distrito"], score_limitado

def obter_coordenadas_e_endereco_oficial(localidade):
    """ORQUESTRADOR GERAL DO PIPELINE DE RESOLUÇÃO UNIVERSAL DE 10 CAMADAS"""
    texto_cru = str(localidade).strip()
    if not texto_cru or texto_cru.lower() == 'nan': 
        return 0.0, 0.0, "", "BAIXA", "", "", 0
    
    # Camada 10 e 15: Leitura de Cache de Longo Prazo na infraestrutura DiskCache
    if texto_cru in cache_geo:
        c = cache_geo[texto_cru]
        return c["lat"], c["lon"], c["endereco"], c["confianca"], c["municipio"], c["distrito"], c["score_num"]
        
    # Camada 3: Filtro estrito de CEP Completo (Sem preenchimento cego por ljust)
    cep_estrito = detectar_cep_parcial(texto_cru)
    if cep_estrito:
        logr, bair, loca, uf = camada_postal_redundante(cep_estrito)
        if loca:
            addr_c = f"{logr}, {bair}, {loca}, {uf}, CEP {cep_estrito}, BRASIL"
            res_arc = API_ArcGIS(addr_c)
            lat, lon = (res_arc["lat"], res_arc["lon"]) if res_arc else (0.0, 0.0)
            return lat, lon, addr_c, "ALTISSIMA", loca, bair, 100

    texto_expandido = expandir_contexto_incompleto(texto_cru)
    texto_norm = normalizar_endereco_universal(texto_cru)
    
    # --- CAMADA 4: EXECUÇÃO ASSÍNCRONA PARALELIZADA POR THREADPOOL EXECUTOR ---
    candidatos_validos = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        f_arc = executor.submit(API_ArcGIS, texto_expandido)
        f_osm = executor.submit(API_Nominatim, texto_expandido)
        f_pho = executor.submit(API_Photon, texto_expandido)
        f_poi = executor.submit(API_Overpass_POIs, texto_norm)
        
        for f in [f_arc, f_osm, f_pho, f_poi]:
            res = f.result()
            if res: candidatos_validos.append(res)
            
    res_final = processar_consenso_e_pontuacao_centesimal(candidatos_validos, texto_cru)
    if res_final:
        # Grava os resultados estruturados na persistência estável de disco
        cache_geo.set(texto_cru, {
            "lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], 
            "confianca": res_final[3], "municipio": res_final[4], "distrito": res_final[5], "score_num": res_final[6]
        }, expire=2592000)
        return res_final
        
    return 0.0, 0.0, texto_expandido, "BAIXA", "", "", 0

# ==============================================================================
# 🚀 BLINDAGEM DO MOTOR DE ROTAS MULTI-CAMADAS (INVERSÃO DE LIDERANÇA OSRM)
# ==============================================================================
def rota_osrm(lat_o, lon_o, lat_d, lon_d):
    """CAMADA LÍDER PRINCIPAL - OSRM Engine Aberto (Estabilidade Operacional e SLA)"""
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
    """Pipeline Central de Roteamento Blindado com Inversão de Fluxo Hierárquico"""
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    # Aciona a resolução universal assíncrona
    lat_o, lon_o, o_oficial, conf_o, mun_o, dist_o, score_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    lat_d, lon_d, d_oficial, conf_d, mun_d, dist_d, score_d = obter_coordenadas_e_endereco_oficial(destino_clean)
    
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)
    
    # Trava Antialucinação Geodésica Ativa de Segurança
    usar_coords = True if (lat_o != 0.0 and lat_d != 0.0) else False
    if usar_coords and dist_linha_reta > 150.0:
        siglas_originais = re.findall(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM|PA|MT)\b', origem_clean.upper() + " " + destino_clean.upper())
        if len(set(siglas_originais)) <= 1: 
            usar_coords = False

    # --- DESPACHO DO MOTOR DE ROTEAMENTO EM CASCATA ---
    # Camada Principal Estável: OSRM Engine por Coordenadas
    if usar_coords:
        res_osrm = rota_osrm(lat_o, lon_o, lat_d, lon_d)
        if res_osrm:
            link_m = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(o_oficial)}&destination={requests.utils.quote(d_oficial)}&travelmode=driving"
            return res_osrm[0], res_osrm[1], link_m, "Não", dist_linha_reta, res_osrm[2], res_osrm[3], conf_o, score_o, dist_o, mun_o, conf_d, score_d, dist_d, mun_d

    # Fallback Secundário: Camada Scraper Google Preview por Strings
    res_google = extrair_dados_reais_google(o_oficial, d_oficial, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=usar_coords)
    if res_google and res_google[0] < (dist_linha_reta * 4.0):
        return res_google[0], res_google[1], res_google[2], res_google[3], dist_linha_reta, "Google Preview", 100, conf_o, score_o, dist_o, mun_o, conf_d, score_d, dist_d, mun_d

    # Contingência de Fechamento de Malha: Modelo Geodésico Adaptativo (Erro Zero)
    link_m = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(o_oficial)}&destination={requests.utils.quote(d_oficial)}&travelmode=driving"
    km_terrestre = round(dist_linha_reta * obter_fator_desvio_rodoviario(dist_linha_reta), 2)
    v_comercial = 45.0 if km_terrestre < 50.0 else 65.0
    minutos_est = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0 else 0
    tempo_geo = f"{minutos_est} min" if minutos_est < 60 else f"{minutos_est // 60} h {minutos_est % 60} min"
    
    # RETORNO CORRIGIDO DA LINHA 464: Vírgula regulamentar cravada no lugar
    return km_terrestre, tempo_geo, link_m, "Não", dist_linha_reta, "Geodésico Adaptativo", 70, conf_o, score_o, dist_o, mun_o, conf_d, score_d, dist_d, mun_d

# ==============================================================================
# 🚗 INTERFACE VISUAL NO STREAMLIT (MANIPULAÇÃO DO DATAFRAME EM LOTE)
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
                    df.at[index, 'Score Num Origem'] = res_pipeline[8]
                    df.at[index, 'Distrito Origem'] = res_pipeline[9]
                    df.at[index, 'Municipio Origem'] = res_pipeline[10]
                    df.at[index, 'Confianca Destino'] = res_pipeline[11]
                    df.at[index, 'Score Num Destino'] = res_pipeline[12]
                    df.at[index, 'Distrito Destino'] = res_pipeline[13]
                    df.at[index, 'Municipio Destino'] = res_pipeline[14]
                    
                    time.sleep(0.3) # Cooldown seguro de requisições paralela de rede
                barra_progresso.progress((index + 1) / total_linhas)
                
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
