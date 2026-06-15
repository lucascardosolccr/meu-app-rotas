import streamlit as st
import pandas as pd
import requests
import time
import math
import io
import re
import os
import pickle
import logging
import atexit
from typing import Optional, Tuple, Dict, List
from unidecode import unidecode
from rapidfuzz import process, fuzz
from diskcache import Cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

# ==============================================================================
# 📋 CONFIGURAÇÃO DE LOGGING
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==============================================================================
# 🔐 CARREGAMENTO DE VARIÁVEIS DE AMBIENTE
# ==============================================================================
load_dotenv()
ORS_API_KEY = os.getenv("ORS_API_KEY", "5b3ce3597851110001cf6248a7b8b6ac4e6d4e00b7c6f8b4e5b3aa60")

if ORS_API_KEY == "5b3ce3597851110001cf6248a7b8b6ac4e6d4e00b7c6f8b4e5b3aa60":
    logger.warning("⚠️  Usando ORS_API_KEY padrão. Configure variável de ambiente para produção!")

# ==============================================================================
# 🎨 CONFIGURAÇÃO CANÔNICA DE UI/UX DO STREAMLIT
# ==============================================================================
st.set_page_config(
    page_title="Gerenciador de Rotas Inteligentes",
    page_icon="🚗",
    layout="centered"
)

# ==============================================================================
# 🔌 SESSION HTTP GLOBAL COM RETRY AUTOMÁTICO (MELHORIA 1 + 9)
# ==============================================================================
def criar_session_http() -> requests.Session:
    """Cria uma requests.Session com retry automático em erros transitórios"""
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retry)
    s = requests.Session()
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    logger.info("✅ Session HTTP criada com retry automático")
    return s

if "http_session" not in st.session_state:
    st.session_state["http_session"] = criar_session_http()

session = st.session_state["http_session"]

# ==============================================================================
# 🧹 CLEANUP AUTOMÁTICO DE RECURSOS
# ==============================================================================
def cleanup_recursos():
    """Libera recursos antes de encerrar"""
    if "executor_global" in st.session_state:
        executor = st.session_state["executor_global"]
        executor.shutdown(wait=False)
        logger.info("✅ ThreadPoolExecutor finalizado")
    
    # Fechar caches
    try:
        if "cache_geo" in globals():
            cache_geo.close()
        if "cache_rotas" in globals():
            cache_rotas.close()
        if "cache_poi" in globals():
            cache_poi.close()
        logger.info("✅ Caches fechados")
    except Exception as e:
        logger.error(f"❌ Erro ao fechar caches: {e}")

atexit.register(cleanup_recursos)

# ==============================================================================
# 🧠 PERSISTÊNCIA EM DISCO E AMBIENTE GLOBAL
# ==============================================================================
# MELHORIA: Usar parâmetros de limite de tamanho
cache_geo = Cache("./cache_geo", size_limit=500_000_000)      # 500 MB
cache_rotas = Cache("./cache_rotas", size_limit=300_000_000)  # 300 MB
cache_poi = Cache("./cache_poi", size_limit=200_000_000)      # 200 MB
CACHE_IBGE_PATH = "municipios_ibge.pkl"

if "ibge_estados" not in st.session_state:
    st.session_state["ibge_estados"] = {}

if "ibge_municipios" not in st.session_state:
    st.session_state["ibge_municipios"] = {}

if "lista_municipios" not in st.session_state:
    st.session_state["lista_municipios"] = []

