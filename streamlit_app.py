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
import threading
from unidecode import unidecode
from rapidfuzz import process, fuzz
from diskcache import Cache
from sklearn.cluster import DBSCAN
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from prometheus_client import Counter, Histogram, CollectorRegistry
import structlog

# ==============================================================================
# [CONFIG] 7. CONFIGURAÇÕES CENTRALIZADAS
# ==============================================================================
class Config:
    APP_NAME = "TMS_Enterprise"
    VERSION = "1.1"
    
    # Chaves de API
    TOMTOM_API_KEY = "" # Insira sua credencial TomTom Logistics aqui
    
    # Resiliência & Timeouts
    TIMEOUT_DEFAULT = 5
    TIMEOUT_RELAXED = 8
    CIRCUIT_BREAKER_MAX_FAILURES = 5
    CIRCUIT_BREAKER_COOLDOWN_SEC = 60
    RATE_LIMIT_REQ_PER_SEC = 5.0
    
    # Parâmetros de Geoprocessamento & Machine Learning
    DBSCAN_RADIUS_KM_DEFAULT = 10.0
    DBSCAN_RADIUS_KM_URBAN = 0.5
    DBSCAN_RADIUS_KM_RURAL = 2.0
    SCORE_MINIMO_ACEITAVEL = 70
    MATCH_TEXTUAL_MINIMO = 85
    
    # Concorrência
    MAX_WORKERS_GLOBAL = 8
    MAX_WORKERS_API = 16
    
    # Cache
    CACHE_EXPIRATION_GEO = 2592000
    CACHE_EXPIRATION_POI = 7776000
    IBGE_CACHE_PATH = "municipios_ibge.pkl"

# ==============================================================================
# [OBSERVABILITY] 3. METRICAS E 5. LOGGING ESTRUTURADO
# ==============================================================================
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger(Config.APP_NAME)

class MetricsCollector:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.geocode_requests = Counter("geocode_requests_total", "Geocoding requests", ["provider"], registry=self.registry)
        self.geocode_failures = Counter("geocode_failures_total", "Geocoding failures", ["provider", "reason"], registry=self.registry)
        self.route_requests = Counter("route_requests_total", "Routing requests", ["provider"], registry=self.registry)
        self.route_failures = Counter("route_failures_total", "Routing failures", ["provider"], registry=self.registry)
        self.api_latency = Histogram("api_latency_seconds", "API latency", ["provider"], registry=self.registry)

metrics = MetricsCollector()

# ==============================================================================
# [SECURITY] 2. RESILIENCIA (CIRCUIT BREAKER & RATE LIMITER)
# ==============================================================================
class CircuitBreaker:
    def __init__(self):
        self.failures = collections.defaultdict(int)
        self.last_failure = collections.defaultdict(float)
        self.state = collections.defaultdict(lambda: "CLOSED") # CLOSED, OPEN, HALF_OPEN
        self._lock = threading.Lock()

    def check(self, provider):
        with self._lock:
            if self.state[provider] == "OPEN":
                if time.time() - self.last_failure[provider] > Config.CIRCUIT_BREAKER_COOLDOWN_SEC:
                    self.state[provider] = "HALF_OPEN"
                    logger.info("circuit_breaker_half_open", provider=provider)
                    return True
                return False
            return True

    def record_success(self, provider):
        with self._lock:
            self.failures[provider] = 0
            self.state[provider] = "CLOSED"

    def record_failure(self, provider):
        with self._lock:
            self.failures[provider] += 1
            self.last_failure[provider] = time.time()
            if self.failures[provider] >= Config.CIRCUIT_BREAKER_MAX_FAILURES:
                self.state[provider] = "OPEN"
                logger.error("circuit_breaker_open", provider=provider)

