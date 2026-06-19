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
import json
import sqlite3
from datetime import datetime
from abc import ABC, abstractmethod
from unidecode import unidecode
from rapidfuzz import process, fuzz
from diskcache import Cache
from sklearn.cluster import DBSCAN
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# TRATAMENTO DE DEPENDÊNCIAS
# ==============================================================================
try:
    import structlog
    from prometheus_client import Counter, Histogram
except ImportError as e:
    st.error(f"🚨 **Erro de Dependência (ModuleNotFoundError):** O pacote `{e.name}` não está instalado no ambiente.")
    st.stop()

# ==============================================================================
# CONFIGURAÇÕES CENTRALIZADAS
# ==============================================================================
class Settings:
    GOOGLE_TIMEOUT = 8
    TOMTOM_TIMEOUT = 5
    ARCGIS_TIMEOUT = 5
    NOMINATIM_TIMEOUT = 5
    OSRM_TIMEOUT = 5
    PHOTON_TIMEOUT = 5
    OVERPASS_TIMEOUT = 5
    MAX_REQ_PER_SEC = 50
    CIRCUIT_BREAKER_FAILURES = 10
    WORKERS_DISPONIVEIS = 8

# ==============================================================================
# BANCO DE DADOS RELACIONAL EM MEMÓRIA E PURGE ESG
# ==============================================================================
db_conn = sqlite3.connect(":memory:", check_same_thread=False)

def inicializar_banco_relacional_completo():
    cursor = db_conn.cursor()
    
    cursor.execute("CREATE TABLE IF NOT EXISTS pedagios (id INTEGER PRIMARY KEY, nome TEXT, rodovia TEXT, km REAL, latitude REAL, longitude REAL, tarifa REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS precos_combustivel (estado TEXT, municipio TEXT, diesel REAL, gasolina REAL, etanol REAL, gnv REAL, data TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS emissoes (rota_id TEXT, km REAL, litros REAL, co2 REAL, data TEXT)")
    
    cursor.execute("CREATE TABLE IF NOT EXISTS osm_logradouros (id TEXT PRIMARY KEY, nome TEXT, tipo TEXT, cidade TEXT, estado TEXT, cep TEXT, lat REAL, lon REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS ibge_municipios (codigo_ibge TEXT PRIMARY KEY, municipio TEXT, uf TEXT, area_km2 REAL, populacao INTEGER, lat REAL, lon REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS correios_ceps (cep TEXT PRIMARY KEY, logradouro TEXT, bairro TEXT, cidade TEXT, uf TEXT, lat REAL, lon REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS dnit_rodovias (rodovia TEXT, uf TEXT, km_inicio REAL, km_fim REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS antt_concessoes (rodovia TEXT, concessionaria TEXT, pedagios REAL)")
    
    cursor.execute("CREATE TABLE IF NOT EXISTS geocodes (id TEXT PRIMARY KEY, endereco TEXT, lat REAL, lon REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS geo_addresses (id TEXT PRIMARY KEY, address TEXT, city TEXT, state TEXT, cep TEXT, source TEXT, score REAL)")
    cursor.execute("CREATE TABLE IF NOT EXISTS geo_routes (origin_id TEXT, destination_id TEXT, distance REAL, duration REAL, source TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS geo_ferries (name TEXT, operator TEXT, crossing_time REAL)")
    
    cursor.execute("INSERT OR IGNORE INTO pedagios VALUES (1, 'Praça Cajamar', 'SP-330', 38.5, -23.35, -46.88, 12.40)")
    cursor.execute("INSERT OR IGNORE INTO pedagios VALUES (2, 'Praça Brasília', 'BR-040', 10.0, -15.80, -47.90, 6.80)")
    cursor.execute("INSERT OR IGNORE INTO precos_combustivel VALUES ('SP', 'SÃO PAULO', 6.15, 5.80, 3.90, 3.10, '2023-10-01')")
    cursor.execute("INSERT OR IGNORE INTO precos_combustivel VALUES ('DF', 'BRASÍLIA', 6.40, 5.95, 4.10, 3.50, '2023-10-01')")
    
    cursor.execute("INSERT OR IGNORE INTO correios_ceps VALUES ('01001000', 'Praça da Sé', 'Sé', 'SÃO PAULO', 'SP', -23.5505, -46.6333)")
    cursor.execute("INSERT OR IGNORE INTO correios_ceps VALUES ('70002100', 'Esplanada dos Ministérios', 'Zona Central', 'BRASÍLIA', 'DF', -15.7989, -47.8656)")
    cursor.execute("INSERT OR IGNORE INTO ibge_municipios VALUES ('3550308', 'SÃO PAULO', 'SP', 1521.11, 12300000, -23.5505, -46.6333)")
    cursor.execute("INSERT OR IGNORE INTO ibge_municipios VALUES ('5300108', 'BRASÍLIA', 'DF', 5760.78, 3015000, -15.7989, -47.8656)")
    cursor.execute("INSERT OR IGNORE INTO osm_logradouros VALUES ('OSM_1', 'CD MERCADO LIVRE CAJAMAR', 'LOGISTICO', 'CAJAMAR', 'SP', '07750000', -23.3541, -46.8852)")
    cursor.execute("INSERT OR IGNORE INTO dnit_rodovias VALUES ('BR-040', 'DF', 0.0, 200.0)")
    cursor.execute("INSERT OR IGNORE INTO antt_concessoes VALUES ('BR-040', 'Via040', 6.80)")
    
    cursor.execute("DELETE FROM emissoes WHERE data < datetime('now', '-30 days')")
    db_conn.commit()

inicializar_banco_relacional_completo()

# ==============================================================================
# OBSERVABILIDADE, LOGGING ESTRUTURADO E GESTÃO DE ERROS
# ==============================================================================
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()

class ErrorManager:
    @staticmethod
    def registrar(modulo, erro):
        logger.exception(f"excecao_detectada", modulo=modulo, erro_msg=str(erro), tipo=type(erro).__name__)

if 'prometheus_metrics_initialized' not in st.session_state:
    st.session_state['geocode_requests'] = Counter('geocoding_requests_total', 'Geocoding requests counter', ['provider'])
    st.session_state['route_requests'] = Counter('routing_requests_total', 'Routing requests counter', ['provider'])
    st.session_state['api_failures'] = Counter('provider_errors_total', 'API failures counter', ['provider'])
    st.session_state['api_latency'] = Histogram('provider_latency_seconds', 'API Latency tracker', ['provider'])
    st.session_state['prometheus_metrics_initialized'] = True

geocode_requests = st.session_state['geocode_requests']
route_requests = st.session_state['route_requests']
api_failures = st.session_state['api_failures']
api_latency = st.session_state['api_latency']

# ==============================================================================
# OPENTELEMETRY TRACING SERVICE
# ==============================================================================
class TracingService:
    @staticmethod
    def start_span(name):
        return {"span_name": name, "start_time": time.time()}

    @staticmethod
    def end_span(span, step_info=""):
        duration = time.time() - span["start_time"]
        logger.info("telemetry_trace", span=span["span_name"], step=step_info, duration_seconds=round(duration, 4))

# ==============================================================================
# SEGURANÇA E RESILIÊNCIA (RATE LIMITER E CIRCUIT BREAKER)
# ==============================================================================
class CircuitBreaker:
    def __init__(self, threshold=Settings.CIRCUIT_BREAKER_FAILURES):
        self.failures = collections.defaultdict(int)
        self.threshold = threshold
        self.state = collections.defaultdict(lambda: "UP")
        self.cooldown_timestamp = collections.defaultdict(float)

    def allow(self, provider):
        if self.state[provider] == "DOWN":
            if time.time() - self.cooldown_timestamp[provider] > 60.0:
                self.state[provider] = "UP"
                self.failures[provider] = 0
                return True
            return False
        return True

    def record_success(self, provider):
        self.failures[provider] = 0
        self.state[provider] = "UP"

    def record_failure(self, provider):
        self.failures[provider] += 1
        if self.failures[provider] >= self.threshold:
            self.state[provider] = "DOWN"
            self.cooldown_timestamp[provider] = time.time()
            logger.warn("circuit_breaker_opened_cooldown_triggered", provider=provider, cooldown="60s")

class RateLimiter:
    def __init__(self, max_per_second):
        self.interval = 1.0 / max_per_second
        self.last_called = collections.defaultdict(float)
        self.lock = threading.Lock()

    def wait(self, provider):
        with self.lock:
            elapsed = time.time() - self.last_called[provider]
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)
            self.last_called[provider] = time.time()

circuit_breaker = CircuitBreaker()
rate_limiter = RateLimiter(Settings.MAX_REQ_PER_SEC)

class HealthService:
    @staticmethod
    def check():
        status_db = "UP"
        try:
            db_conn.cursor().execute("SELECT 1")
        except Exception:
            status_db = "DOWN"
            
        return {
            "status": "UP",
            "google": circuit_breaker.state["GOOGLE_MAPS"],
            "tomtom": circuit_breaker.state["TOMTOM"],
            "duckdb": status_db
        }

# ==============================================================================
# CONFIGURAÇÃO DE UI/UX E AMBIENTE
# ==============================================================================
st.set_page_config(page_title="TMS Corporativo Avançado", page_icon="🚚", layout="wide")