# MELHORIA 4: Pool global reutilizado entre execuções do Streamlit (com cleanup)
if "executor_global" not in st.session_state:
    st.session_state["executor_global"] = ThreadPoolExecutor(max_workers=5)
    logger.info("🚀 ThreadPoolExecutor global inicializado")

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
# 📡 INICIALIZAÇÃO IBGE
# ==============================================================================
def inicializar_infraestrutura_ibge_local() -> None:
    """Carrega dados IBGE do cache ou da API"""
    if os.path.exists(CACHE_IBGE_PATH):
        try:
            with open(CACHE_IBGE_PATH, "rb") as f:
                dados = pickle.load(f)
                st.session_state["ibge_municipios"] = dados.get("municipios", {})
                st.session_state["ibge_estados"] = dados.get("estados", {})
                st.session_state["lista_municipios"] = list(dados.get("municipios", {}).keys())
                logger.info(f"✅ IBGE carregado do cache: {len(st.session_state['lista_municipios'])} municípios")
                return
        except Exception as e:
            logger.warning(f"⚠️  Erro ao carregar cache IBGE: {e}")

    base_municipios = {}
    base_estados = {}
    try:
        logger.info("📡 Buscando dados IBGE da API...")
        r_est = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/estados", timeout=8)
        if r_est.status_code == 200:
            for est in r_est.json():
                base_estados[est["sigla"]] = unidecode(est["nome"]).upper()
            logger.info(f"✅ {len(base_estados)} estados carregados")

        r_mun = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/municipios", timeout=12)
        if r_mun.status_code == 200:
            for mun in r_mun.json():
                nome_norm = unidecode(mun["nome"]).upper().strip()
                base_municipios[nome_norm] = {
                    "id": mun["id"],
                    "uf": mun["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper(),
                    "nome_oficial": mun["nome"]
                }
            logger.info(f"✅ {len(base_municipios)} municípios carregados")
            
            with open(CACHE_IBGE_PATH, "wb") as f:
                pickle.dump({"municipios": base_municipios, "estados": base_estados}, f)
            logger.info("💾 Cache IBGE salvo em disco")

        st.session_state["ibge_municipios"] = base_municipios
        st.session_state["ibge_estados"] = base_estados
        st.session_state["lista_municipios"] = list(base_municipios.keys())
    except Exception as e:
        logger.error(f"❌ Erro ao carregar IBGE: {e}")

inicializar_infraestrutura_ibge_local()

# ==============================================================================
# 🧹 PIPELINE DE ENGENHARIA DE TEXTO
# ==============================================================================
def normalizar_endereco_universal(texto: str) -> str:
    """Normaliza endereço: remove acentos, expande abreviações, aplica sinônimos"""
    if not texto or pd.isna(texto):
        return ""
    t = str(texto).strip()
    t = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', t)  # Remove caracteres de controle
    t = unidecode(t).upper()

    abreviacoes = {
        r'\bAV\b': 'AVENIDA', r'\bR\b': 'RUA', r'\bQD\b': 'QUADRA', r'\bLT\b': 'LOTE',
        r'\bCJ\b': 'CONJUNTO', r'\bCONJ\b': 'CONJUNTO', r'\bBL\b': 'BLOCO',
        r'\bAPT\b': 'APARTAMENTO', r'\bST\b': 'SETOR', r'\bCH\b': 'CHACARA',
        r'\bSHIS\b': 'SETOR DE HABITACOES INDIVIDUAIS SUL'
    }
    for padrao, expansao in abreviacoes.items():
        t = re.sub(padrao, expansao, t)

    for chave, valor in SINONIMOS_SEMANTICOS.items():
        t = re.sub(r'\b' + chave + r'\b', valor, t)

    t = re.sub(r'\s+', ' ', t)
    return t.strip()

def corrigir_toponimo_base_nacional_ibge(texto_normalizado: str) -> str:
    """MELHORIA 7: Threshold elevado para 95 para evitar correções indevidas"""
    if not texto_normalizado or not st.session_state["lista_municipios"]:
        return texto_normalizado

    tokens = texto_normalizado.split()
    for token in tokens:
        if len(token) >= 5:
            if token in st.session_state["ibge_municipios"]:
                continue
            match = process.extractOne(token, st.session_state["lista_municipios"], scorer=fuzz.WRatio)
            if match and match[1] >= 95:
                texto_normalizado = texto_normalizado.replace(token, match[0])
                logger.debug(f"🔧 Topônimo corrigido: {token} → {match[0]}")
                break
    return texto_normalizado

def inferir_estado_ibge(texto_normalizado: str) -> Optional[str]:
    """Infere UF a partir dos últimos tokens do endereço"""
    palavras = texto_normalizado.split()
    ultimos_tokens = palavras[-4:] if len(palavras) >= 4 else palavras

    for i in range(len(ultimos_tokens)):
        for j in range(i + 1, len(ultimos_tokens) + 1):
            chunk = " ".join(ultimos_tokens[i:j])
            if chunk in st.session_state["ibge_municipios"]:
                return st.session_state["ibge_municipios"][chunk]["uf"]
    return None

def expandir_contexto_incompleto(texto: str) -> str:
    """Expande endereço incompleto inferindo estado e país"""
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

def parece_poi(texto_normalizado: str) -> bool:
    """Detecta se texto é um POI (ponto de interesse)"""
    return any(keyword in texto_normalizado for keyword in POI_KEYWORDS)

def camada_postal_redundante(cep_limpo: str) -> Tuple[str, str, str, str]:
    """Valida CEP em múltiplas bases (ViaCEP, BrasilAPI)"""
    try:
        res = session.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=4).json()
        if "erro" not in res:
            logger.debug(f"✅ CEP {cep_limpo} validado via ViaCEP")
            return res.get('logradouro',''), res.get('bairro',''), res.get('localidade',''), res.get('uf','')
    except Exception as e:
        logger.debug(f"⚠️  ViaCEP falhou para {cep_limpo}: {e}")
    
    try:
        res = session.get(f"https://brasilapi.com.br/api/cep/v1/{cep_limpo}", timeout=4).json()
        if "name" not in res:
            logger.debug(f"✅ CEP {cep_limpo} validado via BrasilAPI")
            return res.get('street',''), res.get('neighborhood',''), res.get('city',''), res.get('state','')
    except Exception as e:
        logger.debug(f"⚠️  BrasilAPI falhou para {cep_limpo}: {e}")
    
    return "", "", "", ""