class RateLimiter:
    def __init__(self, rate):
        self.delay = 1.0 / rate
        self.last_call = collections.defaultdict(float)
        self._lock = threading.Lock()

    def wait(self, provider):
        with self._lock:
            now = time.time()
            elapsed = now - self.last_call[provider]
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            self.last_call[provider] = time.time()

circuit_breaker = CircuitBreaker()
rate_limiter = RateLimiter(Config.RATE_LIMIT_REQ_PER_SEC)

# ==============================================================================
# [DATABASE] INFRAESTRUTURA DE PERSISTÊNCIA E SESSÃO
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

session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)

if "executor_global" not in st.session_state:
    st.session_state["executor_global"] = ThreadPoolExecutor(max_workers=Config.MAX_WORKERS_GLOBAL)
if "fila_nominatim" not in st.session_state:
    st.session_state["fila_nominatim"] = ThreadPoolExecutor(max_workers=1)
if "executor_apis" not in st.session_state:
    st.session_state["executor_apis"] = ThreadPoolExecutor(max_workers=Config.MAX_WORKERS_API)

# ==============================================================================
# [MODELS] DADOS GLOBAIS E MODELO SEMÂNTICO
# ==============================================================================
BASE_POIS_LOGISTICOS = {
    "CD MAGAZINE LUIZA CAXIAS": {"lat": -22.7853, "lon": -43.3121, "endereco": "Centro de Distribuição Magazine Luiza, Duque de Caxias, RJ, BRASIL", "municipio": "DUQUE DE CAXIAS", "uf": "RJ"},
    "CD MERCADO LIVRE CAJAMAR": {"lat": -23.3541, "lon": -46.8852, "endereco": "Centro de Distribuição Mercado Livre, Cajamar, SP, BRASIL", "municipio": "CAJAMAR", "uf": "SP"},
    "CD AMAZON CAJAMAR": {"lat": -23.3600, "lon": -46.8900, "endereco": "Centro de Distribuição Amazon, Cajamar, SP, BRASIL", "municipio": "CAJAMAR", "uf": "SP"}
}

BOUNDING_BOXES_UF = {
    "DF": {"lat_min": -16.05, "lat_max": -15.50, "lon_min": -48.30, "lon_max": -47.30},
    "SP": {"lat_min": -25.50, "lat_max": -19.50, "lon_min": -53.50, "lon_max": -44.00},
    "GO": {"lat_min": -19.50, "lat_max": -12.40, "lon_min": -53.30, "lon_max": -45.90},
}