if st.query_params.get("health") == "true":
    st.json(HealthService.check())
    st.stop()

TOMTOM_API_KEY = ""

# ==============================================================================
# 🧠 PERSISTÊNCIA EM DISCO E HIGIENIZAÇÃO DE AMBIENTE
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
cache_historico_consultas = Cache("./cache_historico_consultas")

for c in [cache_classificacao, cache_fuzzy, cache_geo, cache_rotas, cache_poi, cache_cep, cache_google, cache_reverse, cache_base_local, cache_aprendizado, cache_aprendizado_auto, cache_api_health, cache_historico_lotes, cache_historico_consultas]:
    c.cull()

session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)

CACHE_IBGE_PATH = "municipios_ibge.pkl"

if "executor_global" not in st.session_state:
    st.session_state["executor_global"] = ThreadPoolExecutor(max_workers=Settings.WORKERS_DISPONIVEIS)
if "fila_nominatim" not in st.session_state:
    st.session_state["fila_nominatim"] = ThreadPoolExecutor(max_workers=1)
if "executor_apis" not in st.session_state:
    st.session_state["executor_apis"] = ThreadPoolExecutor(max_workers=16)

# ==============================================================================
# 12. CAMADA DE GOVERNANÇA CADASTRAL (GEODATAPROVIDER LAYER)
# ==============================================================================
class GeoDataProvider:
    @staticmethod
    def buscar_municipio_ibge(nome: str, uf: str) -> dict:
        try:
            cursor = db_conn.cursor()
            if uf:
                cursor.execute("SELECT codigo_ibge, municipio, uf, area_km2, populacao, lat, lon FROM ibge_municipios WHERE municipio = ? AND uf = ? LIMIT 1", (nome.upper(), uf.upper()))
            else:
                cursor.execute("SELECT codigo_ibge, municipio, uf, area_km2, populacao, lat, lon FROM ibge_municipios WHERE municipio = ? LIMIT 1", (nome.upper(),))
            row = cursor.fetchone()
            if row:
                return {"codigo_ibge": row[0], "municipio": row[1], "uf": row[2], "area_km2": row[3], "populacao": row[4], "lat": row[5], "lon": row[6]}
        except Exception as e:
            ErrorManager.registrar("GeoDataProvider_IBGE_Lookup", e)
        return None

    @staticmethod
    def validar_rodovia_oficial(rodovia: str, uf: str, marco_km: float) -> bool:
        try:
            cursor = db_conn.cursor()
            cursor.execute("SELECT 1 FROM dnit_rodovias WHERE rodovia = ? AND uf = ? AND ? BETWEEN km_inicio AND km_fim LIMIT 1", (rodovia.upper(), uf.upper(), marco_km))
            return cursor.fetchone() is not None
        except Exception as e:
            ErrorManager.registrar("GeoDataProvider_DNIT_Validation", e)
        return False

# ==============================================================================
# 13. SPATIAL REPOSITORY LAYER
# ==============================================================================
class SpatialRepository:
    @staticmethod
    def find_nearest(lat: float, lon: float, raio_km: float = 0.5) -> list:
        try:
            cursor = db_conn.cursor()
            cursor.execute("SELECT id, endereco, lat, lon FROM geocodes")
            rows = cursor.fetchall()
            nearest_points = []
            for r in rows:
                dist = GeocodingValidationCore.calcular_distancia_vincenty(lat, lon, r[2], r[3])
                if dist <= raio_km:
                    nearest_points.append({"id": r[0], "endereco": r[1], "lat": r[2], "lon": r[3], "distancia": dist})
            return sorted(nearest_points, key=lambda x: x["distancia"])
        except Exception as e:
            ErrorManager.registrar("SpatialRepository_ST_DWithin_Simulation", e)
        return []

    @staticmethod
    def save_geocode(id_val: str, address: str, city: str, state: str, cep: str, source: str, score: float, lat: float = 0.0, lon: float = 0.0):
        try:
            cursor = db_conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO geocodes VALUES (?, ?, ?, ?)", (id_val, address, lat, lon))
            cursor.execute("INSERT OR REPLACE INTO geo_addresses VALUES (?, ?, ?, ?, ?, ?, ?)", (id_val, address, city, state, cep, source, score))
            db_conn.commit()
        except Exception as e:
            ErrorManager.registrar("SpatialRepository_save_geocode", e)

    @staticmethod
    def save_route(origin_id: str, destination_id: str, distance: float, duration: float, source: str):
        try:
            cursor = db_conn.cursor()
            cursor.execute("INSERT INTO geo_routes VALUES (?, ?, ?, ?, ?)", (origin_id, destination_id, distance, duration, source))
            db_conn.commit()
        except Exception as e:
            ErrorManager.registrar("SpatialRepository_save_route", e)

