import streamlit as st
import pandas as pd
import numpy as np
import pydeck as pdk
import requests
import time
import math
import io
import re
import os
import pickle
import collections
import hashlib
from unidecode import unidecode
from rapidfuzz import process, fuzz
from diskcache import Cache
from sklearn.cluster import DBSCAN
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# CONFIGURAÇÃO CORPORATIVA DE UI/UX E AMBIENTE
# ==============================================================================
st.set_page_config(page_title="Motor Nacional de Roteirização", page_icon="🗺️", layout="wide")

TOMTOM_API_KEY = ""  # Insira a chave da TomTom Logistics (Opcional, possui Graceful Degradation se vazia)

# ==============================================================================
# 🧠 PERSISTÊNCIA B2B, LRU CACHE E HIGIENIZAÇÃO DE AMBIENTE
# ==============================================================================
cache_classificacao = Cache("./cache_classificacao")
cache_fuzzy = Cache("./cache_fuzzy")
cache_geo = Cache("./cache_geo")
cache_rotas = Cache("./cache_rotas")
cache_poi = Cache("./cache_poi")
cache_cep = Cache("./cache_cep")
cache_google = Cache("./cache_google")
cache_reverse = Cache("./cache_reverse")
cache_base_local = Cache("./cache_base_local")
cache_aprendizado = Cache("./cache_aprendizado")
cache_aprendizado_auto = Cache("./cache_aprendizado_auto")
cache_api_health = Cache("./cache_api_health")
cache_historico_lotes = Cache("./cache_historico_lotes")

for c in [cache_classificacao, cache_fuzzy, cache_geo, cache_rotas, cache_poi, cache_cep, cache_google, cache_reverse, cache_base_local, cache_aprendizado, cache_aprendizado_auto, cache_api_health, cache_historico_lotes]:
    c.cull()

def realizar_manutencao_logs_google():
    diretorio_logs = "logs_google"
    os.makedirs(diretorio_logs, exist_ok=True)
    limite = time.time() - (30 * 86400)
    for arquivo in os.listdir(diretorio_logs):
        caminho = os.path.join(diretorio_logs, arquivo)
        if os.path.isfile(caminho) and os.path.getmtime(caminho) < limite:
            os.remove(caminho)

realizar_manutencao_logs_google()

session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter); session.mount("http://", adapter)

# ==============================================================================
# 🎛️ INFRAESTRUTURA DE CONCORRÊNCIA E FILAS
# ==============================================================================
if "executor_global" not in st.session_state: st.session_state["executor_global"] = ThreadPoolExecutor(max_workers=12)
if "fila_nominatim" not in st.session_state: st.session_state["fila_nominatim"] = ThreadPoolExecutor(max_workers=1)
if "executor_apis" not in st.session_state: st.session_state["executor_apis"] = ThreadPoolExecutor(max_workers=24)

# ==============================================================================
# 🎛️ DADOS GLOBAIS THREAD-SAFE, HUB B2B E EXPANSÃO SEMÂNTICA
# ==============================================================================
CACHE_IBGE_PATH = "municipios_ibge.pkl"

BASE_POIS_LOGISTICOS = {
    "CD MAGAZINE LUIZA CAXIAS": {"lat": -22.7853, "lon": -43.3121, "endereco": "Centro de Distribuição Magazine Luiza, Duque de Caxias, RJ, BRASIL", "municipio": "DUQUE DE CAXIAS", "uf": "RJ"},
    "CD MERCADO LIVRE CAJAMAR": {"lat": -23.3541, "lon": -46.8852, "endereco": "Centro de Distribuição Mercado Livre, Cajamar, SP, BRASIL", "municipio": "CAJAMAR", "uf": "SP"},
    "CD AMAZON CAJAMAR": {"lat": -23.3600, "lon": -46.8900, "endereco": "Centro de Distribuição Amazon, Cajamar, SP, BRASIL", "municipio": "CAJAMAR", "uf": "SP"}
}

SINONIMOS_SEMANTICOS = {
    "UNB": "UNIVERSIDADE DE BRASILIA", "CATOLICA": "UNIVERSIDADE CATOLICA",
    "CD": "CENTRO DE DISTRIBUICAO", "HUB": "CENTRO LOGISTICO", 
    "FILIAL": "BASE OPERACIONAL", "TECA": "TERMINAL DE CARGAS", 
    "AEROPORTO": "TERMINAL DE CARGAS AEROPORTO"
}

POI_KEYWORDS = ["AEROPORTO", "HOSPITAL", "UNIVERSIDADE", "SHOPPING", "RODOVIARIA", "ESTADIO", "MINISTERIO", "AGENCIA", "BANCO", "CLINICA", "CENTRO DE DISTRIBUICAO", "TERMINAL"]
BOUNDING_BOXES_UF = {
    "DF": {"lat_min": -16.05, "lat_max": -15.50, "lon_min": -48.30, "lon_max": -47.30},
    "SP": {"lat_min": -25.50, "lat_max": -19.50, "lon_min": -53.50, "lon_max": -44.00},
    "GO": {"lat_min": -19.50, "lat_max": -12.40, "lon_min": -53.30, "lon_max": -45.90}
}