def detectar_cep_parcial(texto: str) -> Optional[str]:
    """Detecta CEP no formato XXXXX-XXX ou XXXXXXXX"""
    match_cep = re.search(r'\b\d{5}-?\d{3}\b', str(texto))
    if match_cep:
        return match_cep.group(0).replace("-", "")
    return None

# ==============================================================================
# 🗺️ RESOLUÇÃO MULTI-FONTE CADASTRAIS
# ==============================================================================
def calcular_distancia_vincenty(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcula distância geodésica usando fórmula de Vincenty (WGS-84)"""
    if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0:
        logger.debug("⚠️  Coordenadas inválidas (0,0) detectadas")
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
            sinSigma = math.sqrt((cosU2*sinLambda)**2 + (cosU1*sinU2 - sinU1*cosU2*cosLambda)**2)
            if sinSigma == 0:
                return 0.0
            cosSigma = sinU1*sinU2 + cosU1*cosU2*cosLambda
            sigma = math.atan2(sinSigma, cosSigma)
            sinAlpha = cosU1*cosU2*sinLambda/sinSigma
            cosSqAlpha = 1 - sinAlpha**2
            cos2SigmaM = cosSigma - 2*sinU1*sinU2/cosSqAlpha if cosSqAlpha != 0 else 0
            C = f/16*cosSqAlpha*(4 + f*(4 - 3*cosSqAlpha))
            lambdaPrev = lambda_lon
            lambda_lon = L + (1-f)*C*sinAlpha*(sigma + f*sinAlpha*(cos2SigmaM + C*cosSigma*(-1 + 2*cos2SigmaM**2)))
            if abs(lambda_lon - lambdaPrev) < 1e-12:
                break
        
        uSq = cosSqAlpha*(a**2 - b**2)/(b**2)
        A = 1 + uSq/16384*(4096 + uSq*(-768 + uSq*(320 - 175*uSq)))
        B = uSq/1024*(256 + uSq*(-128 + uSq*(74 - 47*uSq)))
        deltaSigma = B*sinSigma*(cos2SigmaM + B/4*(cosSigma*(-1 + 2*cos2SigmaM**2) - B/6*cos2SigmaM*(-3 + 4*sinSigma**2)*(-3 + 4*cos2SigmaM**2)))
        s = b*A*(sigma - deltaSigma)
        return round(s/1000, 2)
    except Exception as e:
        logger.error(f"❌ Erro ao calcular Vincenty: {e}")
        return 0.0

def executar_reverse_geocoding_enrichment(lat: float, lon: float) -> Dict:
    """Reverse geocoding via Nominatim (enriquecimento de dados)"""
    res = {"logradouro":"","bairro":"","cidade":"","municipio":"","distrito":"","estado":"","cep":""}
    
    # MELHORIA: Validar coordenadas antes
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        logger.warning(f"⚠️  Coordenadas fora do intervalo válido: {lat}, {lon}")
        return res
    
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&addressdetails=1"
        r = session.get(url, headers={"User-Agent": "GerenciadorRotasUniversais/7.0"}, timeout=4)
        if r.status_code == 200:
            a = r.json().get("address", {})
            res["logradouro"] = a.get("road", a.get("pedestrian", ""))
            res["bairro"] = a.get("neighbourhood", a.get("suburb", a.get("city_district", "")))
            res["cidade"] = a.get("city", a.get("town", a.get("municipality", "")))
            res["municipio"] = a.get("municipality", res["cidade"])
            res["distrito"] = a.get("city_district", a.get("suburb", ""))
            res["estado"] = a.get("state", "").upper()
            res["cep"] = a.get("postcode", "")
            logger.debug(f"✅ Reverse geocoding sucesso: {res['cidade']}, {res['estado']}")
    except Exception as e:
        logger.warning(f"⚠️  Reverse geocoding falhou: {e}")
    
    return res

# --- PROVEDORES CARTOGRÁFICOS ---
def API_ArcGIS(query: str) -> Optional[Dict]:
    """Geocodificação via Esri ArcGIS"""
    try:
        url = (f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/"
               f"findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}"
               f"&maxLocations=1&sourceCountry=BRA&outFields=*")
        r = session.get(url, timeout=4).json()
        if r.get('candidates'):
            c = r['candidates'][0]
            attr = c.get('attributes', {})
            logger.debug(f"✅ ArcGIS: {query} → {attr.get('City')}")
            return {
                "lat": float(c['location']['y']), "lon": float(c['location']['x']),
                "fonte": "ARCGIS", "score_base": 30,
                "cidade": attr.get('City','').upper(),
                "estado": attr.get('RegionAbbr','').upper(),
                "bairro": attr.get('Neighborhood','').upper()
            }
    except Exception as e:
        logger.debug(f"⚠️  ArcGIS falhou: {e}")
    return None

def API_Nominatim(query: str) -> Optional[Dict]:
    """Geocodificação via OpenStreetMap Nominatim"""
    try:
        url = (f"https://nominatim.openstreetmap.org/search?format=json"
               f"&q={requests.utils.quote(query)}&limit=1&addressdetails=1&countrycodes=br")
        r = session.get(url, headers={"User-Agent": "GerenciadorRotasUniversais/7.0"}, timeout=4).json()
        if r:
            a = r[0]
            addr = a.get("address", {})
            logger.debug(f"✅ Nominatim: {query} → {addr.get('city')}")
            return {
                "lat": float(a['lat']), "lon": float(a['lon']),
                "fonte": "NOMINATIM", "score_base": 25,
                "cidade": addr.get('city', addr.get('town','')).upper(),
                "estado": addr.get('state','').upper(),
                "bairro": addr.get('neighbourhood', addr.get('suburb','')).upper()
            }
    except Exception as e:
        logger.debug(f"⚠️  Nominatim falhou: {e}")
    return None

def API_Photon(query: str) -> Optional[Dict]:
    """Geocodificação via Photon/Komoot"""
    try:
        url = f"https://photon.komoot.io/api/?q={requests.utils.quote(query)}&limit=1&filter=countrycode:br"
        r = session.get(url, timeout=4).json()
        if r.get("features"):
            feat = r["features"][0]
            lon, lat = feat["geometry"]["coordinates"]
            props = feat.get("properties", {})
            logger.debug(f"✅ Photon: {query} → {props.get('city')}")
            return {
                "lat": lat, "lon": lon,
                "fonte": "PHOTON", "score_base": 20,
                "cidade": props.get("city","").upper(),
                "estado": props.get("state","").upper(),
                "bairro": props.get("district","").upper()
            }
    except Exception as e:
        logger.debug(f"⚠️  Photon falhou: {e}")
    return None

def API_Overpass_POIs(texto_norm: str) -> Optional[Dict]:
    """MELHORIA 2: Busca POIs via Overpass com timeout melhorado (5s)"""
    if texto_norm in cache_poi:
        logger.debug(f"💾 POI em cache: {texto_norm}")
        return cache_poi[texto_norm]
    
    try:
        texto_seguro = re.escape(texto_norm)
        query_osm = f"""
        [out:json][timeout:5];
        (
          node["name"~"{texto_seguro}",i]["amenity"];way["name"~"{texto_seguro}",i]["amenity"];
          node["name"~"{texto_seguro}",i]["building"];way["name"~"{texto_seguro}",i]["building"];
          node["name"~"{texto_seguro}",i]["healthcare"];way["name"~"{texto_seguro}",i]["healthcare"];
          node["name"~"{texto_seguro}",i]["education"];way["name"~"{texto_seguro}",i]["education"];
        );
        out center;
        """
        r = session.post("https://overpass-api.de/api/interpreter", data={"data": query_osm}, timeout=5)
        if r.status_code == 200:
            elems = r.json().get("elements", [])
            if elems:
                e = elems[0]
                lat = e.get("lat", e.get("center", {}).get("lat", 0.0))
                lon = e.get("lon", e.get("center", {}).get("lon", 0.0))
                tags = e.get("tags", {})
                resultado = {
                    "lat": lat, "lon": lon, "fonte": "OVERPASS", "score_base": 35,
                    "cidade": tags.get("addr:city","").upper(),
                    "estado": tags.get("addr:state","").upper(),
                    "bairro": tags.get("addr:suburb","").upper()
                }
                cache_poi.set(texto_norm, resultado, expire=2592000)
                logger.debug(f"✅ Overpass POI encontrado: {texto_norm}")
                return resultado
    except Exception as e:
        logger.debug(f"⚠️  Overpass falhou: {e}")
    
    cache_poi.set(texto_norm, None, expire=86400)
    return None

# MELHORIA 10: Geocodificadores primários executados em paralelo
def geocodificar_paralelo(query: str) -> List[Dict]:
    """Dispara ArcGIS + Nominatim + Photon ao mesmo tempo; retorna todos os resultados válidos"""
    candidatos = []
    provedores = [API_ArcGIS, API_Nominatim, API_Photon]
    
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futuros = {pool.submit(fn, query): fn.__name__ for fn in provedores}
            for fut in as_completed(futuros):
                res = fut.result()
                if res:
                    candidatos.append(res)
        logger.info(f"🌐 Geocodificação paralela: {len(candidatos)} resultado(s) obtido(s)")
    except Exception as e:
        logger.error(f"❌ Erro em geocodificação paralela: {e}")
    
    return candidatos

def processar_consenso_e_pontuacao_centesimal(candidatos: List[Dict], texto_cru: str) -> Optional[Tuple]:
    """Processa consenso espacial e calcula score final"""
    if not candidatos:
        return None

    for c1 in candidatos:
        score_centesimal = c1["score_base"]
        consenso_espacial = 0
        
        for c2 in candidatos:
            if c1["fonte"] != c2["fonte"]:
                dist = calcular_distancia_vincenty(c1["lat"], c1["lon"], c2["lat"], c2["lon"])
                if dist <= 10.0:
                    consenso_espacial += 1
                if c1["cidade"] and c1["cidade"] == c2["cidade"]:
                    score_centesimal += 20
                if c1["estado"] and c1["estado"] == c2["estado"]:
                    score_centesimal += 15
                if c1["bairro"] and c1["bairro"] == c2["bairro"]:
                    score_centesimal += 10
        
        c1["score_final"] = score_centesimal + (consenso_espacial * 25)

    candidatos.sort(key=lambda x: x["score_final"], reverse=True)
    vencedor = candidatos[0]

    # MELHORIA 3: Reverse geocoding só se score insatisfatório
    m = {"logradouro":"","bairro":"","cidade":"","municipio":"","distrito":"","estado":"","cep":""}
    if vencedor["score_final"] < 85:
        m = executar_reverse_geocoding_enrichment(vencedor["lat"], vencedor["lon"])
        if m["cep"]:
            vencedor["score_final"] += 10

    score_limitado = min(int(vencedor["score_final"]), 100)
    confianca = "BAIXA"
    if score_limitado >= 85:
        confianca = "ALTISSIMA"
    elif score_limitado >= 75:
        confianca = "ALTA"
    elif score_limitado >= 60:
        confianca = "MEDIA"

    rua_f = m["logradouro"] if m["logradouro"] else texto_cru.upper()
    cidade_f = m["cidade"] or vencedor.get("cidade","")
    estado_f = m["estado"] or vencedor.get("estado","")
    bairro_f = m["bairro"] or vencedor.get("bairro","")
    endereco_f = ", ".join([c for c in [rua_f, bairro_f, cidade_f, estado_f] if c.strip()]) + ", BRASIL"

    logger.info(f"✅ Consenso: {vencedor['fonte']} score={score_limitado} confiança={confianca}")
    
    return (vencedor["lat"], vencedor["lon"], endereco_f, confianca, score_limitado,
            m["distrito"], m["municipio"] or cidade_f)

# ==============================================================================
# 🎚️ PIPELINE SEQUENCIAL EM CASCATA INTELIGENTE
# ==============================================================================
def obter_coordenadas_e_endereco_oficial(localidade: str) -> Tuple:
    """Pipeline cascata: CEP → POI → Geocodificadores → Fallback geodésico"""
    texto_cru = str(localidade).strip()
    if not texto_cru or texto_cru.lower() == 'nan':
        logger.warning("⚠️  Localidade vazia ou 'nan'")
        return 0.0, 0.0, "", "BAIXA", 0, "", ""

    # MELHORIA 6: chave de cache normalizada
    cache_key = normalizar_endereco_universal(texto_cru)

    if cache_key in cache_geo:
        c = cache_geo[cache_key]
        logger.info(f"💾 Geocodificação em cache: {cache_key}")
        return c["lat"], c["lon"], c["endereco"], c["confianca"], c["score_num"], c["distrito"], c["municipio"]

    # Estratégia 1: Detector de CEP
    cep_estrito = detectar_cep_parcial(texto_cru)
    if cep_estrito:
        logr, bair, loca, uf = camada_postal_redundante(cep_estrito)
        if loca:
            addr_c = f"{logr}, {bair}, {loca}, {uf}, CEP {cep_estrito}, BRASIL"
            res_arc = API_ArcGIS(addr_c)
            lat, lon = (res_arc["lat"], res_arc["lon"]) if res_arc else (0.0, 0.0)
            logger.info(f"✅ CEP resolvido: {cep_estrito} → {loca}, {uf}")
            return lat, lon, addr_c, "ALTISSIMA", 100, bair, loca

    # Estratégia 2: Detecção de POI
    texto_expandido = expandir_contexto_incompleto(texto_cru)
    texto_norm = normalizar_endereco_universal(texto_cru)
    candidatos_validos = []

    if parece_poi(texto_norm):
        res_poi = API_Overpass_POIs(texto_norm)
        if res_poi:
            candidatos_validos.append(res_poi)
            res_final = processar_consenso_e_pontuacao_centesimal(candidatos_validos, texto_cru)
            if res_final:
                cache_geo.set(cache_key, {
                    "lat": res_final[0], "lon": res_final[1], "endereco": res_final[2],
                    "confianca": res_final[3], "score_num": res_final[4],
                    "distrito": res_final[5], "municipio": res_final[6]
                }, expire=2592000)
                logger.info(f"✅ POI resolvido: {texto_norm}")
                return res_final

    # Estratégia 3: Geocodificação paralela (MELHORIA 10)
    candidatos_validos = geocodificar_paralelo(texto_expandido)

    res_final = processar_consenso_e_pontuacao_centesimal(candidatos_validos, texto_cru)
    if res_final:
        cache_geo.set(cache_key, {
            "lat": res_final[0], "lon": res_final[1], "endereco": res_final[2],
            "confianca": res_final[3], "score_num": res_final[4],
            "distrito": res_final[5], "municipio": res_final[6]
        }, expire=2592000)
        logger.info(f"✅ Geocodificação paralela resolvida: {texto_cru}")
        return res_final

    logger.warning(f"⚠️  Falha em geocodificação, retornando fallback: {texto_cru}")
    return 0.0, 0.0, texto_expandido, "BAIXA", 0, "", ""

# ==============================================================================
# 🚀 MOTOR DE ROTEAMENTO
# ==============================================================================
def rota_osrm(lat_o: float, lon_o: float, lat_d: float, lon_d: float) -> Optional[Tuple]:
    """Cálculo de rota via OSRM (Open Source Routing Machine)"""
    try:
        url = (f"https://router.project-osrm.org/route/v1/driving/"
               f"{lon_o},{lat_o};{lon_d},{lat_d}?overview=false")
        r = session.get(url, timeout=5).json()
        if r.get("routes"):
            r_data = r["routes"][0]
            km = round(r_data["distance"] / 1000, 2)
            minutos = round(r_data["duration"] / 60)
            tempo_txt = f"{minutos} min" if minutos < 60 else f"{minutos//60} h {minutos%60} min"
            logger.info(f"✅ OSRM: {km} km, {tempo_txt}")
            return km, tempo_txt, "OSRM", 95
    except Exception as e:
        logger.warning(f"⚠️  OSRM falhou: {e}")
    return None

# MELHORIA 8: Fallback para OpenRouteService se OSRM falhar
def rota_openrouteservice(lat_o: float, lon_o: float, lat_d: float, lon_d: float) -> Optional[Tuple]:
    """Cálculo de rota via OpenRouteService (fallback)"""
    try:
        url = (f"https://api.openrouteservice.org/v2/directions/driving-car"
               f"?api_key={ORS_API_KEY}"
               f"&start={lon_o},{lat_o}&end={lon_d},{lat_d}")
        r = session.get(url, timeout=5).json()
        feats = r.get("features", [])
        if feats:
            seg = feats[0]["properties"]["segments"][0]
            km = round(seg["distance"] / 1000, 2)
            mins = round(seg["duration"] / 60)
            tempo_txt = f"{mins} min" if mins < 60 else f"{mins//60} h {mins%60} min"
            logger.info(f"✅ ORS: {km} km, {tempo_txt}")
            return km, tempo_txt, "ORS", 90
    except Exception as e:
        logger.warning(f"⚠️  OpenRouteService falhou: {e}")
    return None

def obter_fator_desvio_rodoviario(linha_reta: float) -> float:
    """Calcula fator de desvio rodoviário baseado em distância em linha reta"""
    if linha_reta < 5.0:
        return 1.45
    if linha_reta < 20.0:
        return 1.35
    if linha_reta < 100.0:
        return 1.25
    if linha_reta < 500.0:
        return 1.18
    return 1.12

def calcular_pipeline_logistico(origem: str, destino: str) -> Tuple:
    """Pipeline completo: geocodificação + roteamento + cache"""
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()

    # MELHORIA 5: Chave simétrica A→B igual a B→A
    chave_rota_cache = "ROTA_" + "_".join(sorted([origem_clean, destino_clean]))
    if chave_rota_cache in cache_rotas:
        logger.debug(f"💾 Rota em cache: {chave_rota_cache}")
        return cache_rotas[chave_rota_cache]

    logger.info(f"🔄 Processando rota: {origem_clean} → {destino_clean}")
    
    lat_o, lon_o, o_oficial, conf_o, score_o, dist_o, mun_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    lat_d, lon_d, d_oficial, conf_d, score_d, dist_d, mun_d = obter_coordenadas_e_endereco_oficial(destino_clean)

    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)

    usar_coords = lat_o != 0.0 and lat_d != 0.0
    if usar_coords and dist_linha_reta > 150.0:
        siglas = re.findall(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM|PA|MT)\b',
                            (origem_clean + " " + destino_clean).upper())
        if len(set(siglas)) <= 1:
            usar_coords = False
            logger.debug("⚠️  Mesma UF, usando fallback geodésico")

    link_m = (f"https://www.google.com/maps/dir/?api=1"
              f"&origin={requests.utils.quote(o_oficial)}"
              f"&destination={requests.utils.quote(d_oficial)}&travelmode=driving")

    if usar_coords:
        res_rota = rota_osrm(lat_o, lon_o, lat_d, lon_d)
        # MELHORIA 8: fallback ORS se OSRM falhar
        if not res_rota:
            logger.info("🔄 OSRM falhou, tentando OpenRouteService...")
            res_rota = rota_openrouteservice(lat_o, lon_o, lat_d, lon_d)
        
        if res_rota:
            retorno = (res_rota[0], res_rota[1], link_m, "Não", dist_linha_reta,
                       res_rota[2], res_rota[3],
                       conf_o, score_o, dist_o, mun_o,
                       conf_d, score_d, dist_d, mun_d)
            cache_rotas.set(chave_rota_cache, retorno, expire=2592000)
            logger.info(f"✅ Rota cacheada: {chave_rota_cache}")
            return retorno

    # Fallback: Cálculo geodésico
    km_terrestre = round(dist_linha_reta * obter_fator_desvio_rodoviario(dist_linha_reta), 2)
    v_comercial = 45.0 if km_terrestre < 50.0 else 65.0
    minutos_est = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0 else 0
    tempo_geo = f"{minutos_est} min" if minutos_est < 60 else f"{minutos_est//60} h {minutos_est%60} min"

    logger.warning(f"⚠️  Usando fallback geodésico: {km_terrestre} km")
    
    retorno = (km_terrestre, tempo_geo, link_m, "Não", dist_linha_reta,
               "Geodésico Adaptativo", 70,
               conf_o, score_o, dist_o, mun_o,
               conf_d, score_d, dist_d, mun_d)
    cache_rotas.set(chave_rota_cache, retorno, expire=2592000)
    return retorno

def embrulhar_task_paralela(item: Tuple) -> Tuple:
    """Wrapper para execução paralela de cálculo de rotas"""
    idx, orig, dest = item
    return idx, calcular_pipeline_logistico(orig, dest)

# ==============================================================================
# 🚗 INTERFACE VISUAL NO STREAMLIT
# ==============================================================================
st.title("🚗 Gerenciador de Rotas Inteligentes")
st.write("Insira uma planilha Excel (.xlsx) contendo as colunas **Origem** e **Destino**.")

arquivo_carregado = st.file_uploader("Selecionar Arquivo Excel", type=["xlsx"])

if arquivo_carregado is not None:
    try:
        df = pd.read_excel(arquivo_carregado)
        logger.info(f"📊 Planilha carregada: {len(df)} linhas")

        if 'Origem' not in df.columns or 'Destino' not in df.columns:
            st.error("❌ Erro de Validação: Certifique-se de que a planilha possui as colunas obrigatórias 'Origem' e 'Destino'.")
            logger.error("Colunas obrigatórias não encontradas")
        else:
            st.success("✅ Tabela de dados detectada com sucesso! Pronto para processar.")

            if st.button("🚀 Iniciar Processamento em Lote"):
                novas_colunas = [
                    'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta',
                    'Fonte da Rota', 'Score da Rota', 'Confianca Origem', 'Score Num Origem',
                    'Distrito Origem', 'Municipio Origem', 'Confianca Destino', 'Score Num Destino',
                    'Distrito Destino', 'Municipio Destino'
                ]
                for col in novas_colunas:
                    df[col] = None

                total_linhas = len(df)
                barra_progresso = st.progress(0)
                container_status = st.empty()

                tarefas = []
                for index, linha in df.iterrows():
                    origem = str(linha['Origem']).strip()
                    destino = str(linha['Destino']).strip()
                    if origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan':
                        tarefas.append((index, origem, destino))

                logger.info(f"🎯 Total de tarefas: {len(tarefas)}")

                # MELHORIA 4: Reutiliza executor global em vez de criar novo
                lote_executor = st.session_state["executor_global"]
                resultados_mapeados = {}
                futuros = {lote_executor.submit(embrulhar_task_paralela, t): t for t in tarefas}

                concluidos = 0
                for f in as_completed(futuros):
                    try:
                        idx, res_pipeline = f.result()
                        resultados_mapeados[idx] = res_pipeline
                        concluidos += 1
                        container_status.text(f"🚀 Processamento: {concluidos} de {len(tarefas)} rotas calculadas...")
                        barra_progresso.progress(concluidos / len(tarefas))
                    except Exception as e:
                        logger.error(f"❌ Erro ao processar tarefa {f}: {e}")
                        st.error(f"Erro ao processar linha: {e}")

                # Preencher resultados
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

                container_status.empty()
                barra_progresso.empty()
                st.success("✨ Processamento em lote concluído com sucesso!")
                logger.info("✅ Processamento concluído com sucesso")

                # Reordenar colunas
                ordem_finais = [
                    'Origem', 'Destino', 'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta',
                    'Fonte da Rota', 'Score da Rota',
                    'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem',
                    'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino'
                ]
                df = df.reindex(columns=ordem_finais)

                # Gerar arquivo de saída
                output_buffer = io.BytesIO()
                with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False)
                output_buffer.seek(0)

                st.write("---")
                st.balloons()
                st.download_button(
                    label="📥 Baixar Planilha Logística Processada",
                    data=output_buffer.getvalue(),
                    file_name="planilha_rotas_calculada.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

    except Exception as e:
        st.error(f"❌ Erro ao processar arquivo: {e}")
        logger.error(f"❌ Erro crítico: {e}", exc_info=True)