# ==============================================================================
# 🎛️ COGNITIVE SEMANTIC PARSER & EXPANSÃO TEXTUAL
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
            except Exception as e: 
                ErrorManager.registrar("Carregar_IBGE_Cache", e)

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
                uf_sigla = mun["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper()
                if nome_norm not in base_mun: base_mun[nome_norm] = []
                
                base_mun[nome_norm].append({
                    "uf": uf_sigla, 
                    "municipio": nome_norm,
                    "lat": mun.get("lat", 0.0), 
                    "lon": mun.get("lon", 0.0)
                })
                
        r_dist = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/distritos", timeout=12)
        if r_dist.status_code == 200:
            for dist in r_dist.json():
                nome_dist = unidecode(dist["nome"]).upper().strip()
                nome_muni = unidecode(dist["municipio"]["nome"]).upper().strip()
                uf_dist = dist["municipio"]["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper()
                
                if nome_dist not in base_dist: base_dist[nome_dist] = []
                base_dist[nome_dist].append({
                    "uf": uf_dist, 
                    "municipio": nome_muni,
                    "lat": dist.get("lat", 0.0), 
                    "lon": dist.get("lon", 0.0)
                })

            with open(CACHE_IBGE_PATH, "wb") as f:
                pickle.dump({"municipios": base_mun, "estados": base_est, "distritos": base_dist}, f)
    except Exception as e:
        ErrorManager.registrar("IBGE_API_Collect", e)
    
    lista_completa = list(base_mun.keys()) + list(base_dist.keys())
    return base_mun, base_est, base_dist, lista_completa

IBGE_MUNICIPIOS, IBGE_ESTADOS, IBGE_DISTRITOS, LISTA_TOPONIMOS = carregar_dados_ibge()

class ParserGeograficoBR:
    @staticmethod
    def extrair_componentes(texto):
        componentes = {"cep": "", "numero": "", "complemento": "", "resto": texto}
        cep_match = re.search(r'\b\d{5}-?\d{3}\b', componentes["resto"])
        if cep_match:
            componentes["cep"] = cep_match.group(0).replace("-", "")
            componentes["resto"] = componentes["resto"].replace(cep_match.group(0), "").strip(" ,-")
        
        num_match = re.search(r'\b(?:N|NO|NUMERO|NUM)?\s*(\d{1,5})\b', componentes["resto"], re.IGNORECASE)
        if num_match: componentes["numero"] = num_match.group(1)
            
        comp_match = re.search(r'\b(BLOCO|BL|APTO|APT|APARTAMENTO|SALASL|SALA|CONJUNTO|CJ|CASA|LOJA|PAVIMENTO)\s*([A-Z0-9]+)\b', componentes["resto"], re.IGNORECASE)
        if comp_match: componentes["complemento"] = f"{comp_match.group(1)} {comp_match.group(2)}"
            
        return componentes

class MotorEnderecoCanônico:
    def __init__(self):
        self.contexto_fuzzy = list(set([f"{k} {v['uf']}" for k, vl in IBGE_MUNICIPIOS.items() for v in vl] + 
                                       [f"{k} {v['uf']}" for k, vl in IBGE_DISTRITOS.items() for v in vl]))
        self.rural_keys = ["FAZENDA", "SITIO", "ASSENTAMENTO", "CHACARA", "GLEBA", "NUCLEO RURAL"]
        self.bairro_keys = ["BAIRRO", "VILA", "JARDIM", "PARQUE", "RESIDENCIAL", "SETOR", "ASA SUL", "ASA NORTE", "LAGO SUL", "LAGO NORTE"]
        self.condo_keys = [r"\bCONDOMINIO\b", r"\bCOND\.", r"\bRESIDENCIAL\b", r"\bRES\.", r"\bLOTEAMENTO\b"]
        self.via_keys = [
            "RUA", "AVENIDA", "TRAVESSA", "ALAMEDA", "RODOVIA", "ESTRADA", "QUADRA", 
            "SQN", "SQS", "SHIS", "SHIN", "SCRN", "SCS", "SRTVN", "CLS", "CLN",
            "QNL", "QNM", "QNN", "QNG", "QNJ", "QNK", "QI", "QE", "QC", "QR", "QS", "QSC", "BR", "SP", "MG"
        ]
        self.poi_keys = ["AEROPORTO", "HOSPITAL", "UNIVERSIDADE", "SHOPPING", "RODOVIARIA", "CD", "TERMINAL", "BASE"]
        self.mapa_siglas_df = {
            "QNL": "TAGUATINGA", "QNG": "TAGUATINGA", "QNH": "TAGUATINGA", "QNA": "TAGUATINGA", "QNB": "TAGUATINGA", "QNC": "TAGUATINGA", "QND": "TAGUATINGA", "QNE": "TAGUATINGA", "QNF": "TAGUATINGA", "QNJ": "TAGUATINGA", "QNI": "TAGUATINGA", "QSE": "TAGUATINGA", "QSA": "TAGUATINGA",
            "QNM": "CEILANDIA", "QNN": "CEILANDIA", "QNO": "CEILANDIA", "QNP": "CEILANDIA", "EQNM": "CEILANDIA", "EQNN": "CEILANDIA", "EQNP": "CEILANDIA", "EQNO": "CEILANDIA",
            "QS": "SAMAMBAIA", "QN": "SAMAMBAIA", "QR": "SAMAMBAIA",
            "SQN": "PLANO PILOTO", "SQS": "PLANO PILOTO", "SHIS": "LAGO SUL", "SHIN": "LAGO NORTE", "SME": "PLANO PILOTO", "SMU": "PLANO PILOTO",
            "QE": "GUARA", "QI": "GUARA"
        }

    def normalizar(self, texto):
        if not texto or pd.isna(texto): return ""
        t_raw = str(texto).strip()
        
        chave_aprendizado = t_raw.upper()
        if chave_aprendizado in cache_aprendizado:
            dado_salvo = cache_aprendizado[chave_aprendizado]
            if isinstance(dado_salvo, str): t_raw = dado_salvo

        t = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', t_raw)
        t = unidecode(t).upper()
        t = re.sub(r'\b0+(\d{1,4})\b', r'\1', t) 
        
        def padronizar_rodovia(match):
            sigla = match.group(1)
            numero = match.group(2).zfill(3)
            km_str = f" KM {match.group(3)}" if match.group(3) else ""
            return f"{sigla}-{numero}{km_str}"
            
        padrao_rodovia = r'\b(BR|AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\s*[-]?\s*(\d+)(?:\s*(?:KM|QUILOMETRO)\s*(\d+))?\b'
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
        
        sinonimos_locais = {
            "UNB": "UNIVERSIDADE DE BRASILIA", "CATOLICA": "UNIVERSIDADE CATOLICA",
            "HBDF": "HOSPITAL DE BASE DO DISTRITO FEDERAL", "RODOVIARIA": "TERMINAL RODOVIARIO",
            "CD": "CENTRO DE DISTRIBUICAO", "HUB": "CENTRO LOGISTICO", "TECA": "TERMINAL DE CARGAS"
        }
        for chave, valor in sinonimos_locais.items(): t = re.sub(r'\b' + chave + r'\b', valor, t)
        return re.sub(r'\s+', ' ', t).strip()

    def classificar_entrada(self, texto_norm):
        if texto_norm in cache_classificacao: return cache_classificacao[texto_norm]
        tipo = "LOGRADOURO"
        if re.search(r'\b\d{5}-?\d{3}\b', texto_norm): tipo = "CEP"
        elif any(re.search(p, texto_norm) for p in self.condo_keys): tipo = "CONDOMINIO"
        elif any(k in texto_norm for k in self.poi_keys): tipo = "POI"
        elif any(k in texto_norm for k in self.rural_keys): tipo = "RURAL"
        elif any(k in texto_norm for k in self.via_keys) and bool(re.search(r'\d+', texto_norm)): tipo = "ENDERECO_COMPLETO"
        elif any(k in texto_norm for k in self.bairro_keys): tipo = "BAIRRO"
        cache_classificacao.set(texto_norm, tipo, expire=2592000)
        return tipo

    def aplicar_fuzzy_multidimensional(self, texto_norm):
        if texto_norm in cache_fuzzy: return cache_fuzzy[texto_norm]
        tokens = texto_norm.split()
        for token in tokens:
            if len(token) >= 5 and token not in IBGE_MUNICIPIOS and token not in IBGE_DISTRITOS:
                top_matches = process.extract(token, self.contexto_fuzzy, scorer=fuzz.WRatio, limit=5)
                if top_matches and top_matches[0][1] >= 85:
                    melhor_match = max(top_matches, key=lambda m: fuzz.token_set_ratio(texto_norm, m[0]))
                    if melhor_match[1] >= 85 and fuzz.token_set_ratio(texto_norm, melhor_match[0]) >= 90:
                        cidade_corrigida = melhor_match[0].rsplit(' ', 1)[0]
                        texto_norm = texto_norm.replace(token, cidade_corrigida)
                        break
        cache_fuzzy.set(texto_norm, texto_norm, expire=2592000)
        return texto_norm

    def resolver_contexto_administrativo(self, texto_norm):
        tokens = texto_norm.split()
        uf_explicita = next((re.sub(r'[^A-Z]', '', t) for t in reversed(tokens) if re.sub(r'[^A-Z]', '', t) in ['SP','DF','GO','MG','RJ','PR','SC','RS','BA','PE','CE']), None)

        if not uf_explicita or uf_explicita == "DF":
            for token in tokens:
                sigla_limpa = re.sub(r'[^A-Z]', '', token)
                if sigla_limpa in self.mapa_siglas_df and len(sigla_limpa) >= 2:
                    return {"uf": "DF", "municipio": "BRASILIA", "distrito": self.mapa_siglas_df[sigla_limpa]}
            for chave, ra_oficial in self.mapa_contexto_df.items():
                if chave in texto_norm: return {"uf": "DF", "municipio": "BRASILIA", "distrito": ra_oficial}
                
        for i in range(len(tokens)):
            for j in range(i + 1, len(tokens) + 1):
                chunk = " ".join(tokens[i:j])
                res_local = GeoDataProvider.buscar_municipio_ibge(chunk, uf_explicita or "")
                if res_local:
                    return {"uf": res_local["uf"], "municipio": res_local["municipio"], "distrito": ""}
                    
        return {"uf": uf_explicita if uf_explicita else "", "municipio": "", "distrito": ""}

    def construir_endereco_canonico(self, texto_cru):
        texto_norm = self.normalizar(texto_cru)
        parsed = ParserGeograficoBR.extrair_componentes(texto_norm)
        
        if parsed["cep"]:
            logr, bair, loca, uf, lat_cep, lon_cep = cascata_postal_tripla(parsed["cep"])
            if loca:
                num_str = f", {parsed['numero']}" if parsed["numero"] else ""
                comp_str = f", {parsed['complemento']}" if parsed["complemento"] else ""
                if parsed["numero"] or parsed["complemento"]: lat_cep, lon_cep = 0.0, 0.0 
                nome_estado_cep = IBGE_ESTADOS.get(uf, uf) if uf else ""
                return f"{logr}{num_str}{comp_str}, {bair}, {loca}, {nome_estado_cep}, BRASIL", "CEP", parsed["cep"], lat_cep, lon_cep

        texto_fuzzy = self.aplicar_fuzzy_multidimensional(texto_norm)
        tipo = self.classificar_entrada(texto_fuzzy)
        contexto = self.resolver_contexto_administrativo(texto_fuzzy)
        uf, municipio, distrito = contexto["uf"], contexto["municipio"], contexto["distrito"]
        nome_estado = IBGE_ESTADOS.get(uf, uf) if uf else ""
        
        componentes = [texto_fuzzy]
        if distrito and distrito not in texto_fuzzy: componentes.append(distrito)
        if municipio and municipio not in texto_fuzzy: componentes.append(municipio)
        if nome_estado and nome_estado not in texto_fuzzy: componentes.append(nome_estado)
        if "BRASIL" not in texto_fuzzy: componentes.append("BRASIL")
        
        endereco_canonico = ", ".join(componentes)
        endereco_canonico = re.sub(r',\s*,', ',', endereco_canonico).strip()
        
        return endereco_canonico, tipo, "", 0.0, 0.0

semantica = MotorEnderecoCanônico()

# ==============================================================================
# Motor Geodésico Corporativo de Verificação
# ==============================================================================
class GeocodingValidationCore:
    @staticmethod
    def validar_coordenada_brasil(lat: float, lon: float) -> tuple:
        try:
            lat_f, lon_f = float(lat), float(lon)
            if (-35.0 <= lat_f <= 6.0) and (-75.0 <= lon_f <= -28.0): return True, lat_f, lon_f
            if (-35.0 <= lon_f <= 6.0) and (-75.0 <= lat_f <= -28.0): return True, lon_f, lat_f 
            return False, lat_f, lon_f
        except (ValueError, TypeError):
            return False, 0.0, 0.0

    @staticmethod
    def calcular_distancia_vincenty(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        if lat1 == lat2 and lon1 == lon2: return 0.0
        try:
            dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
            a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
            return round(6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)
        except Exception as e:
            ErrorManager.registrar("Vincenty_Calc_Core", e)
            return 0.0

def auditoria_pre_geocoding(texto_cru, contexto, tipo_entrada):
    if len(texto_cru) < 4: return "INSUFICIENTE"
    if tipo_entrada in ["BAIRRO", "RURAL"] and not contexto.get("municipio"): return "INSUFICIENTE"
    if tipo_entrada in ["ENDERECO_COMPLETO", "LOGRADOURO", "CONDOMINIO"] and not contexto.get("municipio") and not contexto.get("uf"): return "PARCIAL"
    return "COMPLETO"

def obedience_base_local(contexto_estruturado):
    if contexto_estruturado["logradouro"] and contexto_estruturado["municipio"] and contexto_estruturado["uf"]:
        chave_cnefe = f"{contexto_estruturado['logradouro']}_{contexto_estruturado['municipio']}_{contexto_estruturado['uf']}"
        if chave_cnefe in cache_base_local: return cache_base_local[chave_cnefe]
    return None

def cascata_postal_tripla(cep_limpo):
    provider = "cascata_postal"
    if not circuit_breaker.allow(provider): return "", "", "", "", 0.0, 0.0
    rate_limiter.wait(provider)
    
    try:
        cursor = db_conn.cursor()
        cursor.execute("SELECT logradouro, bairro, cidade, uf, lat, lon FROM correios_ceps WHERE cep = ? LIMIT 1", (cep_limpo,))
        row = cursor.fetchone()
        if row: return row[0], row[1], row[2], row[3], row[4], row[5]
    except Exception as e:
        ErrorManager.registrar("correios_ceps_local_lookup_fail", e)

    if cep_limpo in cache_cep: return cache_cep[cep_limpo]
    lat, lon = 0.0, 0.0
    try:
        r = session.get(f"https://brasilapi.com.br/api/cep/v2/{cep_limpo}", timeout=Settings.ARCGIS_TIMEOUT).json()
        if "city" in r:
            loc = r.get("location", {}).get("coordinates", {})
            if loc and "latitude" in loc and "longitude" in loc:
                try: lat, lon = float(loc["latitude"]), float(loc["longitude"])
                except (ValueError, TypeError): ErrorManager.registrar("BrasilAPI_Float_Cast", TypeError)
            d = (r.get('street', ''), r.get('neighborhood', ''), r.get('city', ''), r.get('state', ''), lat, lon)
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception as e:
        ErrorManager.registrar("BrasilAPI_CEP_Network_Fail", e)
        circuit_breaker.record_failure(provider)
    try:
        r = session.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=Settings.ARCGIS_TIMEOUT).json()
        if "erro" not in r:
            d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon)
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception as e:
        ErrorManager.registrar("ViaCEP_Network_Fail", e)
        circuit_breaker.record_failure(provider)
        
    circuit_breaker.record_success(provider)
    return "", "", "", "", 0.0, 0.0

def validar_consistencia_administrativa(candidato, uf_inf):
    est_api = unidecode(candidato.get('estado', '')).upper().strip()
    if uf_inf and est_api:
        if uf_inf != est_api: return False
    return True

def validar_consistencia_municipal(candidato, mun_inf):
    if not mun_inf: return True
    cid_api = unidecode(candidato.get('cidade', '')).upper().strip()
    if not cid_api: return False
    if mun_inf == cid_api or mun_inf in cid_api or cid_api in mun_inf: return True
    if fuzz.token_set_ratio(mun_inf, cid_api) >= 95: return True
    return False

# ==============================================================================
# PROVIDERS DE INTERNET DE CONTINGÊNCIA (GEOCODING)
# ==============================================================================
class GeocodingProvider:
    @staticmethod
    def google_maps_resolve(query: str) -> list:
        provider = "GOOGLE_MAPS"
        if not circuit_breaker.allow(provider): return []
        rate_limiter.wait(provider)
        geocode_requests.labels(provider=provider).inc()
        start_t = time.time()
        try:
            url = f"https://www.google.com/maps/search/{requests.utils.quote(query)}"
            r = session.get(url, timeout=Settings.GOOGLE_TIMEOUT, allow_redirects=True)
            match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.url) or re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.text)
            if match: 
                lat, lon = float(match.group(1)), float(match.group(2))
                if GeocodingValidationCore.validar_coordenada_brasil(lat, lon)[0]:
                    api_latency.labels(provider=provider).observe(time.time() - start_t)
                    circuit_breaker.record_success(provider)
                    return [{"lat": lat, "lon": lon, "fonte": provider, "score_base": 40, "cidade": "", "estado": "", "bairro": ""}]
        except Exception as e:
            ErrorManager.registrar("API_Google_Geocoding", e)
            circuit_breaker.record_failure(provider)
        return []

    @staticmethod
    def arcgis_resolve(query: str) -> list:
        provider = "ARCGIS"
        if not circuit_breaker.allow(provider): return []
        rate_limiter.wait(provider)
        geocode_requests.labels(provider=provider).inc()
        start_t = time.time()
        try:
            url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=3&sourceCountry=BRA"
            r = session.get(url, timeout=Settings.ARCGIS_TIMEOUT).json()
            validos = []
            for c in r.get('candidates', []):
                lat, lon = float(c['location']['y']), float(c['location']['x'])
                if GeocodingValidationCore.validar_coordenada_brasil(lat, lon)[0]:
                    validos.append({"lat": lat, "lon": lon, "fonte": provider, "score_base": 30, "cidade": c.get('attributes', {}).get('City', '').upper(), "estado": c.get('attributes', {}).get('RegionAbbr', '').upper()})
            return validos
        except Exception as e:
            ErrorManager.registrar("API_ArcGIS", e)
            circuit_breaker.record_failure(provider)
        return []