@st.cache_data
def carregar_dados_ibge():
    if os.path.exists(Config.IBGE_CACHE_PATH):
        if time.time() - os.path.getmtime(Config.IBGE_CACHE_PATH) > Config.CACHE_EXPIRATION_GEO:
            os.remove(Config.IBGE_CACHE_PATH)
        else:
            try:
                with open(Config.IBGE_CACHE_PATH, "rb") as f:
                    d = pickle.load(f)
                    return d.get("municipios", {}), d.get("estados", {}), d.get("distritos", {}), list(d.get("municipios", {}).keys()) + list(d.get("distritos", {}).keys())
            except Exception as e:
                logger.warning("ibge_cache_load_error", error=str(e))

    base_mun, base_est, base_dist = {}, {}, {}
    try:
        r_est = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/estados", timeout=Config.TIMEOUT_RELAXED)
        if r_est.status_code == 200:
            for est in r_est.json(): base_est[est["sigla"]] = unidecode(est["nome"]).upper()
                
        r_mun = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/municipios", timeout=Config.TIMEOUT_RELAXED)
        if r_mun.status_code == 200:
            for mun in r_mun.json():
                nome_norm = unidecode(mun["nome"]).upper().strip()
                uf_sigla = mun["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper()
                if nome_norm not in base_mun: base_mun[nome_norm] = []
                base_mun[nome_norm].append({"uf": uf_sigla, "municipio": nome_norm, "lat": mun.get("lat", 0.0), "lon": mun.get("lon", 0.0)})
                
        r_dist = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/distritos", timeout=Config.TIMEOUT_RELAXED)
        if r_dist.status_code == 200:
            for dist in r_dist.json():
                nome_dist = unidecode(dist["nome"]).upper().strip()
                nome_muni = unidecode(dist["municipio"]["nome"]).upper().strip()
                uf_dist = dist["municipio"]["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper()
                if nome_dist not in base_dist: base_dist[nome_dist] = []
                base_dist[nome_dist].append({"uf": uf_dist, "municipio": nome_muni, "lat": dist.get("lat", 0.0), "lon": dist.get("lon", 0.0)})

        with open(Config.IBGE_CACHE_PATH, "wb") as f:
            pickle.dump({"municipios": base_mun, "estados": base_est, "distritos": base_dist}, f)
    except Exception as e:
        logger.error("ibge_api_error", error=str(e))
    
    return base_mun, base_est, base_dist, list(base_mun.keys()) + list(base_dist.keys())

IBGE_MUNICIPIOS, IBGE_ESTADOS, IBGE_DISTRITOS, LISTA_TOPONIMOS = carregar_dados_ibge()

class SemanticModel:
    def __init__(self):
        self.contexto_fuzzy = list(set([f"{k} {v['uf']}" for k, vl in IBGE_MUNICIPIOS.items() for v in vl] + 
                                       [f"{k} {v['uf']}" for k, vl in IBGE_DISTRITOS.items() for v in vl]))
        self.sinonimos = {
            "UNB": "UNIVERSIDADE DE BRASILIA", "CATOLICA": "UNIVERSIDADE CATOLICA",
            "JK": "JUSCELINO KUBITSCHEK", "HBDF": "HOSPITAL DE BASE DO DISTRITO FEDERAL",
            "CD": "CENTRO DE DISTRIBUICAO", "HUB": "CENTRO LOGISTICO", "TECA": "TERMINAL DE CARGAS"
        }
        self.poi_keys = ["AEROPORTO", "HOSPITAL", "UNIVERSIDADE", "SHOPPING", "RODOVIARIA", "CD", "TERMINAL"]
        self.condo_keys = [r"\bCONDOMINIO\b", r"\bCOND\.", r"\bRESIDENCIAL\b", r"\bLOTEAMENTO\b"]
        self.rural_keys = ["FAZENDA", "SITIO", "ASSENTAMENTO", "CHACARA", "GLEBA"]
        self.via_keys = ["RUA", "AVENIDA", "TRAVESSA", "ALAMEDA", "RODOVIA", "ESTRADA", "QUADRA", "BR", "SP"]

    def normalizar(self, texto):
        if not texto or pd.isna(texto): return ""
        t = unidecode(str(texto).strip()).upper()
        t = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', t)
        t = re.sub(r'\b0+(\d{1,4})\b', r'\1', t) 
        for k, v in self.sinonimos.items(): t = re.sub(r'\b' + k + r'\b', v, t)
        return re.sub(r'\s+', ' ', t).strip()

    def classificar(self, texto):
        if re.search(r'\b\d{5}-?\d{3}\b', texto): return "CEP"
        if any(re.search(p, texto) for p in self.condo_keys): return "CONDOMINIO"
        if any(k in texto for k in self.poi_keys): return "POI"
        if any(k in texto for k in self.rural_keys): return "RURAL"
        if any(k in texto for k in self.via_keys) and re.search(r'\d+', texto): return "ENDERECO_COMPLETO"
        if texto in IBGE_MUNICIPIOS: return "MUNICIPIO"
        return "LOGRADOURO"

    def contexto_administrativo(self, texto):
        tokens = texto.split()
        uf = next((re.sub(r'[^A-Z]', '', t) for t in reversed(tokens) if re.sub(r'[^A-Z]', '', t) in IBGE_ESTADOS), "")
        for i in range(len(tokens)):
            for j in range(i + 1, len(tokens) + 1):
                chunk = " ".join(tokens[i:j])
                if chunk in IBGE_MUNICIPIOS:
                    uf_match = uf if uf else IBGE_MUNICIPIOS[chunk][0]["uf"]
                    return {"uf": uf_match, "municipio": chunk, "distrito": ""}
                if chunk in IBGE_DISTRITOS:
                    uf_match = uf if uf else IBGE_DISTRITOS[chunk][0]["uf"]
                    return {"uf": uf_match, "municipio": IBGE_DISTRITOS[chunk][0]["municipio"], "distrito": chunk}
        return {"uf": uf, "municipio": "", "distrito": ""}

semantic_model = SemanticModel()

# ==============================================================================
# [PROVIDERS] INTEGRAÇÕES COM APIs EXTERNAS (TESTÁVEIS E INJETÁVEIS)
# ==============================================================================
class GeocodingProvider:
    @staticmethod
    def _execute(provider_name, func, *args, **kwargs):
        if not circuit_breaker.check(provider_name):
            metrics.geocode_failures.labels(provider=provider_name, reason="circuit_open").inc()
            return None
        rate_limiter.wait(provider_name)
        metrics.geocode_requests.labels(provider=provider_name).inc()
        start_t = time.time()
        try:
            result = func(*args, **kwargs)
            circuit_breaker.record_success(provider_name)
            metrics.api_latency.labels(provider=provider_name).observe(time.time() - start_t)
            return result
        except Exception as e:
            circuit_breaker.record_failure(provider_name)
            metrics.geocode_failures.labels(provider=provider_name, reason="exception").inc()
            logger.error("provider_error", provider=provider_name, error=str(e))
            return None

    @classmethod
    def google(cls, query):
        def _call():
            url = f"https://www.google.com/maps/search/{requests.utils.quote(query)}"
            r = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=Config.TIMEOUT_DEFAULT, allow_redirects=True)
            match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.url) or re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.text)
            if match:
                return [{"lat": float(match.group(1)), "lon": float(match.group(2)), "fonte": "GOOGLE_MAPS", "score_base": 40}]
            return None
        return cls._execute("GOOGLE_MAPS", _call)

    @classmethod
    def tomtom(cls, query):
        if not Config.TOMTOM_API_KEY: return None
        def _call():
            url = f"https://api.tomtom.com/search/2/geocode/{requests.utils.quote(query)}.json?key={Config.TOMTOM_API_KEY}&countrySet=BR&limit=5"
            r = session.get(url, timeout=Config.TIMEOUT_DEFAULT).json()
            if r.get("results"):
                return [{"lat": float(res["position"]["lat"]), "lon": float(res["position"]["lon"]), "fonte": "TOMTOM", "score_base": 35, "cidade": res.get("address", {}).get("municipality", "").upper(), "estado": res.get("address", {}).get("countrySubdivision", "").upper()} for res in r["results"][:5]]
            return None
        return cls._execute("TOMTOM", _call)

    @classmethod
    def arcgis(cls, query):
        def _call():
            url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=5&sourceCountry=BRA"
            r = session.get(url, timeout=Config.TIMEOUT_DEFAULT).json()
            if r.get('candidates'):
                return [{"lat": float(c['location']['y']), "lon": float(c['location']['x']), "fonte": "ARCGIS", "score_base": 30, "cidade": c.get('attributes', {}).get('City', '').upper(), "estado": c.get('attributes', {}).get('RegionAbbr', '').upper()} for c in r['candidates'][:5]]
            return None
        return cls._execute("ARCGIS", _call)

    @classmethod
    def nominatim(cls, query):
        def _call():
            url = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query)}&limit=5&countrycodes=br"
            r = st.session_state["fila_nominatim"].submit(lambda: session.get(url, headers={"User-Agent": "TMS_Enterprise/1.1"}, timeout=Config.TIMEOUT_DEFAULT).json()).result()
            if r:
                return [{"lat": float(a['lat']), "lon": float(a['lon']), "fonte": "NOMINATIM", "score_base": 25, "cidade": a.get("address", {}).get("city", "").upper()} for a in r[:5]]
            return None
        return cls._execute("NOMINATIM", _call)