@st.cache_data
def carregar_dados_ibge():
    if os.path.exists(CACHE_IBGE_PATH) and time.time() - os.path.getmtime(CACHE_IBGE_PATH) <= (30 * 86400):
        try:
            with open(CACHE_IBGE_PATH, "rb") as f:
                d = pickle.load(f)
                return d.get("municipios", {}), d.get("estados", {}), d.get("distritos", {}), list(d.get("municipios", {}).keys()) + list(d.get("distritos", {}).keys())
        except: pass
    base_mun, base_est, base_dist = {}, {}, {}
    try:
        r_est = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/estados", timeout=8)
        if r_est.status_code == 200:
            for est in r_est.json(): base_est[est["sigla"]] = unidecode(est["nome"]).upper()
        r_mun = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/municipios", timeout=12)
        if r_mun.status_code == 200:
            for mun in r_mun.json():
                nome_norm = unidecode(mun["nome"]).upper().strip()
                if nome_norm not in base_mun: base_mun[nome_norm] = []
                base_mun[nome_norm].append({"uf": mun["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper(), "municipio": nome_norm, "lat": mun.get("lat", 0.0), "lon": mun.get("lon", 0.0)})
        r_dist = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/distritos", timeout=12)
        if r_dist.status_code == 200:
            for dist in r_dist.json():
                nome_dist = unidecode(dist["nome"]).upper().strip()
                if nome_dist not in base_dist: base_dist[nome_dist] = []
                base_dist[nome_dist].append({"uf": dist["municipio"]["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper(), "municipio": unidecode(dist["municipio"]["nome"]).upper().strip(), "lat": dist.get("lat", 0.0), "lon": dist.get("lon", 0.0)})
        with open(CACHE_IBGE_PATH, "wb") as f: pickle.dump({"municipios": base_mun, "estados": base_est, "distritos": base_dist}, f)
    except: pass
    return base_mun, base_est, base_dist, list(base_mun.keys()) + list(base_dist.keys())

IBGE_MUNICIPIOS, IBGE_ESTADOS, IBGE_DISTRITOS, LISTA_TOPONIMOS = carregar_dados_ibge()
LISTA_CONTEXTO_FUZZY = list(set([f"{k} {v['uf']}" for k, lst in IBGE_MUNICIPIOS.items() for v in lst] + [f"{k} {v['uf']}" for k, lst in IBGE_DISTRITOS.items() for v in lst]))

# ==============================================================================
# 🧹 ENGINE DE RESOLUÇÃO UNIVERSAL E ENDEREÇAMENTO CANÔNICO
# ==============================================================================
class ParserGeograficoBR:
    @staticmethod
    def extrair_componentes(texto):
        comp = {"cep": "", "numero": "", "complemento": "", "resto": texto}
        if cep_m := re.search(r'\b\d{5}-?\d{3}\b', comp["resto"]):
            comp["cep"] = cep_m.group(0).replace("-", "")
            comp["resto"] = comp["resto"].replace(cep_m.group(0), "").strip(" ,-")
        if num_m := re.search(r'\b(?:N|NO|NUMERO|NUM)?\s*(\d{1,5})\b', comp["resto"], re.IGNORECASE): comp["numero"] = num_m.group(1)
        if c_m := re.search(r'\b(BLOCO|BL|APTO|APT|APARTAMENTO|SALA|CJ|CASA|LOJA|PAVIMENTO)\s*([A-Z0-9]+)\b', comp["resto"], re.IGNORECASE): comp["complemento"] = f"{c_m.group(1)} {c_m.group(2)}"
        return comp

class MotorEnderecoCanônico:
    def __init__(self):
        self.rural_keys = ["FAZENDA", "SITIO", "ASSENTAMENTO", "CHACARA", "GLEBA", "NUCLEO RURAL"]
        self.bairro_keys = ["BAIRRO", "VILA", "JARDIM", "PARQUE", "SETOR", "ASA SUL", "ASA NORTE", "LAGO SUL", "LAGO NORTE"]
        self.condo_keys = [r"\bCONDOMINIO\b", r"\bCOND\.", r"\bRESIDENCIAL\b", r"\bRES\.", r"\bLOTEAMENTO\b"]
        self.via_keys = ["RUA", "AVENIDA", "TRAVESSA", "ALAMEDA", "RODOVIA", "ESTRADA", "QUADRA", "SQN", "SQS", "SHIS", "SHIN", "SCRN", "SCS", "SRTVN", "CLS", "CLN", "QNL", "QNM", "QNN", "QNG", "QNJ", "QNK", "QI", "QE", "QC", "QR", "QS", "QSC"]
        self.mapa_siglas_df = {"QNL": "TAGUATINGA", "QNM": "CEILANDIA", "QS": "SAMAMBAIA", "SQN": "PLANO PILOTO", "SQS": "PLANO PILOTO", "SHIS": "LAGO SUL", "SHIN": "LAGO NORTE", "QE": "GUARA", "QI": "GUARA"}

    def normalizar(self, texto):
        if not texto or pd.isna(texto): return ""
        t_raw = str(texto).strip()
        if t_raw.upper() in cache_aprendizado and isinstance(cache_aprendizado[t_raw.upper()], str): t_raw = cache_aprendizado[t_raw.upper()]
        t = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', t_raw)
        t = unidecode(t).upper()
        t = re.sub(r'\b0+(\d{1,4})\b', r'\1', t) 
        
        def padronizar_rodovia(m): return f"{m.group(1)}-{m.group(2).zfill(3)}{' KM ' + m.group(3) if m.group(3) else ''}"
        t = re.sub(r'\b(BR|AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\s*[-]?\s*(\d+)(?:\s*(?:KM|QUILOMETRO)\s*(\d+))?\b', padronizar_rodovia, t)
        
        abreviacoes = {r'\bAV\b': 'AVENIDA', r'\bR\b': 'RUA', r'\bQD\b': 'QUADRA', r'\bLT\b': 'LOTE', r'\bCJ\b': 'CONJUNTO', r'\bBL\b': 'BLOCO', r'\bAPT\b': 'APARTAMENTO', r'\bST\b': 'SETOR', r'\bCH\b': 'CHACARA', r'\bROD\b': 'RODOVIA', r'\bKM\b': 'QUILOMETRO'}
        for padrao, exp in abreviacoes.items(): t = re.sub(padrao, exp, t)
        for k, v in SINONIMOS_SEMANTICOS.items(): t = re.sub(rf'\b{k}\b', v, t)
        return re.sub(r'\s+', ' ', t).strip()

    def classificar_entrada(self, texto_norm):
        if texto_norm in cache_classificacao: return cache_classificacao[texto_norm]
        tipo = "LOGRADOURO"
        if re.search(r'\b\d{5}-?\d{3}\b', texto_norm): tipo = "CEP"
        elif any(re.search(p, texto_norm) for p in self.condo_keys): tipo = "CONDOMINIO"
        elif any(k in texto_norm for k in POI_KEYWORDS): tipo = "POI"
        elif any(k in texto_norm for k in self.rural_keys): tipo = "RURAL"
        elif any(k in texto_norm for k in self.via_keys) and bool(re.search(r'\d+', texto_norm)): tipo = "ENDERECO_COMPLETO"
        elif any(k in texto_norm for k in self.bairro_keys): tipo = "BAIRRO"
        elif texto_norm in IBGE_MUNICIPIOS: tipo = "MUNICIPIO"
        elif texto_norm in IBGE_DISTRITOS: tipo = "DISTRITO"
        cache_classificacao.set(texto_norm, tipo, expire=2592000)
        return tipo

    def resolver_contexto_administrativo(self, texto_norm):
        tokens = texto_norm.split()
        uf_exp = next((re.sub(r'[^A-Z]', '', t) for t in reversed(tokens) if re.sub(r'[^A-Z]', '', t) in IBGE_ESTADOS), None)
        if not uf_exp or uf_exp == "DF":
            for t in tokens:
                sl = re.sub(r'[^A-Z]', '', t)
                if sl in self.mapa_siglas_df and len(sl) >= 2: return {"uf": "DF", "municipio": "BRASILIA", "distrito": self.mapa_siglas_df[sl]}
        for i in range(len(tokens)):
            for j in range(i + 1, len(tokens) + 1):
                chunk = " ".join(tokens[i:j])
                if chunk in IBGE_MUNICIPIOS: return {"uf": uf_exp if uf_exp else IBGE_MUNICIPIOS[chunk][0]["uf"], "municipio": chunk, "distrito": ""}
                if chunk in IBGE_DISTRITOS: return {"uf": uf_exp if uf_exp else IBGE_DISTRITOS[chunk][0]["uf"], "municipio": IBGE_DISTRITOS[chunk][0]["municipio"], "distrito": chunk}
        return {"uf": uf_exp if uf_exp else "", "municipio": "", "distrito": ""}

    def construir_endereco_canonico(self, texto_cru):
        texto_norm = self.normalizar(texto_cru)
        parsed = ParserGeograficoBR.extrair_componentes(texto_norm)
        if parsed["cep"]:
            logr, bair, loca, uf, lat, lon = cascata_postal_tripla(parsed["cep"])
            if loca: return f"{logr}, {bair}, {loca}, {IBGE_ESTADOS.get(uf, uf)}, BRASIL", "CEP", parsed["cep"], 0.0, 0.0
        
        tipo = self.classificar_entrada(texto_norm)
        ctx = self.resolver_contexto_administrativo(texto_norm)
        comps = [texto_norm]
        if ctx["distrito"] and ctx["distrito"] not in texto_norm: comps.append(ctx["distrito"])
        if ctx["municipio"] and ctx["municipio"] not in texto_norm: comps.append(ctx["municipio"])
        if ctx["uf"] and IBGE_ESTADOS.get(ctx["uf"], ctx["uf"]) not in texto_norm: comps.append(IBGE_ESTADOS.get(ctx["uf"], ctx["uf"]))
        if "BRASIL" not in texto_norm: comps.append("BRASIL")
        return re.sub(r',\s*,', ',', ", ".join(comps)).strip(), tipo, "", 0.0, 0.0

semantica = MotorEnderecoCanônico()

# ==============================================================================
# 🧮 AUDITORIA PRÉ-GEOCODING & LÓGICA GEODÉSICA
# ==============================================================================
def auditoria_pre_geocoding(texto_cru, contexto, tipo_entrada):
    if len(texto_cru) < 4: return "INSUFICIENTE"
    if tipo_entrada in ["BAIRRO", "RURAL"] and not contexto.get("municipio"): return "INSUFICIENTE"
    if tipo_entrada in ["ENDERECO_COMPLETO", "LOGRADOURO", "CONDOMINIO"] and not contexto.get("municipio") and not contexto.get("uf"): return "PARCIAL"
    return "COMPLETO"

def validar_coordenada_brasil(lat, lon):
    try:
        lat_f, lon_f = float(lat), float(lon)
        if (-35.0 <= lat_f <= 6.0) and (-75.0 <= lon_f <= -28.0): return True, lat_f, lon_f
        if (-35.0 <= lon_f <= 6.0) and (-75.0 <= lat_f <= -28.0): return True, lon_f, lat_f 
        return False, lat_f, lon_f
    except: return False, 0.0, 0.0

def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0 or (lat1 == lat2 and lon1 == lon2): return 0.0
    try:
        a, b, f = 6378137.0, 6356752.314245, 1 / 298.257223563
        L = math.radians(lon2 - lon1); U1, U2 = math.atan((1 - f) * math.tan(math.radians(lat1))), math.atan((1 - f) * math.tan(math.radians(lat2)))
        sinU1, cosU1, sinU2, cosU2 = math.sin(U1), math.cos(U1), math.sin(U2), math.cos(U2)
        lam = L
        for _ in range(100):
            sinLam, cosLam = math.sin(lam), math.cos(lam)
            sinSigma = math.sqrt((cosU2 * sinLam) ** 2 + (cosU1 * sinU2 - sinU1 * cosU2 * cosLam) ** 2)
            if sinSigma == 0: return 0.0
            cosSigma = sinU1 * sinU2 + cosU1 * cosU2 * cosLam
            sigma = math.atan2(sinSigma, cosSigma); sinAlpha = cosU1 * cosU2 * sinLam / sinSigma
            cosSqAlpha = 1 - sinAlpha ** 2; cos2SigmaM = cosSigma - 2 * sinU1 * sinU2 / cosSqAlpha if cosSqAlpha != 0 else 0
            C = f / 16 * cosSqAlpha * (4 + f * (4 - 3 * cosSqAlpha)); lambdaPrev = lam
            lam = L + (1 - f) * C * sinAlpha * (sigma + f * sinAlpha * (cos2SigmaM + C * cosSigma * (-1 + 2 * cos2SigmaM ** 2)))
            if abs(lam - lambdaPrev) < 1e-12: break
        uSq = cosSqAlpha * (a ** 2 - b ** 2) / (b ** 2)
        A = 1 + uSq / 16384 * (4096 + uSq * (-768 + uSq * (320 - 175 * uSq))); B = uSq / 1024 * (256 + uSq * (-128 + uSq * (74 - 47 * uSq)))
        deltaSigma = B * sinSigma * (cos2SigmaM + B / 4 * (cosSigma * (-1 + 2 * cos2SigmaM ** 2) - B / 6 * cos2SigmaM * (-3 + 4 * sinSigma ** 2) * (-3 + 4 * cos2SigmaM ** 2)))
        return round((b * A * (sigma - deltaSigma)) / 1000, 2)
    except:
        dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        m_a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
        return round(6371.0 * 2 * math.atan2(math.sqrt(m_a), math.sqrt(1 - m_a)), 2)

def auditoria_geografica(km_rota, minutos_str, dist_linha_reta, lat_o, lon_o, lat_d, lon_d):
    for lat, lon, loc in [(lat_o, lon_o, "Origem"), (lat_d, lon_d, "Destino")]:
        if not (-75.0 <= lon <= -28.0) or not (-35.0 <= lat <= 6.0): return f"AUDITORIA: Coordenada {loc} fora do BR ({lat},{lon})"
    if km_rota and dist_linha_reta and km_rota > 0 and dist_linha_reta > 0:
        if km_rota < (dist_linha_reta * 0.9): return f"Violação Geodésica (Rota {km_rota}km < Linha Reta {dist_linha_reta}km)"
    return None

def cascata_postal_tripla(cep_limpo):
    if cep_limpo in cache_cep: return cache_cep[cep_limpo] if len(cache_cep[cep_limpo]) == 6 else (*cache_cep[cep_limpo], 0.0, 0.0)
    lat, lon = 0.0, 0.0
    for api in [f"https://brasilapi.com.br/api/cep/v2/{cep_limpo}", f"https://viacep.com.br/ws/{cep_limpo}/json/", f"https://opencep.com/v1/{cep_limpo}"]:
        try:
            r = session.get(api, timeout=4).json()
            if "city" in r or "localidade" in r:
                if "coordinates" in r.get("location", {}): lat, lon = float(r["location"]["coordinates"].get("latitude", 0)), float(r["location"]["coordinates"].get("longitude", 0))
                d = (r.get('street', r.get('logradouro', '')), r.get('neighborhood', r.get('bairro', '')), r.get('city', r.get('localidade', '')), r.get('state', r.get('uf', '')), lat, lon)
                cache_cep.set(cep_limpo, d, expire=2592000); return d
        except: pass
    return "", "", "", "", 0.0, 0.0

# ==============================================================================
# 🗺️ MÓDULOS DE TELEMETRIA E GEOCODIFICAÇÃO (APIs)
# ==============================================================================
def reportar_telemetria(fonte, sucesso, tempo):
    m = cache_api_health.get(fonte, {"hits": 0, "calls": 0, "falhas": 0, "tempo_total": 0.0})
    m["calls"] += 1; m["tempo_total"] += tempo
    if sucesso: m["hits"] += 1
    else: m["falhas"] += 1
    cache_api_health.set(fonte, m, expire=None)

def API_Google_Geocoding_Scraper(query):
    start_t = time.time()
    try:
        r = session.get(f"https://www.google.com/maps/search/{requests.utils.quote(query)}", headers={"User-Agent": "Mozilla/5.0"}, timeout=5, allow_redirects=True)
        match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.url) or re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.text)
        if match:
            reportar_telemetria("GOOGLE_MAPS", True, time.time() - start_t)
            return [{"lat": float(match.group(1)), "lon": float(match.group(2)), "fonte": "GOOGLE_MAPS", "score_base": 40}]
    except: pass
    reportar_telemetria("GOOGLE_MAPS", False, time.time() - start_t); return None

def API_TomTom(query):
    if not TOMTOM_API_KEY: return None
    start_t = time.time()
    try:
        r = session.get(f"https://api.tomtom.com/search/2/geocode/{requests.utils.quote(query)}.json?key={TOMTOM_API_KEY}&countrySet=BR&limit=5", timeout=4).json()
        if r.get("results"):
            reportar_telemetria("TOMTOM", True, time.time() - start_t)
            return [{"lat": float(res["position"]["lat"]), "lon": float(res["position"]["lon"]), "fonte": "TOMTOM", "score_base": 35, "cidade": res.get("address", {}).get("municipality", "").upper(), "estado": res.get("address", {}).get("countrySubdivision", "").upper(), "bairro": res.get("address", {}).get("neighbourhood", "").upper(), "logradouro": res.get("address", {}).get("streetName", "").upper(), "numero": str(res.get("address", {}).get("streetNumber", "")).upper(), "cep": res.get("address", {}).get("postalCode", "").replace("-", "")} for res in r["results"][:5]]
    except: pass
    reportar_telemetria("TOMTOM", False, time.time() - start_t); return None

def API_ArcGIS(query, ctx=None):
    start_t = time.time()
    try:
        url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&Address={requests.utils.quote(ctx.get('logradouro', ''))}&Neighborhood={requests.utils.quote(ctx.get('bairro', ''))}&City={requests.utils.quote(ctx.get('municipio', ''))}&Region={requests.utils.quote(ctx.get('uf', ''))}&Postal={requests.utils.quote(ctx.get('cep', ''))}&maxLocations=5&sourceCountry=BRA&outFields=*" if ctx and (ctx.get("logradouro") or ctx.get("municipio")) else f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=5&sourceCountry=BRA&outFields=*"
        if cands := session.get(url, timeout=4).json().get('candidates'):
            reportar_telemetria("ARCGIS", True, time.time() - start_t)
            return [{"lat": float(c['location']['y']), "lon": float(c['location']['x']), "fonte": "ARCGIS", "score_base": 30, "cidade": c.get('attributes', {}).get('City', '').upper(), "estado": c.get('attributes', {}).get('RegionAbbr', '').upper(), "bairro": c.get('attributes', {}).get('Neighborhood', '').upper(), "logradouro": c.get('attributes', {}).get('StName', c.get('attributes', {}).get('Address', '')).upper(), "numero": str(c.get('attributes', {}).get('AddNum', '')).upper(), "cep": c.get('attributes', {}).get('Postal', '')} for c in cands[:5]]
    except: pass
    reportar_telemetria("ARCGIS", False, time.time() - start_t); return None

def API_Nominatim(query, ctx=None):
    start_t = time.time()
    try:
        def _call():
            time.sleep(1.1)
            url = f"https://nominatim.openstreetmap.org/search?format=json&street={requests.utils.quote(ctx['logradouro'])}&city={requests.utils.quote(ctx['municipio'])}&state={requests.utils.quote(ctx.get('uf', ''))}&limit=5&addressdetails=1&countrycodes=br" if ctx and ctx.get("logradouro") and ctx.get("municipio") else f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query)}&limit=5&addressdetails=1&countrycodes=br"
            return session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
        if r := st.session_state["fila_nominatim"].submit(_call).result():
            reportar_telemetria("NOMINATIM", True, time.time() - start_t)
            return [{"lat": float(a['lat']), "lon": float(a['lon']), "fonte": "NOMINATIM", "score_base": 25, "cidade": a.get("address", {}).get('city', a.get("address", {}).get('town', '')).upper(), "estado": a.get("address", {}).get('state', '').upper(), "bairro": a.get("address", {}).get('neighbourhood', '').upper(), "logradouro": a.get("address", {}).get('road', '').upper(), "cep": a.get("address", {}).get('postcode', '').replace("-", "")} for a in r[:5]]
    except: pass
    reportar_telemetria("NOMINATIM", False, time.time() - start_t); return None

def API_Photon(query):
    start_t = time.time()
    try:
        if r := session.get(f"https://photon.komoot.io/api/?q={requests.utils.quote(query)}&limit=5&filter=countrycode:br", timeout=4).json().get("features"):
            reportar_telemetria("PHOTON", True, time.time() - start_t)
            return [{"lat": f["geometry"]["coordinates"][1], "lon": f["geometry"]["coordinates"][0], "fonte": "PHOTON", "score_base": 20, "cidade": f.get("properties", {}).get("city", "").upper(), "estado": f.get("properties", {}).get("state", "").upper(), "bairro": f.get("properties", {}).get("district", "").upper(), "logradouro": f.get("properties", {}).get("street", "").upper(), "cep": f.get("properties", {}).get("postcode", "").replace("-", "")} for f in r[:5]]
    except: pass
    reportar_telemetria("PHOTON", False, time.time() - start_t); return None

def API_Overpass_POIs(texto_norm):
    if len(texto_norm) < 10: return None
    start_t = time.time()
    try:
        if elems := session.post("https://overpass-api.de/api/interpreter", data={"data": f'[out:json][timeout:3];(node["name"~"{re.escape(texto_norm)}",i]["amenity"];way["name"~"{re.escape(texto_norm)}",i]["amenity"];node["name"~"{re.escape(texto_norm)}",i]["building"];way["name"~"{re.escape(texto_norm)}",i]["building"];);out center;'}, timeout=4).json().get("elements", []):
            tags = elems[0].get("tags", {})
            res_poi = {"lat": elems[0].get("lat", elems[0].get("center", {}).get("lat", 0.0)), "lon": elems[0].get("lon", elems[0].get("center", {}).get("lon", 0.0)), "fonte": "OVERPASS", "score_base": 40, "cidade": tags.get("addr:city", "").upper(), "estado": tags.get("addr:state", "").upper(), "bairro": tags.get("addr:suburb", "").upper(), "logradouro": tags.get("addr:street", "").upper(), "cep": tags.get("addr:postcode", "").replace("-", "")}
            reportar_telemetria("OVERPASS", True, time.time() - start_t); return [res_poi]
    except: pass
    reportar_telemetria("OVERPASS", False, time.time() - start_t); return None

def executar_reverse_geocoding_multimotor(lat, lon):
    rev_key = f"{round(lat,5)}|{round(lon,5)}"
    if rev_key in cache_reverse: return cache_reverse[rev_key]
    res = {"logradouro": "", "bairro": "", "cidade": "", "estado": "", "cep": ""}
    try:
        def _nom_rev():
            time.sleep(1.1)
            return session.get(f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&addressdetails=1", headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
        a = st.session_state["fila_nominatim"].submit(_nom_rev).result().get("address", {})
        res.update({"logradouro": a.get("road", a.get("pedestrian", "")), "bairro": a.get("neighbourhood", a.get("suburb", "")), "cidade": a.get("city", a.get("town", a.get("municipality", ""))), "estado": a.get("state", "").upper(), "cep": a.get("postcode", "")})
        cache_reverse.set(rev_key, res, expire=2592000); return res
    except: pass
    try:
        if addr := session.get(f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/reverseGeocode?location={lon},{lat}&f=json", timeout=4).json().get('address'):
            res.update({"logradouro": addr.get('Address', ''), "bairro": addr.get('Neighborhood', ''), "cidade": addr.get('City', ''), "estado": addr.get('RegionAbbr', '').upper(), "cep": addr.get('Postal', '')})
            cache_reverse.set(rev_key, res, expire=2592000)
    except: pass
    return res

# ==============================================================================
# 🧠 ENSEMBLE BAYESIANO, DBSCAN, ANTI-FANTASMA E XAI
# ==============================================================================
def processar_consenso_dinamico(candidatos, tipo_entrada, texto_cru):
    candidatos_validos = [c for c in candidatos if validar_coordenada_brasil(c["lat"], c["lon"])[0]]
    if not candidatos_validos: return None

    # DBSCAN Clusterização Esférica
    raio_cluster_km = 0.5 if tipo_entrada in ["ENDERECO_COMPLETO", "POI", "CEP", "CONDOMINIO"] else 2.0 if tipo_entrada in ["BAIRRO", "RURAL"] else 10.0
    coords_rad = np.radians([[c["lat"], c["lon"]] for c in candidatos_validos])
    
    if len(coords_rad) >= 2:
        db_model = DBSCAN(eps=(raio_cluster_km / 6371.0), min_samples=2, metric='haversine').fit(coords_rad)
        labels = db_model.labels_
        valid_labels = [l for l in labels if l != -1]
        
        if valid_labels:
            contagem = collections.Counter(valid_labels).most_common(2)
            # Ambiguidade Tie-Breaker
            if len(contagem) > 1 and contagem[0][1] == contagem[1][1]:
                c1_amb = candidatos_validos[labels.tolist().index(contagem[0][0])]; c2_amb = candidatos_validos[labels.tolist().index(contagem[1][0])]
                motivo = f"AMBÍGUO: Empate espacial entre {c1_amb.get('cidade','')}/{c1_amb.get('estado','')} e {c2_amb.get('cidade','')}/{c2_amb.get('estado','')}"
                return 0.0, 0.0, texto_cru, "AMBIGUA", 0, "", "", "N/A", [motivo]
            candidatos_validos = [candidatos_validos[idx] for idx, label in enumerate(labels) if label == contagem[0][0]]
            
    if not candidatos_validos: return None

    # Benchmark Dinâmico de APIs
    PESO_FONTES = {}
    DEFAULT_WEIGHTS = {"GOOGLE_MAPS": 1.00, "ARCGIS": 0.95, "TOMTOM": 0.90, "OVERPASS": 0.85, "NOMINATIM": 0.80, "PHOTON": 0.75}
    for fonte, def_weight in DEFAULT_WEIGHTS.items():
        metricas = cache_api_health.get(fonte, {"hits": 0, "calls": 0})
        PESO_FONTES[fonte] = round(max(0.5, metricas["hits"]/metricas["calls"]), 2) if metricas["calls"] >= 50 else def_weight

    BAYES_MULTIPLIERS = {
        "CEP": {"mun": 1.5, "uf": 1.2, "cep": 4.0, "bairro": 1.0, "numero": 1.0, "rua_peso": 0.2},
        "ENDERECO_COMPLETO": {"mun": 1.8, "uf": 1.3, "cep": 1.5, "bairro": 1.2, "numero": 2.5, "rua_peso": 1.5},
        "CONDOMINIO": {"mun": 1.8, "uf": 1.3, "cep": 1.2, "bairro": 1.5, "numero": 1.0, "rua_peso": 1.8},
        "DEFAULT": {"mun": 1.5, "uf": 1.2, "cep": 1.2, "bairro": 1.2, "numero": 1.2, "rua_peso": 0.8}
    }
    bm = BAYES_MULTIPLIERS.get(tipo_entrada, BAYES_MULTIPLIERS["DEFAULT"])
    ctx_inf = semantica.resolver_contexto_administrativo(texto_cru.upper())
    input_usuario = ParserGeograficoBR.extrair_componentes(texto_cru.upper())

    # Inferência Ensemble
    for c1 in candidatos_validos:
        p_prior = min(c1.get("score_base", 30) / 100.0, 0.50)
        feat_mun = ctx_inf.get("municipio") and c1.get("cidade") and (ctx_inf["municipio"] in c1["cidade"] or fuzz.token_set_ratio(ctx_inf["municipio"], c1["cidade"]) >= 95)
        feat_uf = ctx_inf.get("uf") and c1.get("estado") and ctx_inf["uf"] in c1["estado"]
        feat_cep = input_usuario.get("cep") and c1.get("cep") and input_usuario["cep"] in c1["cep"].replace("-", "")
        feat_bairro = ctx_inf.get("distrito") and c1.get("bairro") and ctx_inf["distrito"] in c1["bairro"]
        feat_numero = input_usuario.get("numero") and c1.get("numero") and input_usuario["numero"] in c1.get("numero", "")
        fuzz_rua = fuzz.token_set_ratio(texto_cru.upper(), c1.get("logradouro", "")) / 100.0 if c1.get("logradouro") else 0.1
        
        regex_rodovia = r'\b(BR|SP|MG|GO|DF|RJ|PR|SC|RS)[- ]?\d+\b|\b(RODOVIA|KM|ESTRADA)\b'
        feat_punicao_rodovia = not bool(re.search(regex_rodovia, texto_cru.upper())) and bool(re.search(regex_rodovia, c1.get("logradouro", "").upper()))

        probabilidades_cluster = [p_prior]
        apis_concordantes = set([c1.get("fonte", "")])
        for c2 in candidatos_validos:
            if c1.get("fonte") != c2.get("fonte") and calcular_distancia_vincenty(c1["lat"], c1["lon"], c2["lat"], c2["lon"]) <= raio_cluster_km: 
                apis_concordantes.add(c2.get("fonte", ""))
                probabilidades_cluster.append(PESO_FONTES.get(c2.get("fonte", ""), 0.5))
        
        falha_combinada = 1.0
        for prob in probabilidades_cluster: falha_combinada *= (1.0 - prob)
        prob_ensemble = 1.0 - falha_combinada
        
        odds = (prob_ensemble / (1 - prob_ensemble)) * (bm["mun"] if feat_mun else 0.4) * (bm["uf"] if feat_uf else 0.7) * (bm["cep"] if feat_cep else 0.9) * (bm["bairro"] if feat_bairro else 0.9) * (bm["numero"] if feat_numero else 0.8) * (0.5 + (fuzz_rua * bm["rua_peso"])) * (0.1 if feat_punicao_rodovia else 1.0)
        
        c1["score_final"] = min((odds / (1 + odds)) * 100, 99.9)
        c1["xai"] = {"feat_mun": feat_mun, "feat_uf": feat_uf, "feat_cep": feat_cep, "feat_numero": feat_numero, "fuzz_rua": round(fuzz_rua * 100, 1), "apis": list(apis_concordantes)}
        
    candidatos_validos.sort(key=lambda x: x["score_final"], reverse=True)
    
    # Top-3 Reverse Truncate
    vencedor = None
    for cand in candidatos_validos[:3]:
        m = executar_reverse_geocoding_multimotor(cand["lat"], cand["lon"])
        if ctx_inf.get("uf") and m.get("estado") and ctx_inf["uf"] != m["estado"].upper().strip(): continue 
        if ctx_inf.get("municipio") and m.get("cidade") and not (ctx_inf["municipio"] in m["cidade"].upper().strip() or fuzz.token_set_ratio(ctx_inf["municipio"], m["cidade"].upper().strip()) >= 85): continue
        end_reverse = ", ".join([c for c in [m.get("logradouro", ""), m.get("bairro", ""), m.get("cidade", ""), m.get("estado", "").upper()] if c.strip()])
        if fuzz.token_set_ratio(texto_cru.upper(), end_reverse.upper()) >= 70:
            vencedor = cand; break
            
    if not vencedor: return None

    # XAI & Quality Shield
    m = executar_reverse_geocoding_multimotor(vencedor["lat"], vencedor["lon"])
    score_lim = min(int(vencedor["score_final"]), 95 if tipo_entrada == "ENDERECO_COMPLETO" else 100 if tipo_entrada == "CEP" else 85)
    
    exp_humanas = []
    if len(vencedor["xai"]["apis"]) >= 2: exp_humanas.append(f"Consenso espacial Ensemble P=({round(vencedor['score_final'],1)}%) entre {' + '.join(vencedor['xai']['apis'])}.")
    else: exp_humanas.append(f"Decisão isolada na fonte {vencedor['fonte']} após DBSCAN.")
    if vencedor["xai"]["feat_mun"]: exp_humanas.append("Município validado via matriz IBGE.")
    if vencedor["xai"]["feat_cep"]: exp_humanas.append("CEP coincidente com alta precisão.")
    
    # Sistema Anti-Endereço Fantasma (Cross-Validation)
    match_logr = fuzz.token_set_ratio(texto_cru.upper(), m.get("logradouro", "").upper())
    match_bairro = fuzz.token_set_ratio(ctx_inf.get("distrito", ""), m.get("bairro", "").upper()) if ctx_inf.get("distrito") else 100
    match_cep = 100 if input_usuario.get("cep") and m.get("cep") and input_usuario["cep"] in m.get("cep", "").replace("-", "") else 0 if input_usuario.get("cep") else 100
    
    if (match_logr * 0.5) + (match_bairro * 0.3) + (match_cep * 0.2) < 65.0:
        confianca = "REVISAO_MANUAL"
        exp_humanas.append("⚠️ Alerta Anti-Fantasma: Integridade semântica final muito baixa. Possível interpolação forçada de logradouro/número.")
        score_lim = min(score_lim, 49)
    else:
        confianca = "MUNICIPAL" if tipo_entrada in ["ENDERECO_COMPLETO", "CEP"] and not vencedor.get("logradouro") else "ALTISSIMA" if score_lim >= 85 else "ALTA" if score_lim >= 75 else "MEDIA" if score_lim >= 60 else "BAIXA"

    rua_f = m.get("logradouro") if m.get("logradouro") else texto_cru.upper()
    endereco_f = ", ".join([c for c in [rua_f, m.get("bairro", ""), m.get("cidade", ""), m.get("estado", "").upper()] if c.strip()]) + ", BRASIL"
    return vencedor["lat"], vencedor["lon"], endereco_f, confianca, score_lim, m.get("distrito", ""), m.get("cidade", ""), vencedor["fonte"], exp_humanas

# ==============================================================================
# 🎚️ ORQUESTRADOR HIERÁRQUICO
# ==============================================================================
def obter_coordenadas_e_endereco_oficial(localidade):
    texto_cru = str(localidade).strip()
    if not texto_cru or texto_cru.lower() == 'nan': return 0.0, 0.0, "", "BAIXA", 0, "", "", "N/A", ["String Vazia"]
    
    # Bypass Coordenadas O(1)
    if match_coords := re.match(r'^\s*(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*$', texto_cru):
        lat_in, lon_in = float(match_coords.group(1)), float(match_coords.group(2))
        if validar_coordenada_brasil(lat_in, lon_in)[0]:
            m = executar_reverse_geocoding_multimotor(lat_in, lon_in)
            end_f = ", ".join([c for c in [m.get("logradouro", ""), m.get("bairro", ""), m.get("cidade", ""), m.get("estado", "")] if c.strip()]) + ", BRASIL"
            return lat_in, lon_in, end_f, "ABSOLUTA", 100, m.get("bairro", ""), m.get("cidade", ""), "COORDENADA_EXATA", ["Consulta direta de Coordenadas Lat/Lon."]

    # Bypass POIs Logísticos Nacionais B2B O(1)
    for poi_key, poi_data in BASE_POIS_LOGISTICOS.items():
        if poi_key in texto_cru.upper():
            return poi_data["lat"], poi_data["lon"], poi_data["endereco"], "ABSOLUTA", 100, "", poi_data["municipio"], "BASE_POIS_NACIONAIS", ["Resolvido via Ground Truth de Dicionário Offline de POIs."]

    # Memória Espacial Rica O(1)
    chave_auto = texto_cru.upper()
    if chave_auto in cache_aprendizado_auto:
        d = cache_aprendizado_auto[chave_auto]
        return d["lat"], d["lon"], d.get("endereco", texto_cru.upper()), "ALTISSIMA", 100, d.get("distrito", ""), d.get("municipio", ""), "APRENDIZADO_AUTO", d.get("metadata", {}).get("evidencias_xai", ["Cache LRU Espacial"])

    endereco_canonico, tipo_entrada, _, _, _ = semantica.construir_endereco_canonico(texto_cru)
    ctx = semantica.resolver_contexto_administrativo(texto_cru.upper())
    parsed_comp = ParserGeograficoBR.extrair_componentes(texto_cru.upper())
    
    cache_key = hashlib.md5(f"{tipo_entrada}_{endereco_canonico}".encode('utf-8')).hexdigest()
    if cache_key in cache_geo:
        c = cache_geo[cache_key]
        return c["lat"], c["lon"], c["endereco"], c["confianca"], c["score_num"], c["distrito"], c["municipio"], c["fonte"], c.get("xai", ["Cache Hit Geo L2"])

    rua_suja = parsed_comp["resto"]
    for loc in [ctx.get("municipio", ""), ctx.get("distrito", ""), ctx.get("uf", ""), "BRASIL", "DF"]:
        if loc: rua_suja = re.sub(rf'\b{loc}\b', '', rua_suja).strip(" ,-")
    rua_limpa = re.sub(r'\s+', ' ', rua_suja).strip()
    if parsed_comp["numero"]: rua_limpa = f"{rua_limpa} {parsed_comp['numero']}".strip()
    
    ctx_estr = {"logradouro": rua_limpa if rua_limpa else texto_cru.upper(), "bairro": ctx.get("distrito", ""), "municipio": ctx.get("municipio", ""), "uf": ctx.get("uf", ""), "cep": parsed_comp.get("cep", "")}

    # Pre-Flight Validator
    if auditoria_pre_geocoding(texto_cru, ctx_estr, tipo_entrada) == "INSUFICIENTE": return 0.0, 0.0, texto_cru, "INSUFICIENTE", 0, "", "", "PRE_FLIGHT", ["Rejeitado pelo validador semântico: falta contexto de cidade ou número."]

    candidatos_validos = []

    if tipo_entrada == "CEP" and parsed_comp["cep"]:
        logr, bair, loca, uf, lat_c, lon_c = cascata_postal_tripla(parsed_comp["cep"])
        if loca:
            addr_c = re.sub(r',\s*,', ',', f"{logr}, {bair}, {loca}, {IBGE_ESTADOS.get(uf, uf)}, CEP {parsed_comp['cep']}, BRASIL").strip(' ,')
            if validar_coordenada_brasil(lat_c, lon_c)[0] and lat_c != 0.0:
                res_f = (lat_c, lon_c, addr_c, "ALTISSIMA", 100, bair, loca, "BrasilAPI/OSM Postal", ["Postal Cascata Hit"])
                cache_geo.set(cache_key, {"lat": lat_c, "lon": lon_c, "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca, "fonte": "BrasilAPI/OSM Postal", "xai": ["Postal Cascata Hit"]}, expire=2592000)
                return res_f

    def disparar_apis(tarefas):
        resultados = []
        for f in as_completed([st.session_state["executor_apis"].submit(func, *args, **kwargs) for func, args, kwargs in tarefas]):
            if res := f.result(): resultados.extend(res)
        return resultados

    if tipo_entrada == "POI" or tipo_entrada == "CONDOMINIO":
        candidatos_validos.extend(disparar_apis([(API_Google_Geocoding_Scraper, (endereco_canonico,), {}), (API_Overpass_POIs, (semantica.normalizar(texto_cru),), {}), (API_TomTom, (endereco_canonico,), {})]))
    elif tipo_entrada in ["ENDERECO_COMPLETO", "LOGRADOURO"]:
        candidatos_validos.extend(disparar_apis([(API_ArcGIS, (endereco_canonico,), {"ctx": ctx_estr}), (API_Google_Geocoding_Scraper, (endereco_canonico,), {}), (API_TomTom, (endereco_canonico,), {})]))
        if r_nom := API_Nominatim(endereco_canonico, ctx=ctx_estr): candidatos_validos.extend(r_nom)
    elif tipo_entrada in ["BAIRRO", "MUNICIPIO", "DISTRITO"]:
        candidatos_validos.extend(disparar_apis([(API_Photon, (endereco_canonico,), {})]))
        if r_nom := API_Nominatim(endereco_canonico, ctx=ctx_estr): candidatos_validos.extend(r_nom)
    else:
        candidatos_validos.extend(disparar_apis([(API_Google_Geocoding_Scraper, (endereco_canonico,), {}), (API_Photon, (endereco_canonico,), {}), (API_ArcGIS, (endereco_canonico,), {"ctx": ctx_estr}), (API_TomTom, (endereco_canonico,), {})]))
            
    res_final = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru)
    
    if not res_final and tipo_entrada not in ["BAIRRO", "MUNICIPIO"]:
        if r_nom := API_Nominatim(endereco_canonico, ctx=ctx_estr):
            candidatos_validos.extend(r_nom)
            res_final = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru)

    if res_final:
        cache_geo.set(cache_key, {"lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], "confianca": res_final[3], "score_num": res_final[4], "distrito": res_final[5], "municipio": res_final[6], "fonte": res_final[7], "xai": res_final[8]}, expire=2592000)
        if res_final[4] >= 95 and res_final[3] == "ALTISSIMA":
            cache_aprendizado_auto.set(chave_auto, {"lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], "distrito": res_final[5], "municipio": res_final[6], "metadata": {"score_confianca": res_final[4], "data_captura": time.time(), "fonte_geradora": res_final[7], "evidencias_xai": res_final[8]}}, expire=7776000)
        return res_final
        
    return 0.0, 0.0, endereco_canonico, "BAIXA", 0, "", "", "N/A", ["Falha Geral de Roteamento ou Coordenadas Inválidas"]

# ==============================================================================
# 🚀 MOTOR DE ROTEAMENTO 
# ==============================================================================
def extrair_dados_reais_google(origem_raw, destino_raw, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=True):
    cache_key = f"{origem_raw}|{destino_raw}|{usar_coordenadas}"
    if cache_key in cache_google: return cache_google[cache_key]

    origem_param = f"{lat_o},{lon_o}" if usar_coordenadas else requests.utils.quote(origem_raw)
    destino_param = f"{lat_d},{lon_d}" if usar_coordenadas else requests.utils.quote(destino_raw)
    url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(origem_raw)}&destination={requests.utils.quote(destino_raw)}&travelmode=driving"
    
    try:
        resposta = session.get(url_api, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if len(resposta.text) < 500 or "directions" not in resposta.text.lower(): return None
        match_km = re.findall(r'\"(\d+[\.,]?\d*)\s*km\"', resposta.text)
        match_tempo = re.findall(r'\"(\d+\s*h\s*\d+\s*min|\d+\s*h|\d+\s*min)\"', resposta.text)
        
        if match_km and match_tempo:
            km_puro = float(match_km[0].replace('.', '').replace(',', '.'))
            if dist_linha_reta > 0:
                if dist_linha_reta <= 50.0 and km_puro > max(dist_linha_reta * 2.0, dist_linha_reta + 15.0): return None  
                elif km_puro < dist_linha_reta * 0.8 or km_puro > dist_linha_reta * 4.0: return None  

            balsa = "Sim" if any(re.search(p, resposta.text.lower()) for p in [r'\"utilizar\s+balsa\b', r'\"ferry\b']) else "Não"
            score_g = 70 + (10 if km_puro > 0 else 0) + (10 if match_tempo[0] else 0) + (10 if km_puro >= dist_linha_reta else 0)
            res = (km_puro, match_tempo[0], link_maps, balsa, score_g)
            cache_google.set(cache_key, res, expire=2592000); return res
    except: pass
    return None

def rota_osrm(lat_o, lon_o, lat_d, lon_d):
    try:
        r = session.get(f"https://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=false", timeout=5).json()
        if r.get("routes"):
            m = round(r["routes"][0]["duration"] / 60)
            return round(r["routes"][0]["distance"] / 1000, 2), f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min", "OSRM", 95
    except: pass
    return None

def calcular_pipeline_logistico(origem, destino, perfil_rota="shortest"):
    start_total = time.time()
    origem_clean, destino_clean = str(origem).strip(), str(destino).strip()
    
    chave_rota_cache = hashlib.md5(f"ROTA_{semantica.normalizar(origem_clean)}->{semantica.normalizar(destino_clean)}".encode('utf-8')).hexdigest()
    if chave_rota_cache in cache_rotas: return cache_rotas[chave_rota_cache]
    
    start_geo = time.time()
    lat_o, lon_o, end_o, conf_o, sc_num_o, dist_o, mun_o, fnt_o, xai_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    if conf_o == "INSUFICIENTE": return ("QA_REJEITADO", "N/A", "N/A", "N/A", 0.0, "Pre-Flight Reject (Origem)", 0, conf_o, sc_num_o, dist_o, mun_o, fnt_o, end_o, "N/A", 0, "", "", "N/A", destino_clean, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, xai_o, [])
    
    lat_d, lon_d, end_d, conf_d, sc_num_d, dist_d, mun_d, fnt_d, xai_d = obter_coordenadas_e_endereco_oficial(destino_clean)
    tempo_geo = round(time.time() - start_geo, 2)
    start_rot = time.time()

    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d) if lat_o and lat_d else 0.0

    def formatar_retorno(tupla_dados):
        if falha_qa := auditoria_geografica(tupla_dados[0], tupla_dados[1], dist_linha_reta, lat_o, lon_o, lat_d, lon_d):
            return ("QA_REJEITADO", "N/A", "N/A", "N/A", dist_linha_reta, f"QA Falhou: {falha_qa}", 0, conf_o, sc_num_o, dist_o, mun_o, fnt_o, end_o, conf_d, sc_num_d, dist_d, mun_d, fnt_d, end_d, lat_o, lon_o, lat_d, lon_d, tempo_geo, 0.0, round(time.time() - start_total, 2), xai_o, xai_d)
        return tupla_dados

    usar_coords = True if (lat_o != 0.0 and lat_d != 0.0) else False
    if usar_coords and dist_linha_reta > 150.0:
        if len(set(re.findall(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM|PA|MT|MS)\b', origem_clean.upper() + " " + destino_clean.upper()))) <= 1: usar_coords = False

    link_fb = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(end_o)}&destination={requests.utils.quote(end_d)}&travelmode=driving"

    res_osrm = rota_osrm(lat_o, lon_o, lat_d, lon_d) if usar_coords else None
    if res_osrm and perfil_rota == "fastest":
        ret = formatar_retorno((res_osrm[0], res_osrm[1], link_fb, "Não", dist_linha_reta, res_osrm[2], res_osrm[3], conf_o, sc_num_o, dist_o, mun_o, fnt_o, end_o, conf_d, sc_num_d, dist_d, mun_d, fnt_d, end_d, lat_o, lon_o, lat_d, lon_d, tempo_geo, round(time.time() - start_rot, 2), round(time.time() - start_total, 2), xai_o, xai_d))
        cache_rotas.set(chave_rota_cache, ret, expire=2592000); return ret

    res_google = extrair_dados_reais_google(end_o, end_d, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=usar_coords)

    if perfil_rota == "shortest":
        opcoes = []
        if res_osrm: opcoes.append((res_osrm[0], res_osrm[1], link_fb, "Não", dist_linha_reta, res_osrm[2], res_osrm[3]))
        if res_google: opcoes.append((res_google[0], res_google[1], res_google[2], res_google[3], dist_linha_reta, "Google Preview", res_google[4]))
        if opcoes:
            m_opt = min(opcoes, key=lambda x: x[0]) 
            ret = formatar_retorno((*m_opt, conf_o, sc_num_o, dist_o, mun_o, fnt_o, end_o, conf_d, sc_num_d, dist_d, mun_d, fnt_d, end_d, lat_o, lon_o, lat_d, lon_d, tempo_geo, round(time.time() - start_rot, 2), round(time.time() - start_total, 2), xai_o, xai_d))
            cache_rotas.set(chave_rota_cache, ret, expire=2592000); return ret

    if res_google:
        ret = formatar_retorno((res_google[0], res_google[1], res_google[2], res_google[3], dist_linha_reta, "Google Preview", res_google[4], conf_o, sc_num_o, dist_o, mun_o, fnt_o, end_o, conf_d, sc_num_d, dist_d, mun_d, fnt_d, end_d, lat_o, lon_o, lat_d, lon_d, tempo_geo, round(time.time() - start_rot, 2), round(time.time() - start_total, 2), xai_o, xai_d))
        cache_rotas.set(chave_rota_cache, ret, expire=2592000); return ret

    km_t = round(dist_linha_reta * (1.45 if dist_linha_reta < 5.0 else 1.35 if dist_linha_reta < 20.0 else 1.25 if dist_linha_reta < 100.0 else 1.18), 2)
    m_est = round((km_t / (45.0 if km_t < 50.0 else 65.0)) * 60) if km_t > 0 else 0
    t_geo_str = f"{m_est} min" if m_est < 60 else f"{m_est // 60} h {m_est % 60} min"
    
    ret = formatar_retorno((km_t, t_geo_str, link_fb, "Não", dist_linha_reta, "Geodésico Adaptativo", 70, conf_o, sc_num_o, dist_o, mun_o, fnt_o, end_o, conf_d, sc_num_d, dist_d, mun_d, fnt_d, end_d, lat_o, lon_o, lat_d, lon_d, tempo_geo, round(time.time() - start_rot, 2), round(time.time() - start_total, 2), xai_o, xai_d))
    cache_rotas.set(chave_rota_cache, ret, expire=2592000); return ret

def embrulhar_task_paralela(item):
    try: return item[0], calcular_pipeline_logistico(item[1], item[2], perfil_rota="shortest")
    except: return item[0], None

# ==============================================================================
# 🚗 UI CORPORATIVA (STREAMLIT, PYDECK E AUDIT TABS)
# ==============================================================================
st.markdown("""
<div style="background-color:#1E1E1E; padding:20px; border-radius:10px; margin-bottom: 25px; border-left: 5px solid #00FF7F;">
    <h1 style="color:white; margin:0;">🗺️ Motor Nacional de Roteirização Inteligente</h1>
    <p style="color:#A0A0A0; margin:0; font-size: 16px;">Plataforma Corporativa B2B de Geocodificação, Inferência Bayesiana e Auditoria Logística.</p>
</div>
""", unsafe_allow_html=True)

with st.sidebar:
    st.header("📖 Manual do Sistema")
    with st.expander("🎯 Visão Geral"): st.markdown("1. Validador B2B Offline\n2. Ensemble Bayesiano (DBSCAN)\n3. Cross-Validation IBGE\n4. Roteamento OSRM/Google\n5. Auditoria de Decisões")
    with st.expander("📍 Geocodificação"): st.markdown("APIs Independentes em Paralelo: ArcGIS, Google, TomTom, Nominatim, Photon, Overpass.")
    with st.expander("📏 Linha Reta vs Rota"): st.markdown("Geodésia de Vincenty para cálculo real da esfera vs Malha Rodoviária Oficial.")
    with st.expander("📊 Score e XAI"): st.markdown("Pesos Dinâmicos Bayesianos para Origem, Destino e Qualidade da Rota com Explicabilidade Ativa.")

tab_individual, tab_processamento, tab_analytics, tab_auditoria = st.tabs([
    "📍 Consulta Rápida (Single-Shot)", "🛣️ Processamento em Lote (Excel)", "📊 Analytics & Saúde", "🕵️ Dossiê de Auditoria (XAI)"
])

with tab_individual:
    st.markdown("### 🔍 Validador Rápido de Rota")
    col1, col2 = st.columns(2)
    with col1: orig_ind = st.text_input("Origem (Endereço, POI Logístico, ou Lat, Lon)", "CD MERCADO LIVRE CAJAMAR")
    with col2: dest_ind = st.text_input("Destino", "-15.793889, -47.882778")
    
    if st.button("🚀 Processar Rota Individual", type="primary"):
        if orig_ind and dest_ind:
            with st.spinner("Triangulando coordenadas e extraindo malha viária..."):
                res = calcular_pipeline_logistico(orig_ind, dest_ind)
                
            if res and res[0] != "QA_REJEITADO" and res[0] != "GEOCODING_FALHOU":
                st.success("✅ Rota Corporativa Estabelecida!")
                c_dist, c_time, c_score = st.columns(3)
                c_dist.metric("Distância Viária", f"{res[0]} km" if isinstance(res[0], float) else res[0])
                c_time.metric("Tempo Viário", res[1])
                score_g = round((0.35 * res[8]) + (0.35 * res[14]) + (0.30 * res[6]), 1)
                c_score.metric("Score de Integridade", f"{score_g} / 100")
                
                lat_center, lon_center = (res[19] + res[21]) / 2, (res[20] + res[22]) / 2
                arc_layer = pdk.Layer("ArcLayer", data=[{"o": [res[20], res[19]], "d": [res[22], res[21]]}], get_source_position="o", get_target_position="d", get_source_color=[0, 255, 128, 160], get_target_color=[255, 0, 0, 160], auto_highlight=True, width_scale=0.04, get_width="outLineWidth * 2", width_min_pixels=3, width_max_pixels=15)
                scatter_layer = pdk.Layer("ScatterplotLayer", data=[{"p": [res[20], res[19]], "c": [0, 255, 128]}, {"p": [res[22], res[21]], "c": [255, 0, 0]}], get_position="p", get_fill_color="c", get_radius=800)
                coverage_layer = pdk.Layer("ScatterplotLayer", data=[{"p": [res[22], res[21]], "r": 50000, "c": [255, 165, 0, 80]}, {"p": [res[22], res[21]], "r": 100000, "c": [0, 191, 255, 60]}, {"p": [res[22], res[21]], "r": 200000, "c": [138, 43, 226, 40]}], get_position="p", get_radius="r", stroked=True, filled=True, get_fill_color="c", get_line_color=[255, 255, 255, 150], line_width_min_pixels=1)
                
                st.pydeck_chart(pdk.Deck(layers=[coverage_layer, arc_layer, scatter_layer], initial_view_state=pdk.ViewState(latitude=lat_center, longitude=lon_center, zoom=4, pitch=45), map_style="mapbox://styles/mapbox/dark-v10"))
                
                st.info(f"**Origem:** {res[11]} ({res[12]}) | **Destino:** {res[17]} ({res[18]}) | **Roteamento:** {res[5]}")
                st.markdown(f"[🔗 Abrir Rota Operacional no Google Maps]({res[2]})")
            else: st.error(f"Falha ao estabelecer rota. Motivo: {res[5] if res else 'Erro Desconhecido'}")
        else: st.warning("Preencha origem e destino.")

with tab_processamento:
    st.markdown("### 🛣️ Motor de Processamento Paralelo O(U)")
    arquivo_carregado = st.file_uploader("Selecione sua matriz logística (Excel)", type=["xlsx"])

    if arquivo_carregado is not None:
        df = pd.read_excel(arquivo_carregado)
        df.columns = df.columns.str.strip().str.title()
        
        if 'Origem' not in df.columns or 'Destino' not in df.columns: st.error("A planilha deve possuir as colunas 'Origem' e 'Destino'.")
        else:
            if len(df) > 5000: st.error("Limitação do cluster: Máximo de 5.000 linhas por lote."); st.stop()
            st.success(f"Tabela validada: {len(df)} registros.")
            
            nome_operador = st.text_input("Matrícula / Identificação do Operador (Opcional)", max_chars=50)
            
            if st.button("🚀 Iniciar Lote Corporativo", type="primary"):
                start_lote = time.time()
                for col in ['Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta', 'Fonte da Rota', 'Score da Rota', 'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem', 'Fonte Origem', 'Endereco Oficial Origem', 'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino', 'Fonte Destino', 'Endereco Oficial Destino', 'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Total (s)', 'Score Final Global', 'Status da Rota']: df[col] = None
                    
                pares_unicos, mapeamento = set(), []
                for idx, linha in df.iterrows():
                    o, d = str(getattr(linha, 'Origem', '')).strip(), str(getattr(linha, 'Destino', '')).strip()
                    if o and d and o.lower() != 'nan' and d.lower() != 'nan':
                        pares_unicos.add((o, d)); mapeamento.append((idx, o, d))
                
                if not pares_unicos: st.stop()
                st.info(f"Otimização Deduplicada: {len(pares_unicos)} rotas exclusivas na fila de prioridade.")
                
                MAPA_PRI = {"CEP": 1, "CONDOMINIO": 2, "ENDERECO_COMPLETO": 3, "POI": 3, "MUNICIPIO": 4, "BAIRRO": 5, "RURAL": 6, "LOGRADOURO": 7}
                tarefas_pri = sorted([(MAPA_PRI.get(semantica.classificar_entrada(semantica.normalizar(p[0])), 99), p) for p in pares_unicos], key=lambda x: x[0])
                futuros = {st.session_state["executor_global"].submit(embrulhar_task_paralela, (t[1], t[1][0], t[1][1])): t for t in tarefas_pri}
                
                resultados_unicos, concluidos, progresso, c_status = {}, 0, st.progress(0), st.empty()
                st.session_state['logs_auditoria'] = []
                
                for f in as_completed(futuros):
                    par_id, res = f.result()
                    resultados_unicos[par_id] = res
                    concluidos += 1
                    c_status.text(f"🚀 Fila de Prioridade Bayesiana: {concluidos} / {len(pares_unicos)}")
                    progresso.progress(concluidos / len(pares_unicos))
                    
                for idx, o, d in mapeamento:
                    if res := resultados_unicos.get((o, d)):
                        if res[0] == "QA_REJEITADO":
                            df.at[idx, 'Status da Rota'] = "Erro Crítico / QA Falhou"
                            df.at[idx, 'Score Final Global'] = 0.0
                        else:
                            sg = round((0.35 * res[8]) + (0.35 * res[14]) + (0.30 * res[6]), 2)
                            df.loc[idx, ['Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta', 'Fonte da Rota', 'Score da Rota', 'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem', 'Fonte Origem', 'Endereco Oficial Origem', 'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino', 'Fonte Destino', 'Endereco Oficial Destino', 'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Total (s)', 'Score Final Global', 'Status da Rota']] = [res[0], res[1], res[2], res[3], res[4], res[5], res[6], res[7], res[8], res[9], res[10], res[11], res[12], res[13], res[14], res[15], res[16], res[17], res[18], res[19], res[20], res[21], res[22], res[23], res[25], sg, "Excelente" if sg >= 90 else "Boa" if sg >= 80 else "Aceitável" if sg >= 70 else "Revisão Manual"]
                            st.session_state['logs_auditoria'].append({"Endereço Informado": o, "Canonical": res[12], "Fonte Vencedora": res[11], "Confiança": res[7], "Score": res[8], "XAI (Evidências)": " | ".join(res[26]) if len(res)>26 else "N/A"})
                    else: df.at[idx, 'Status da Rota'] = "Erro Estrutural"

                t_total = round(time.time() - start_lote, 2)
                cache_historico_lotes.set(f"lote_{start_lote}", {"Data": time.strftime("%Y-%m-%d %H:%M:%S"), "Operador": nome_operador.strip() or "Operador Automático", "Rotas Únicas": len(pares_unicos), "Tempo Total (s)": t_total, "Méd/Rota (s)": round(t_total/max(1, len(pares_unicos)), 2)}, expire=None)
                
                st.session_state['df_processado'] = df
                c_status.empty(); progresso.empty()
                st.success("✨ Lote auditado e finalizado!")
                
                buf = io.BytesIO()
                with pd.ExcelWriter(buf, engine='openpyxl') as w: df.to_excel(w, index=False)
                st.session_state['planilha_pronta'] = buf.getvalue()

        if 'planilha_pronta' in st.session_state:
            st.write("---")
            st.download_button("📥 Download Matriz Enriquecida", data=st.session_state['planilha_pronta'], file_name="matriz_logistica.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

with tab_analytics:
    st.markdown("### 📊 Painel de Integridade de Negócios (BI)")
    if 'df_processado' in st.session_state:
        df_kpi = st.session_state['df_processado']
        df_suc = df_kpi[~df_kpi["Status da Rota"].str.contains("Erro", na=False)]
        
        c1, c2, c3 = st.columns(3); c4, c5, c6 = st.columns(3)
        c1.metric("Volume do Lote", len(df_kpi))
        c2.metric("Taxa de Sucesso", f"{round((len(df_suc)/len(df_kpi))*100, 1)}%" if len(df_kpi)>0 else "0%")
        c3.metric("Distância Média", f"{round(df_suc['Distancia'].mean(), 1) if not df_suc.empty else 0} km")
        c4.metric("Tempo Médio Geocoding", f"{round(df_kpi['Tempo Geocoding (s)'].mean(), 2)} s")
        c5.metric("Tempo Médio Rota", f"{round(df_kpi['Tempo Total (s)'].mean(), 2)} s")
        c6.metric("Score Corporativo", f"{round(df_suc['Score Final Global'].mean(), 1) if not df_suc.empty else 0} / 100")
        
        st.markdown("#### 🚨 Heatmap de Falhas (Score < 70)")
        df_err = df_kpi[(df_kpi['Score Final Global'] < 70) & (df_kpi['Lat Destino'].notna())]
        if not df_err.empty:
            hl = pdk.Layer("HeatmapLayer", data=df_err, get_position=['Lon Destino', 'Lat Destino'], aggregation='"SUM"', get_weight="100 - `Score Final Global`", radiusPixels=50)
            st.pydeck_chart(pdk.Deck(layers=[hl], initial_view_state=pdk.ViewState(latitude=-15.78, longitude=-47.92, zoom=3), map_style="mapbox://styles/mapbox/dark-v10"))
        else: st.success("Nenhuma inconsistência de alto risco detectada!")
        
        st.markdown("#### 🩺 Telemetria das APIs (Health Monitor)")
        health = [{"Provedor": k, "Status": "Online" if v["falhas"] < v["calls"] else "Instável", "Latência (ms)": round((v["tempo_total"]/max(1,v["calls"]))*1000), "Taxa Erro": f"{round((v['falhas']/max(1,v['calls']+v['falhas']))*100, 1)}%", "Chamadas": v["calls"]} for k, v in cache_api_health.items() if k in ["GOOGLE_MAPS", "ARCGIS", "TOMTOM", "NOMINATIM", "PHOTON", "OVERPASS"]]
        st.dataframe(pd.DataFrame(health), use_container_width=True)
        
        st.markdown("#### 📜 Audit Trail (Histórico de Lotes)")
        hst = [cache_historico_lotes[k] for k in cache_historico_lotes]
        st.dataframe(pd.DataFrame(hst).sort_values(by="Data", ascending=False).reset_index(drop=True) if hst else pd.DataFrame(), use_container_width=True)
    else: st.info("Processe uma matriz Excel para popular o dashboard de negócios.")

with tab_auditoria:
    st.markdown("### 🕵️ Dossiê de Explicabilidade Ativa (XAI)")
    st.write("Acompanhe como o Ensemble Bayesiano avaliou as inferências do último processamento.")
    if 'logs_auditoria' in st.session_state and st.session_state['logs_auditoria']: st.dataframe(pd.DataFrame(st.session_state['logs_auditoria']), use_container_width=True)
    else: st.info("Aguardando operações para gerar matriz de explicabilidade.")