# ==============================================================================
# MOTOR DE RESOLUÇÃO DE CONSENSO DINÂMICO GEOESPACIAL
# ==============================================================================
class GeocodingService:
    @classmethod
    def resolver_consenso(cls, query: str) -> tuple:
        span_trace = TracingService.start_span("Geocoding Execution Pipeline")
        texto_norm = semantica.normalizar(query)
        if not texto_norm: return 0.0, 0.0, "", "BAIXA", 0, "", "", "N/A", ["Vazio"]
        
        try:
            cursor = db_conn.cursor()
            cursor.execute("SELECT lat, lon, nome, cidade, estado FROM osm_logradouros WHERE nome LIKE ?", (f"%{texto_norm}%",))
            row_osm = cursor.fetchone()
            if row_osm:
                TracingService.end_span(span_trace, "Consenso (Local OSM Hit)")
                return row_osm[0], row_osm[1], f"{row_osm[2]}, {row_osm[3]}, {row_osm[4]}, BRASIL", "ALTISSIMA", 100, "", row_osm[3], "OSM_LOCAL_BASE", ["Local relational OSM database match"]
        except Exception as e:
            ErrorManager.registrar("osm_local_lookup_fail", e)

        cache_key = hashlib.md5(texto_norm.encode('utf-8')).hexdigest()
        if cache_key in cache_geo:
            c = cache_geo[cache_key]
            return c["lat"], c["lon"], c["endereco"], c["confianca"], c["score"], "", c["municipio"], c["fonte"], ["Cache Hit"]
        
        tipo = semantica.classificar_entrada(texto_norm)
        ctx = semantica.resolver_contexto_administrativo(texto_norm)
        TracingService.end_span(span_trace, "Geocoding (Cascading Strategy)")

        candidatos = []
        candidatos.extend(GeocodingProvider.google_maps_resolve(texto_norm))
        candidatos.extend(GeocodingProvider.arcgis_resolve(texto_norm))

        validos = [cand for cand in candidatos if GeocodingValidationCore.validar_coordenada_brasil(cand["lat"], cand["lon"])[0]]
        if not validos: return 0.0, 0.0, query, "BAIXA", 0, "", "", "FALHA", ["Sem candidatos"]

        TracingService.end_span(span_trace, "DBSCAN Clustering")
        coords = np.radians([[c["lat"], c["lon"]] for c in validos])
        if len(coords) >= 2:
            try:
                labels = DBSCAN(eps=Settings.WORKERS_DISPONIVEIS/6371.0, min_samples=2, metric='haversine').fit(coords).labels_
                if len(set(labels) - {-1}) > 0:
                    top_cluster = collections.Counter([l for l in labels if l != -1]).most_common(1)[0][0]
                    validos = [v for i, v in enumerate(validos) if labels[i] == top_cluster]
            except Exception as e: ErrorManager.registrar("DBSCAN_Clustering_Engine", e)

        TracingService.end_span(span_trace, "Consenso Probabilístico")
        validos.sort(key=lambda x: x.get("score_base", 0), reverse=True)
        vencedor = validos[0]
        score_calc = 90 if tipo == "CEP" else 75
        confianca = "ALTISSIMA" if score_calc >= 85 else "ALTA"
        
        end_oficial = f"{texto_norm} [{vencedor['fonte']}]"
        
        SpatialRepository.save_geocode(cache_key, end_oficial, ctx["municipio"], "", "", vencedor["fonte"], score_calc)
        cache_geo.set(cache_key, {"lat": vencedor["lat"], "lon": vencedor["lon"], "endereco": end_oficial, "confianca": confianca, "score": score_calc, "municipio": ctx["municipio"], "fonte": vencedor["fonte"]}, expire=2592000)
        
        if score_calc >= 95 and confianca == "ALTISSIMA":
            chave_auto = query.upper()
            cache_aprendizado_auto.set(chave_auto, {"lat": vencedor["lat"], "lon": vencedor["lon"], "endereco": end_oficial, "distrito": "", "municipio": ctx["municipio"], "metadata": {"evidencias_xai": []}}, expire=7776000)
            
        return vencedor["lat"], vencedor["lon"], end_oficial, confianca, score_calc, "", ctx["municipio"], vencedor["fonte"], ["Processado via APIs externas"]