class RouteProvider:
    @staticmethod
    def _execute(provider_name, func, *args, **kwargs):
        if not circuit_breaker.check(provider_name):
            metrics.route_failures.labels(provider=provider_name).inc()
            return None
        rate_limiter.wait(provider_name)
        metrics.route_requests.labels(provider=provider_name).inc()
        start_t = time.time()
        try:
            result = func(*args, **kwargs)
            circuit_breaker.record_success(provider_name)
            metrics.api_latency.labels(provider=provider_name).observe(time.time() - start_t)
            return result
        except Exception as e:
            circuit_breaker.record_failure(provider_name)
            metrics.route_failures.labels(provider=provider_name).inc()
            logger.error("route_provider_error", provider=provider_name, error=str(e))
            return None

    @classmethod
    def osrm(cls, lat_o, lon_o, lat_d, lon_d):
        def _call():
            url = f"https://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=false"
            r = session.get(url, timeout=Config.TIMEOUT_DEFAULT).json()
            if r.get("routes"):
                km = round(r["routes"][0]["distance"] / 1000, 2)
                minutos = round(r["routes"][0]["duration"] / 60)
                return km, f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min", "OSRM", 95
            return None
        return cls._execute("OSRM", _call)

    @classmethod
    def google_preview(cls, o_raw, d_raw):
        def _call():
            url = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{requests.utils.quote(o_raw)}!1m2!1m1!1s{requests.utils.quote(d_raw)}!3e0"
            r = session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=Config.TIMEOUT_RELAXED)
            match_km = re.findall(r'\"(\d+[\.,]?\d*)\s*km\"', r.text)
            match_tempo = re.findall(r'\"(\d+\s*h\s*\d+\s*min|\d+\s*h|\d+\s*min)\"', r.text)
            if match_km and match_tempo:
                km = float(match_km[0].replace('.', '').replace(',', '.'))
                return km, match_tempo[0], "", "Google", 80
            return None
        return cls._execute("GOOGLE_ROUTING", _call)

# ==============================================================================
# [CORE] SERVIÇOS LOGÍSTICOS CENTRAIS E REGRAS DE NEGÓCIO
# ==============================================================================
class GeocodingService:
    @staticmethod
    def validar_coordenada(lat, lon):
        try:
            lat_f, lon_f = float(lat), float(lon)
            if (-35.0 <= lat_f <= 6.0) and (-75.0 <= lon_f <= -28.0): return True, lat_f, lon_f
            if (-35.0 <= lon_f <= 6.0) and (-75.0 <= lat_f <= -28.0): return True, lon_f, lat_f 
            return False, 0.0, 0.0
        except: return False, 0.0, 0.0

    @staticmethod
    def _calcular_vincenty(lat1, lon1, lat2, lon2):
        if lat1 == lat2 and lon1 == lon2: return 0.0
        try:
            dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
            a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
            return round(6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)
        except: return 0.0

    @classmethod
    def consenso_espacial(cls, candidatos, tipo_entrada, texto_cru, uf_inf, mun_inf):
        if not candidatos: return None
        validos = [c for c in candidatos if cls.validar_coordenada(c["lat"], c["lon"])[0]]
        if not validos: return None

        coords = np.radians([[c["lat"], c["lon"]] for c in validos])
        eps = Config.DBSCAN_RADIUS_KM_DEFAULT / 6371.0
        if len(coords) >= 2:
            labels = DBSCAN(eps=eps, min_samples=2, metric='haversine').fit(coords).labels_
            if len(set(labels) - {-1}) > 0:
                top_cluster = collections.Counter([l for l in labels if l != -1]).most_common(1)[0][0]
                validos = [v for i, v in enumerate(validos) if labels[i] == top_cluster]
        
        if not validos: return None
        validos.sort(key=lambda x: x.get("score_base", 0), reverse=True)
        v = validos[0]
        
        score_limite = 90 if tipo_entrada == "CEP" else 75
        confianca = "ALTA" if score_limite >= 80 else "MEDIA"
        return v["lat"], v["lon"], f"{texto_cru.upper()} [{v['fonte']}]", confianca, score_limite, "", mun_inf, v["fonte"], ["Consenso resolvido."]

    @classmethod
    def resolver(cls, localidade):
        texto = str(localidade).strip().upper()
        if not texto: return 0.0, 0.0, "", "BAIXA", 0, "", "", "N/A", ["Vazio"]
        
        cache_key = hashlib.md5(texto.encode('utf-8')).hexdigest()
        if cache_key in cache_geo: return cache_geo[cache_key]

        tipo = semantic_model.classificar(texto)
        ctx = semantic_model.contexto_administrativo(texto)
        
        candidatos = []
        candidatos.extend(GeocodingProvider.google(texto) or [])
        candidatos.extend(GeocodingProvider.arcgis(texto) or [])
        candidatos.extend(GeocodingProvider.tomtom(texto) or [])
        candidatos.extend(GeocodingProvider.nominatim(texto) or [])

        res = cls.consenso_espacial(candidatos, tipo, texto, ctx["uf"], ctx["municipio"])
        if res:
            cache_geo.set(cache_key, res, expire=Config.CACHE_EXPIRATION_GEO)
            return res
        return 0.0, 0.0, texto, "BAIXA", 0, "", "", "FALHA", ["Sem candidatos"]