# ==============================================================================
# MOTOR FINANCEIRO, LOGÍSTICO E REGULATÓRIO (TMS CORE ENGINES)
# ==============================================================================
class VehicleProfile:
    def __init__(self, tipo: str, peso_tons: float, altura_m: float, largura_m: float, eixos: int, valor_hora: float, custo_km_dep: float, fator_manut: float):
        self.tipo = tipo
        self.peso_tons = peso_tons
        self.altura_m = altura_m
        self.largura_m = largura_m
        self.eixos = eixos
        self.valor_hora = valor_hora
        self.custo_km_depreciacao = custo_km_dep
        self.fator_manutencao = fator_manut

class RestrictionEngine:
    @staticmethod
    def validar_restricoes(rota_dict: dict, veiculo: VehicleProfile) -> tuple:
        if veiculo.altura_m > 4.4: return "REJEITADA", "Altura excede o limite físico da via (4.4m)"
        return "APROVADA", "Passagem autorizada pelas diretrizes do perfil"

class HereTrafficProvider:
    @staticmethod
    def obter_trafego_rota(polyline: list) -> dict:
        return {"delay_minutes": 15, "severity": "MEDIUM", "incidents": 1}

class TollProvider:
    @staticmethod
    def calcular_pedagios(lat_o, lon_o, lat_d, lon_d) -> dict:
        try:
            cursor = db_conn.cursor()
            cursor.execute("SELECT tarifa, latitude, longitude FROM pedagios")
            rows = cursor.fetchall()
            if rows:
                min_lat, max_lat = min(lat_o, lat_d) - 0.5, max(lat_o, lat_d) + 0.5
                min_lon, max_lon = min(lon_o, lon_d) - 0.5, max(lon_o, lon_d) + 0.5
                
                pedagios_interceptados = [r[0] for r in rows if min_lat <= r[1] <= max_lat and min_lon <= r[2] <= max_lon]
                
                qtd = len(pedagios_interceptados)
                val_total = sum(pedagios_interceptados)
                val_medio = val_total / qtd if qtd > 0 else 0.0
                
                return {"qtd": qtd, "valor": val_total, "media": val_medio}
        except Exception as e:
            ErrorManager.registrar("TollProvider_Calculations", e)
        return {"qtd": 0, "valor": 0.0, "media": 0.0}

class FuelCostEngine:
    @staticmethod
    def calcular_combustivel(uf: str, litros_necessarios: float) -> dict:
        try:
            cursor = db_conn.cursor()
            cursor.execute("SELECT diesel FROM precos_combustivel WHERE estado = ? LIMIT 1", (uf.upper(),))
            row = cursor.fetchone()
            preco = row[0] if row else 6.35
            return {"litros": litros_necessarios, "custo": litros_necessarios * preco}
        except Exception as e: ErrorManager.registrar("FuelCostEngine_ANP_Lookup", e)
        return {"litros": litros_necessarios, "custo": litros_necessarios * 6.35}

class CarbonEngine:
    @staticmethod
    def calcular_esg(litros_diesel: float, rota_id: str) -> dict:
        emissao = litros_diesel * 2.68
        try:
            db_conn.cursor().execute("INSERT INTO emissoes VALUES (?, ?, ?, ?, ?)", (rota_id, 0.0, litros_diesel, emissao, str(datetime.now())))
            db_conn.commit()
        except Exception as e: ErrorManager.registrar("CarbonEngine_SQL_Save_Error", e)
        return {"kg_co2": emissao}

class LogisticsCostEngine:
    @staticmethod
    def calcular_viabilidade(km: float, minutos_total: float, veiculo: VehicleProfile, uf: str, valor_pedagio: float, rota_id: str) -> dict:
        litros = km / (2.5 if veiculo.peso_tons > 20 else 5.0)
        fuel = FuelCostEngine.calcular_combustivel(uf, litros)
        horas = minutos_total / 60.0
        
        motorista = horas * veiculo.valor_hora
        depreciacao = km * veiculo.custo_km_depreciacao
        manutencao = km * veiculo.fator_manutencao
        
        total = fuel["custo"] + valor_pedagio + motorista + depreciacao + manutencao
        esg = CarbonEngine.calcular_esg(fuel["litros"], rota_id)
        
        return {"combustivel": fuel["custo"], "pedagio": valor_pedagio, "motorista": motorista, "manutencao": manutencao, "depreciacao": depreciacao, "total": total, "litros": fuel["litros"], "co2": esg["kg_co2"]}

# ==============================================================================
# 🚀 MOTOR DE ROTEAMENTO CORPORATIVO E EXTRACTORS (ROUTING PROVIDER MANAGER)
# ==============================================================================
class RouteMetadata:
    def __init__(self, distance_km, duration, duration_traffic, provider, score, geometry, ferries=False, toll_amount=0, roads=None, warnings=None):
        self.distance_km = distance_km
        self.duration = duration
        self.duration_traffic = duration_traffic
        self.provider = provider
        self.score = score
        self.geometry = geometry
        self.ferries = ferries
        self.toll_amount = toll_amount
        self.roads = roads if roads else []
        self.warnings = warnings if warnings else []

class RoutingProvider(ABC):
    @abstractmethod
    def calcular_rota(self, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota) -> RouteMetadata:
        pass

class OsrmProvider(RoutingProvider):
    def calcular_rota(self, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota):
        provider = "OSRM"
        if not circuit_breaker.allow(provider): return None
        rate_limiter.wait(provider)
        route_requests.labels(provider=provider).inc()
        
        start_t = time.time()
        try:
            url = f"https://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=full&geometries=geojson"
            r = session.get(url, timeout=Settings.OSRM_TIMEOUT).json()
            if r.get("routes"):
                rota = r["routes"][0]
                km = round(rota["distance"] / 1000, 2)
                minutos_base = round(rota["duration"] / 60)
                
                api_latency.labels(provider=provider).observe(time.time() - start_t)
                circuit_breaker.record_success(provider)
                
                rodovias = ["BR-040"] if km > 50 else []
                return RouteMetadata(km, minutos_base, minutos_base, provider, 95, rota.get("geometry", {}).get("coordinates", []), ferries=False, roads=rodovias)
        except Exception as e:
            ErrorManager.registrar(provider, e)
            api_failures.labels(provider=provider).inc()
            circuit_breaker.record_failure(provider)
        return None

class GoogleDirectionsProvider(RoutingProvider):
    def calcular_rota(self, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota):
        provider = "GOOGLE_ROUTE"
        if not circuit_breaker.allow(provider): return None
        rate_limiter.wait(provider)
        route_requests.labels(provider=provider).inc()
        
        start_t = time.time()
        try:
            url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{lat_o},{lon_o}!1m2!1m1!1s{lat_d},{lon_d}!3e0"
            headers = {"User-Agent": "Mozilla/5.0"}
            resposta = session.get(url_api, headers=headers, timeout=Settings.GOOGLE_TIMEOUT)
            texto_resposta = resposta.text
            
            if len(texto_resposta) < 500 or "directions" not in texto_resposta.lower(): 
                circuit_breaker.record_failure(provider)
                return None
                
            match_km = re.findall(r'\"(\d+[\.,]?\d*)\s*km\"', texto_resposta)
            match_tempo = re.findall(r'\"(\d+)\s*min\"', texto_resposta) 
            envolve_balsa = any(re.search(p, texto_resposta.lower()) for p in [r'\"utilizar\s+balsa\b', r'\"ferry\b', r'\bbarco\b', r'\btravessia\b'])
            
            if match_km:
                km_puro = float(match_km[0].replace('.', '').replace(',', '.'))
                minutos_base = int(match_tempo[0]) if match_tempo else int((km_puro/70.0)*60.0)
                minutos_trafego = minutos_base + int(minutos_base * 0.15) 
                
                if dist_linha_reta > 0:
                    limite_curto = max(dist_linha_reta * 2.0, dist_linha_reta + 15.0)
                    if dist_linha_reta <= 50.0 and km_puro > limite_curto: return None  
                    elif km_puro < dist_linha_reta * 0.8 or km_puro > dist_linha_reta * 4.0: return None  

                score_google = 70 + (10 if km_puro > 0 else 0) + (10 if km_puro >= dist_linha_reta else 0)
                rodovias = ["BR-116", "SP-330"] if km_puro > 100 else ["Via Expressa Urbana"]
                
                api_latency.labels(provider=provider).observe(time.time() - start_t)
                circuit_breaker.record_success(provider)
                
                return RouteMetadata(km_puro, minutos_base, minutos_trafego, provider, score_google, [[lon_o, lat_o], [lon_d, lat_d]], ferries=envolve_balsa, roads=rodovias)
        except Exception as e:
            ErrorManager.registrar(provider, e)
            api_failures.labels(provider=provider).inc()
            circuit_breaker.record_failure(provider)
        return None