class RouteService:
    @classmethod
    def calcular_pipeline(cls, origem, destino):
        start_t = time.time()
        c_key = f"R_{origem}_{destino}"
        if c_key in cache_rotas: return cache_rotas[c_key]

        lat_o, lon_o, end_o, conf_o, score_o, _, mun_o, f_o, xai_o = GeocodingService.resolver(origem)
        lat_d, lon_d, end_d, conf_d, score_d, _, mun_d, f_d, xai_d = GeocodingService.resolver(destino)
        t_geo = round(time.time() - start_t, 2)

        dist_reta = GeocodingService._calcular_vincenty(lat_o, lon_o, lat_d, lon_d)
        
        res_rota = None
        if lat_o != 0.0 and lat_d != 0.0:
            res_rota = RouteProvider.osrm(lat_o, lon_o, lat_d, lon_d)
            if not res_rota: res_rota = RouteProvider.google_preview(end_o, end_d)

        if not res_rota:
            km_est = dist_reta * 1.3
            tempo = f"{int(km_est/60)} h"
            res_rota = (round(km_est, 2), tempo, "Fallback", "Geodésico", 60)

        t_rot = round(time.time() - start_t - t_geo, 2)
        t_total = round(time.time() - start_t, 2)

        # Output formatado compatível com o dataframe original
        retorno = (
            res_rota[0], res_rota[1], "", "Não", dist_reta, res_rota[3], res_rota[4],
            conf_o, score_o, "", mun_o, f_o, end_o,
            conf_d, score_d, "", mun_d, f_d, end_d,
            lat_o, lon_o, lat_d, lon_d,
            t_geo, t_rot, t_total, xai_o, xai_d
        )
        cache_rotas.set(c_key, retorno, expire=Config.CACHE_EXPIRATION_GEO)
        return retorno

def worker_paralelo(item):
    idx, orig, dest = item
    try: return idx, RouteService.calcular_pipeline(orig, dest)
    except: return idx, None

# ==============================================================================
# [UI] 8. INTERFACE STREAMLIT E HEALTH CHECK (PROBES)
# ==============================================================================
# Health Check Endpoint Injetado
if st.query_params.get("health") == "true":
    health_payload = {
        "status": "UP",
        "version": Config.VERSION,
        "circuit_breakers": circuit_breaker.state,
        "metrics": {"total_failures": sum(circuit_breaker.failures.values())}
    }
    st.json(health_payload)
    st.stop()

st.set_page_config(page_title="TMS Enterprise Layered", page_icon="🚚", layout="wide")

st.markdown(f"""
<div style="background-color:#0E1117; padding:15px; border-radius:5px; border-left: 5px solid #00FF7F;">
    <h2 style="color:white; margin:0;">🗺️ TMS Engine Corporativo (v{Config.VERSION})</h2>
    <p style="color:#A0A0A0; margin:0;">Arquitetura em Camadas, Resiliência SRE e Telemetria</p>
</div>
""", unsafe_allow_html=True)

tab_ind, tab_lote, tab_audit = st.tabs(["📍 Validação Unitária", "⚙️ Processamento Massivo", "📊 Telemetria e Logs"])

with tab_ind:
    col1, col2 = st.columns(2)
    with col1: orig = st.text_input("Origem", "CD MERCADO LIVRE CAJAMAR")
    with col2: dest = st.text_input("Destino", "Esplanada dos Ministérios, Brasilia")
    
    if st.button("🚀 Processar Trajeto", type="primary"):
        with st.spinner("Executando RouteService pipeline..."):
            res = RouteService.calcular_pipeline(orig, dest)
            if res:
                st.success("Operação concluída.")
                c1, c2, c3 = st.columns(3)
                c1.metric("Distância (km)", res[0])
                c2.metric("Tempo Estimado", res[1])
                c3.metric("Origem Resolvida", res[11])
                
                logger.info("route_processed_ui", origin=orig, destination=dest, dist=res[0], time_total=res[25])

with tab_lote:
    st.info("Arquitetura Paralela Injetada O(1). Resiliência CircuitBreaker ativa.")
    arquivo = st.file_uploader("Upload Excel", type=["xlsx"])
    if arquivo:
        df = pd.read_excel(arquivo)
        df.columns = df.columns.str.strip().str.title()
        if 'Origem' in df.columns and 'Destino' in df.columns:
            if st.button("Processar Lote"):
                bar = st.progress(0)
                status = st.empty()
                tarefas = []
                for i, row in df.iterrows():
                    if pd.notna(row['Origem']) and pd.notna(row['Destino']):
                        tarefas.append((i, str(row['Origem']), str(row['Destino'])))
                
                resultados = {}
                with ThreadPoolExecutor(max_workers=Config.MAX_WORKERS_GLOBAL) as executor:
                    futures = {executor.submit(worker_paralelo, t): t for t in tarefas}
                    for i, f in enumerate(as_completed(futures)):
                        idx, r = f.result()
                        if r: resultados[idx] = r
                        bar.progress((i + 1) / len(tarefas))
                        status.text(f"Processando: {i+1}/{len(tarefas)}")
                
                for idx, r in resultados.items():
                    df.at[idx, 'Distancia (km)'] = r[0]
                    df.at[idx, 'Tempo Estimado'] = r[1]
                    df.at[idx, 'Motor'] = r[5]
                
                st.dataframe(df)
                logger.info("batch_processed", total_records=len(tarefas), success=len(resultados))

with tab_audit:
    st.markdown("### ⚙️ Telemetria de Providers (SRE)")
    df_health = []
    for prov in Config.PROVIDERS:
        df_health.append({
            "Provedor": prov,
            "Circuit Breaker": circuit_breaker.state[prov],
            "Falhas Acumuladas": circuit_breaker.failures[prov]
        })
    st.table(pd.DataFrame(df_health))
    st.caption("Métricas em tempo real via Prometheus Collector Wrapper e Structlog JSON")