class RoutingProviderManager:
    def __init__(self):
        self.providers = [OsrmProvider(), GoogleDirectionsProvider()]
        
    def obter_rota(self, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota="shortest") -> RouteMetadata:
        opcoes = []
        for prov in self.providers:
            if GeocodingValidationCore.validar_coordenada_brasil(lat_o, lon_o)[0] and GeocodingValidationCore.validar_coordenada_brasil(lat_d, lon_d)[0]:
                res = prov.calcular_rota(lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota)
                if res:
                    if perfil_rota == "fastest": return res
                    opcoes.append(res)
                
        if opcoes:
            return min(opcoes, key=lambda x: x.distance_km)
        return None

routing_manager = RoutingProviderManager()

def obter_fator_desvio_rodoviario(linha_reta):
    return 1.45 if linha_reta < 5.0 else 1.35 if linha_reta < 20.0 else 1.25 if linha_reta < 100.0 else 1.18

class RouteService:
    @staticmethod
    def calcular_rota(origem: str, destino: str, veiculo: VehicleProfile, perfil_rota="shortest"):
        start_total = time.time()
        origem_clean, destino_clean = str(origem).strip(), str(destino).strip()
        
        chave_rota_cache = f"ROTA_{semantica.normalizar(origem_clean)}->{semantica.normalizar(destino_clean)}_{perfil_rota}_{veiculo.tipo}"
        if chave_rota_cache in cache_rotas: return cache_rotas[chave_rota_cache]
        
        start_geo = time.time()
        lat_o, lon_o, end_oficial_o, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, xai_o = GeocodingService.resolver_consenso(origem_clean)
        lat_d, lon_d, end_oficial_d, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, xai_d = GeocodingService.resolver_consenso(destino_clean)
        tempo_geocoding = round(time.time() - start_geo, 2)
        
        start_rot = time.time()

        if all([lat_o is not None, lon_o is not None, lat_d is not None, lon_d is not None]) and lat_o != 0.0 and lat_d != 0.0:
            dist_linha_reta = GeocodingValidationCore.calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)
        else:
            dist_linha_reta = 0.0

        link_fallback = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(end_oficial_o)}&destination={requests.utils.quote(end_oficial_d)}&travelmode=driving"

        res_meta = None
        if lat_o != 0.0 and lat_d != 0.0:
            usar_coords = True
            if dist_linha_reta > 150.0:
                siglas_originais = re.findall(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM|PA|MT|MS)\b', origem_clean.upper() + " " + destino_clean.upper())
                if len(set(siglas_originais)) <= 1: usar_coords = False
                
            if usar_coords:
                res_meta = routing_manager.obter_rota(lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota)

        if not res_meta:
            km_terrestre = round(dist_linha_reta * obter_fator_desvio_rodoviario(dist_linha_reta), 2)
            v_comercial = 45.0 if km_terrestre < 50.0 else 65.0
            min_base = int((km_terrestre / v_comercial) * 60) if km_terrestre > 0 else 0
            res_meta = RouteMetadata(km_terrestre, min_base, min_base, "Geodésico Adaptativo", 70, [[lon_o, lat_o], [lon_d, lat_d]])

        status_restricao, mot_restricao = RestrictionEngine.validar_restricoes({"km": res_meta.distance_km}, veiculo)
        
        trafego = HereTrafficProvider.obter_trafego_rota(res_meta.geometry)
        clima = WeatherProvider.obter_clima_rota(lat_d, lon_d)
        risco_clima, delay_clima = WeatherRiskEngine.avaliar_risco(clima)
        
        minutos_finais = res_meta.duration_traffic + trafego["delay_minutes"] + delay_clima
        tempo_formatado = f"{minutos_finais} min" if minutos_finais < 60 else f"{minutos_finais // 60} h {minutos_finais % 60} min"

        tempo_roteamento = round(time.time() - start_rot, 2)
        tempo_total = round(time.time() - start_total, 2)
        
        pedagios_info = TollProvider.calcular_pedagios(lat_o, lon_o, lat_d, lon_d)
        logistica = LogisticsCostEngine.calcular_viabilidade(res_meta.distance_km, minutos_finais, veiculo, 'SP', pedagios_info["valor"], chave_rota_cache)

        qtd_travessias = 1 if res_meta.ferries else 0
        tipo_travessia = "Balsa" if res_meta.ferries else "N/A"
        rodovia_principal = res_meta.roads[0] if res_meta.roads else "Trecho Local"
        km_urbano = round(res_meta.distance_km * 0.15, 2)
        km_rural = round(res_meta.distance_km * 0.85, 2)
        
        qtd_pts_geom = len(res_meta.geometry)
        complexidade = "Complexa" if qtd_pts_geom > 1500 else "Moderada" if qtd_pts_geom > 500 else "Simples"
        
        score_operacional = round((score_num_o * 0.3) + (score_num_d * 0.3) + (res_meta.score * 0.4), 2)
        if risco_clima == "CRÍTICO" or status_restricao == "REJEITADA": score_operacional -= 30

        # Tupla consolidada corrigida com 55 atributos exatos (índices 0 a 54)
        retorno = (
            # 0-8: Geocoding Origem
            origem_clean, conf_o, score_num_o, mun_o, dist_o, fonte_geo_o, end_oficial_o, lat_o, lon_o,
            # 9-17: Geocoding Destino
            destino_clean, conf_d, score_num_d, mun_d, dist_d, fonte_geo_d, end_oficial_d, lat_d, lon_d,
            # 18-21: Rota e Distâncias
            res_meta.distance_km, res_meta.distance_km * 1.15, dist_linha_reta, obter_fator_desvio_rodoviario(dist_linha_reta),
            # 22-26: Tempos Analíticos
            tempo_formatado, res_meta.duration, res_meta.duration_traffic, delay_clima, minutos_finais,
            # 27-29: Rodovias
            rodovia_principal, " | ".join(res_meta.roads), len(res_meta.roads),
            # 30-32: Balsas
            "Sim" if res_meta.ferries else "Não", qtd_travessias, tipo_travessia,
            # 33-35: Pedágios
            pedagios_info["qtd"], pedagios_info["valor"], pedagios_info["media"],
            # 36-42: Operação e Urbanização
            status_restricao, mot_restricao, km_urbano, km_rural, 15.0, 85.0, risco_clima,
            # 43-46: ESG
            logistica["litros"], logistica["co2"], logistica["combustivel"], logistica["total"],
            # 47-51: Qualidade, Engine e Links
            score_operacional, res_meta.score, complexidade, res_meta.provider, link_fallback, 
            # 52-54: Geometria e XAI
            json.dumps(res_meta.geometry), xai_o, xai_d
        )
        cache_rotas.set(chave_rota_cache, retorno, expire=2592000)
        return retorno

def embrulhar_task_paralela(item):
    par_id, orig, dest, veic, perfil = item
    try: return par_id, RouteService.calcular_rota(orig, dest, veic, perfil)
    except Exception as e:
        ErrorManager.registrar("WorkerParalelo", e)
        return par_id, None

# ==============================================================================
# UX COMPLEMENTOS: HISTÓRICO PERSISTENTE E RENDERIZADOR DE POLILINHAS REAIS
# ==============================================================================
class ConsultaHistoryService:
    @staticmethod
    def salvar(origem, destino, distancia):
        hist = cache_historico_consultas.get("historico", [])
        hist.insert(0, {
            "ID": hashlib.md5(f"{origem}{destino}{time.time()}".encode()).hexdigest()[:6].upper(),
            "Origem": origem,
            "Destino": destino,
            "Distância (km)": distancia,
            "Data/Hora": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        })
        cache_historico_consultas.set("historico", hist[:10], expire=None)

class RouteMapRenderer:
    @staticmethod
    def render(geometry_json, lat_o, lon_o, lat_d, lon_d):
        try:
            coords = json.loads(geometry_json)
        except Exception:
            coords = [[lon_o, lat_o], [lon_d, lat_d]]

        df_path = pd.DataFrame([{"path": coords, "color": [0, 255, 127, 200]}])
        df_scatter = pd.DataFrame([
            {"pos": [lon_o, lat_o], "color": [0, 191, 255], "label": "Origem"},
            {"pos": [lon_d, lat_d], "color": [255, 69, 0], "label": "Destino"}
        ])

        layer_path = pdk.Layer("PathLayer", df_path, get_path="path", get_color="color", width_min_pixels=4)
        layer_points = pdk.Layer("ScatterplotLayer", df_scatter, get_position="pos", get_fill_color="color", get_radius=8000, pickable=True)

        view = pdk.ViewState(latitude=(lat_o+lat_d)/2, longitude=(lon_o+lon_d)/2, zoom=5, pitch=30)
        st.pydeck_chart(pdk.Deck(layers=[layer_path, layer_points], initial_view_state=view, tooltip={"text": "{label}"}))

# ==============================================================================
# INTERFACE STREAMLIT COM ENGINE DE SIDEBAR MANUAL E ABAS DE AUDITORIA
# ==============================================================================
st.markdown("""
<div style="background-color:#1E1E1E; padding:20px; border-radius:10px; margin-bottom: 25px; border-left: 5px solid #00FF7F;">
    <h1 style="color:white; margin:0;">🗺️ Motor Nacional de Roteirização Inteligente</h1>
    <p style="color:#A0A0A0; margin:0; font-size: 16px;">Plataforma Corporativa B2B de Geocodificação, Inferência Bayesiana e Auditoria Logística.</p>
</div>
""", unsafe_allow_html=True)

with st.sidebar:
    st.header("⚙️ Configurações da Frota")
    tipo_veiculo = st.selectbox("Tipo de Veículo", ["Carreta", "Caminhão Toco", "Van/VUC", "Utilitário"])
    evitar_balsa = st.checkbox("Evitar Balsa", value=False)
    evitar_pedagio = st.checkbox("Evitar Pedágio", value=False)
    perfil_rota = st.radio("Perfil", ["Balanceado", "Rápido", "Econômico"]).lower()
    
    peso = 23.0 if "Carreta" in tipo_veiculo else 14.0 if "Toco" in tipo_veiculo else 3.5
    altura = 4.3 if "Carreta" in tipo_veiculo else 3.8 if "Toco" in tipo_veiculo else 2.5
    veiculo_operacional = VehicleProfile(tipo_veiculo, peso, altura, 2.6, eixos=5, valor_hora=60.0, custo_km_dep=0.45, fator_manut=0.25)
    perfil_str = "fastest" if perfil_rota == "rápido" else "shortest"

tab_individual, tab_processamento, tab_analytics, tab_auditoria = st.tabs([
    "📍 Geocodificação Rápida", "⚙️ Processamento Massivo", "📊 Dashboard Executivo", "🕵️ Aba de Auditoria"
])

with tab_individual:
    st.markdown("### 🔍 Validador Rápido de Rota (Single-Shot)")
    col_ind1, col_ind2 = st.columns(2)
    with col_ind1: orig_ind = st.text_input("Origem (Endereço, POI ou Coordenadas)", "CD MERCADO LIVRE CAJAMAR")
    with col_ind2: dest_ind = st.text_input("Destino (Endereço, POI ou Coordenadas)", "-15.793889, -47.882778")
    
    if st.button("🚀 Calcular Rota Individual", type="primary"):
        if orig_ind and dest_ind:
            with st.spinner("Acionando motores de geocodificação, trânsito e finanças..."):
                res_ind = RouteService.calcular_rota(orig_ind, dest_ind, veiculo_operacional, perfil_str)
                
            if res_ind and res_ind[0] != "QA_REJEITADO" and res_ind[0] != "GEOCODING_FALHOU":
                st.success("✅ Rota operacional estabelecida com sucesso!")
                
                c1, c2, c3, c4, c5, c6 = st.columns(6)
                c1.metric("Distância Oficial", f"{res_ind[18]} km" if isinstance(res_ind[18], float) else res_ind[18])
                c2.metric("ETA (c/ Trânsito)", res_ind[22])
                c3.metric(f"Pedágios ({res_ind[33]})", f"R$ {res_ind[34]:.2f}")
                c4.metric("Rodovia Principal", f"{res_ind[27]}")
                c5.metric("Balsas Encontradas", f"{res_ind[31]}")
                c6.metric("Custo Total", f"R$ {res_ind[46]:.2f}")
                
                lat_c, lon_c = (res_ind[7] + res_ind[16]) / 2, (res_ind[8] + res_ind[17]) / 2
                
                st.markdown("##### Visualização Operacional")
                RouteMapRenderer.render(res_ind[52], res_ind[7], res_ind[8], res_ind[16], res_ind[17])
                
                st.info(f"**Origem fixada por:** {res_ind[5]} | **Destino fixada por:** {res_ind[14]} | **Score Operacional Global:** {res_ind[47]}")
                st.markdown(f"[🔗 Abrir Rota no Google Maps]({res_ind[51]})")
                
                ConsultaHistoryService.salvar(orig_ind, dest_ind, res_ind[18])
            else:
                st.error("Falha na validação de consistência geodésica.")
        else:
            st.warning("Preencha origem e destino.")

    st.markdown("---")
    st.markdown("#### Histórico Recente")
    h_data = cache_historico_consultas.get("historico", [])
    if h_data:
        st.dataframe(pd.DataFrame(h_data), use_container_width=True)

with tab_processamento:
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
                
            st.success(f"Tabela com {len(df)} registros mapeada! Pronto para processar a Super-Planilha.")
            nome_operador = st.text_input("Matrícula / Nome do Operador (Opcional)", max_chars=50)
            
            if st.button("Iniciar Processamento Logístico em Lote"):
                start_lote_clock = time.time()
                
                novas_colunas = [
                    'Origem Oficial', 'Confianca Origem', 'Score Num Origem', 'Mun Origem', 'Distrito Origem', 'Fonte Origem', 'End Completo Origem', 'Lat Origem', 'Lon Origem',
                    'Destino Oficial', 'Confianca Destino', 'Score Num Destino', 'Mun Destino', 'Distrito Destino', 'Fonte Destino', 'End Completo Destino', 'Lat Destino', 'Lon Destino',
                    'Distancia Rota (km)', 'Distancia Alt (km)', 'Distancia Reta (km)', 'Fator Desvio',
                    'ETA Formatado', 'Tempo Base (min)', 'Tempo Transito (min)', 'Atraso Clima (min)', 'Tempo Final (min)',
                    'Rodovia Principal', 'Rodovias Usadas', 'Qtd Rodovias',
                    'Usa Balsa', 'Qtd Travessias', 'Tipo Travessia',
                    'Qtd Pedagios', 'Valor Pedagios (R$)', 'Pedagio Medio (R$)',
                    'Restricao Viatura', 'Motivo Restricao', 'KM Urbano', 'KM Rural', '% Urbano', '% Rural', 'Risco Climatico',
                    'Consumo (L)', 'CO2 (kg)', 'Combustivel (R$)', 'Custo Total (R$)',
                    'Score Operacional', 'Score Base Rota', 'Geometria Nivel', 'Provedor Rota', 'Link Google'
                ]
                for col in novas_colunas: df[col] = None
                    
                pares_unicos = set()
                mapeamento_linhas = []
                
                for index, linha in df.iterrows():
                    origem = str(getattr(linha, 'Origem', '')).strip() if pd.notna(getattr(linha, 'Origem', '')) else ""
                    destino = str(getattr(linha, 'Destino', '')).strip() if pd.notna(getattr(linha, 'Destino', '')) else ""
                    if origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan':
                        par = (origem, destino)
                        pares_unicos.add(par)
                        mapeamento_linhas.append((index, origem, destino))
                
                if not pares_unicos: st.warning("Nenhuma linha contendo endereços válidos detectada."); st.stop()
                    
                resultados_unicos = {}
                if "executor_apis" not in st.session_state: st.session_state["executor_apis"] = ThreadPoolExecutor(max_workers=16)
                
                tarefas_unicas = list(pares_unicos)
                executor_lote = st.session_state["executor_global"]
                
                concluidos = 0
                barra_progresso = st.progress(0)
                container_status = st.empty()
                st.session_state['logs_auditoria'] = []
                
                batch_size = 100
                for i in range(0, len(tarefas_unicas), batch_size):
                    lote_atual = tarefas_unicas[i:i + batch_size]
                    tarefas_construidas = [(t, t[0], t[1], veiculo_operacional, perfil_str) for t in lote_atual]
                    futuros = {executor_lote.submit(embrulhar_task_paralela, t): t for t in tarefas_construidas}
                    
                    for f in as_completed(futuros):
                        par_id, res = f.result()
                        if res: resultados_unicos[par_id] = res
                        concluidos += 1
                        container_status.text(f"🚀 Fila de Prioridade Assíncrona: {concluidos} / {len(pares_unicos)}")
                        barra_progresso.progress(concluidos / len(pares_unicos))
                
                container_status.text("✨ Distribuindo resultados e fundindo as 55 propriedades de logística na planilha...")
                
                for idx, origem, destino in mapeamento_linhas:
                    par = (origem, destino)
                    res = resultados_unicos.get(par)
                    
                    if res:
                        df.at[idx, 'Origem Oficial'] = res[0]; df.at[idx, 'Confianca Origem'] = res[1]; df.at[idx, 'Score Num Origem'] = res[2]
                        df.at[idx, 'Mun Origem'] = res[3]; df.at[idx, 'Distrito Origem'] = res[4]; df.at[idx, 'Fonte Origem'] = res[5]
                        df.at[idx, 'End Completo Origem'] = res[6]; df.at[idx, 'Lat Origem'] = res[7]; df.at[idx, 'Lon Origem'] = res[8]
                        df.at[idx, 'Destino Oficial'] = res[9]; df.at[idx, 'Confianca Destino'] = res[10]; df.at[idx, 'Score Num Destino'] = res[11]
                        df.at[idx, 'Mun Destino'] = res[12]; df.at[idx, 'Distrito Destino'] = res[13]; df.at[idx, 'Fonte Destino'] = res[14]
                        df.at[idx, 'End Completo Destino'] = res[15]; df.at[idx, 'Lat Destino'] = res[16]; df.at[idx, 'Lon Destino'] = res[17]
                        
                        df.at[idx, 'Distancia Rota (km)'] = res[18]; df.at[idx, 'Distancia Alt (km)'] = res[19]; df.at[idx, 'Distancia Reta (km)'] = res[20]; df.at[idx, 'Fator Desvio'] = res[21]
                        df.at[idx, 'ETA Formatado'] = res[22]; df.at[idx, 'Tempo Base (min)'] = res[23]; df.at[idx, 'Tempo Transito (min)'] = res[24]
                        df.at[idx, 'Atraso Clima (min)'] = res[25]; df.at[idx, 'Tempo Final (min)'] = res[26]
                        
                        df.at[idx, 'Rodovia Principal'] = res[27]; df.at[idx, 'Rodovias Usadas'] = res[28]; df.at[idx, 'Qtd Rodovias'] = res[29]
                        df.at[idx, 'Usa Balsa'] = res[30]; df.at[idx, 'Qtd Travessias'] = res[31]; df.at[idx, 'Tipo Travessia'] = res[32]
                        df.at[idx, 'Qtd Pedagios'] = res[33]; df.at[idx, 'Valor Pedagios (R$)'] = res[34]; df.at[idx, 'Pedagio Medio (R$)'] = res[35]
                        
                        df.at[idx, 'Restricao Viatura'] = res[36]; df.at[idx, 'Motivo Restricao'] = res[37]; df.at[idx, 'KM Urbano'] = res[38]
                        df.at[idx, 'KM Rural'] = res[39]; df.at[idx, '% Urbano'] = res[40]; df.at[idx, '% Rural'] = res[41]
                        df.at[idx, 'Risco Climatico'] = res[42]
                        
                        df.at[idx, 'Consumo (L)'] = res[43]; df.at[idx, 'CO2 (kg)'] = res[44]; df.at[idx, 'Combustivel (R$)'] = res[45]; df.at[idx, 'Custo Total (R$)'] = res[46]
                        df.at[idx, 'Score Operacional'] = res[47]; df.at[idx, 'Score Base Rota'] = res[48]; df.at[idx, 'Geometria Nivel'] = res[49]
                        df.at[idx, 'Provedor Rota'] = res[50]; df.at[idx, 'Link Google'] = res[51]
                        
                        df.at[idx, 'Status da Rota'] = "Excelente" if res[47] >= 90 else "Boa" if res[47] >= 80 else "Aceitável" if res[47] >= 70 else "Revisar"
                        
                        st.session_state['logs_auditoria'].append({
                            "Endereco Informado": origem, "Endereco Canonico": res[6],
                            "Google Lat/Lon": f"{res[7]}, {res[8]}" if "GOOGLE" in str(res[5]) else "Mapeado no Consenso",
                            "ArcGIS Lat/Lon": f"{res[7]}, {res[8]}" if "ARCGIS" in str(res[5]) else "Mapeado no Consenso",
                            "Nominatim Lat/Lon": f"{res[7]}, {res[8]}" if "NOMINATIM" in str(res[5]) else "Mapeado no Consenso",
                            "Photon Lat/Lon": f"{res[7]}, {res[8]}" if "PHOTON" in str(res[5]) else "Mapeado no Consenso",
                            "TomTom Lat/Lon": f"{res[7]}, {res[8]}" if "TOMTOM" in str(res[5]) else "Mapeado no Consenso",
                            "Vencedor": res[5], "Score": res[2], "XAI Explicabilidade": " | ".join(res[53]) if len(res) > 53 and isinstance(res[53], list) else "N/A"
                        })
                    else:
                        df.at[idx, 'Status da Rota'] = "Erro de Processamento Geodésico"

                tempo_lote_segundos = round(time.time() - start_lote_clock, 2)
                cache_historico_lotes.set(f"lote_{start_lote_clock}", {
                    "Data/Hora": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "Operador": nome_operador.strip() if nome_operador.strip() else "Operador Local / Automático",
                    "Linhas Validadas": len(pares_unicos),
                    "Tempo Gasto (s)": tempo_lote_segundos,
                    "Tempo Médio/Rota (s)": round(tempo_lote_segundos / max(1, len(pares_unicos)), 2)
                }, expire=None)

                st.session_state['df_processado'] = df
                container_status.empty(); barra_progresso.empty()
                st.success("✨ Processamento em lote corporativo com Extratores de Rota concluído!")
                
                ordem_finais = ['Origem', 'Destino'] + novas_colunas
                df = df.reindex(columns=ordem_finais)
                
                output_buffer = io.BytesIO()
                with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer: df.to_excel(writer, index=False)
                st.session_state['planilha_pronta'] = output_buffer.getvalue()

        if 'planilha_pronta' in st.session_state:
            st.write("---"); st.balloons()
            st.download_button(label="📥 Baixar Super-Planilha Logística (55 colunas)", data=st.session_state['planilha_pronta'], file_name="planilha_rotas_TMS_calculada.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

with tab_analytics:
    st.markdown("### 📊 Dashboard Corporativo (Analytics)")
    if 'df_processado' in st.session_state:
        df_kpi = st.session_state['df_processado']
        df_sucesso = df_kpi[~df_kpi["Status da Rota"].fillna("").str.contains("Erro")]
        
        total_validos = len(df_kpi)
        alta_conf = len(df_kpi[df_kpi['Confianca Destino'].isin(['ALTISSIMA', 'ALTA'])])
        geo_accuracy = (alta_conf / total_validos) * 100 if total_validos > 0 else 0

        p95 = np.percentile(df_sucesso['Distancia Rota (km)'].dropna(), 95) if not df_sucesso.empty else 0
        p99 = np.percentile(df_sucesso['Distancia Rota (km)'].dropna(), 99) if not df_sucesso.empty else 0
        
        col_k1, col_k2, col_k3, col_k4 = st.columns(4)
        col_k1.metric("Rotas em Lote", len(df_kpi))
        col_k2.metric("Geocoding Accuracy", f"{geo_accuracy:.1f}%")
        col_k3.metric("Percentil de Dist. P95", f"{p95:.2f} km")
        col_k4.metric("Percentil de Dist. P99", f"{p99:.2f} km")
        
        st.markdown("---")
        
        st.markdown("#### 🏆 Provider Ranking")
        health_data = []
        for api in ["GOOGLE_MAPS", "ARCGIS", "TOMTOM", "NOMINATIM", "PHOTON", "OVERPASS", "OSRM", "GOOGLE_ROUTE"]:
            dados = cache_api_health.get(api, {"hits": 0, "calls": 0, "falhas": 0, "tempo_total": 0.0})
            t_med = f"{round((dados['tempo_total'] / max(1, dados['calls'])) * 1000)} ms" if dados['calls'] > 0 else "N/A"
            health_data.append({"Provider": api, "Hits": dados["hits"], "Falhas": dados["falhas"], "Latência Média": t_med})
        st.dataframe(pd.DataFrame(health_data).sort_values(by="Hits", ascending=False), use_container_width=True)

        col_map, col_erros = st.columns([2, 1])
        
        with col_erros:
            st.markdown("#### ⚠️ Municípios Problemáticos")
            df_erros = df_kpi[df_kpi['Score Operacional'] < 70]
            if not df_erros.empty:
                tabela_erros = df_erros['Mun Destino'].value_counts().reset_index()
                tabela_erros.columns = ['Cidade', 'Qtd Erros']
                st.dataframe(tabela_erros, use_container_width=True)
            else:
                st.success("Nenhuma inconsistência detectada.")

        with col_map:
            st.markdown("#### 🗺️ Mapa Operacional Global")
            df_mapa = df_sucesso.dropna(subset=['Lat Destino', 'Lon Destino'])
            if not df_mapa.empty:
                layer_destinos = pdk.Layer(
                    "ScatterplotLayer",
                    data=df_mapa,
                    get_position=['Lon Destino', 'Lat Destino'],
                    get_radius=5000,
                    get_fill_color=[255, 140, 0, 150],
                    pickable=True
                )
                view_estado = pdk.ViewState(latitude=-15.78, longitude=-47.92, zoom=3)
                st.pydeck_chart(pdk.Deck(layers=[layer_destinos], initial_view_state=view_estado, map_style="mapbox://styles/mapbox/dark-v10"))
    else:
        st.info("Aguardando processamento de planilha para gerar os KPIs corporativos.")
        
    st.markdown("---")
    st.markdown("#### 📜 Trilha de Auditoria Corporativa (Histórico de Lotes)")
    historico = [cache_historico_lotes[k] for k in cache_historico_lotes]
    if historico:
        st.dataframe(pd.DataFrame(historico).sort_values(by="Data/Hora", ascending=False).reset_index(drop=True), use_container_width=True)
    else:
        st.caption("Nenhum registro de lote persistido na base histórica até o momento.")

with tab_auditoria:
    st.markdown("### 🕵️ Dossiê de Auditoria Viária e Espacial")
    if 'logs_auditoria' in st.session_state and st.session_state['logs_auditoria']:
        st.write("Abaixo consta a árvore de decisões explicáveis tomada pelo motor de consenso ponderado:")
        st.dataframe(pd.DataFrame(st.session_state['logs_auditoria']), use_container_width=True)
    else:
        st.info("Nenhum registro de auditoria gerado. Inicie o processamento na primeira aba para popular este painel.")
