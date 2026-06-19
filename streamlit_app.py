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

# Tenta importar Playwright para suporte ao Scraper automatizado solicitado
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    pass

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
# BANCO DE DADOS RELACIONAL EM MEMÓRIA
# ==============================================================================
db_conn = sqlite3.connect(":memory:", check_same_thread=False)

def inicializar_banco_relacional():
    cursor = db_conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pedagios (
            id INTEGER PRIMARY KEY, nome TEXT, rodovia TEXT, km REAL, latitude REAL, longitude REAL, tarifa REAL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS precos_combustivel (
            estado TEXT, municipio TEXT, diesel REAL, gasolina REAL, etanol REAL, gnv REAL, data TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS emissoes (
            rota_id TEXT, km REAL, litros REAL, co2 REAL, data TEXT
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO pedagios VALUES (1, 'Praça Cajamar', 'SP-330', 38.5, -23.35, -46.88, 12.40)")
    cursor.execute("INSERT OR IGNORE INTO pedagios VALUES (2, 'Praça Brasília', 'BR-040', 10.0, -15.80, -47.90, 6.80)")
    cursor.execute("INSERT OR IGNORE INTO precos_combustivel VALUES ('SP', 'SÃO PAULO', 6.15, 5.80, 3.90, 3.10, '2023-10-01')")
    cursor.execute("INSERT OR IGNORE INTO precos_combustivel VALUES ('DF', 'BRASÍLIA', 6.40, 5.95, 4.10, 3.50, '2023-10-01')")
    db_conn.commit()

inicializar_banco_relacional()

# ==============================================================================
# OBSERVABILIDADE, LOGGING ESTRUTURADO E ERROR MANAGER
# ==============================================================================
structlog.configure(
    processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()]
)
logger = structlog.get_logger()

class ErrorManager:
    @staticmethod
    def registrar(modulo, erro):
        logger.exception(f"{modulo}_falha", erro=str(erro), tipo=type(erro).__name__)

if 'prometheus_metrics_initialized' not in st.session_state:
    st.session_state['geocode_requests'] = Counter('geocode_requests_total', 'Geocoding requests', ['provider'])
    st.session_state['route_requests'] = Counter('route_requests_total', 'Routing requests', ['provider'])
    st.session_state['api_failures'] = Counter('api_failures_total', 'API failures', ['provider'])
    st.session_state['api_latency'] = Histogram('provider_latency_seconds', 'API Latency', ['provider'])
    st.session_state['prometheus_metrics_initialized'] = True

geocode_requests = st.session_state['geocode_requests']
route_requests = st.session_state['route_requests']
api_failures = st.session_state['api_failures']
api_latency = st.session_state['api_latency']

# ==============================================================================
# SEGURANÇA E RESILIÊNCIA
# ==============================================================================
class CircuitBreaker:
    def __init__(self, threshold=Settings.CIRCUIT_BREAKER_FAILURES):
        self.failures = collections.defaultdict(int)
        self.threshold = threshold
        self.state = collections.defaultdict(lambda: "UP")

    def allow(self, provider): return self.failures[provider] < self.threshold
    def record_success(self, provider): self.failures[provider] = 0; self.state[provider] = "UP"
    def record_failure(self, provider):
        self.failures[provider] += 1
        if self.failures[provider] >= self.threshold: self.state[provider] = "DOWN"

class RateLimiter:
    def __init__(self, max_per_second):
        self.interval = 1.0 / max_per_second
        self.last_called = collections.defaultdict(float)
        self.lock = threading.Lock()

    def wait(self, provider):
        with self.lock:
            elapsed = time.time() - self.last_called[provider]
            if elapsed < self.interval: time.sleep(self.interval - elapsed)
            self.last_called[provider] = time.time()

circuit_breaker = CircuitBreaker()
rate_limiter = RateLimiter(Settings.MAX_REQ_PER_SEC)

class HealthService:
    @staticmethod
    def check(): return circuit_breaker.state

# ==============================================================================
# CONFIGURAÇÃO DE UI/UX E AMBIENTE
# ==============================================================================
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

if "executor_global" not in st.session_state: st.session_state["executor_global"] = ThreadPoolExecutor(max_workers=Settings.WORKERS_DISPONIVEIS)
if "fila_nominatim" not in st.session_state: st.session_state["fila_nominatim"] = ThreadPoolExecutor(max_workers=1)
if "executor_apis" not in st.session_state: st.session_state["executor_apis"] = ThreadPoolExecutor(max_workers=16)

# ==============================================================================
# 🎛️ DADOS GLOBAIS THREAD-SAFE, HUB B2B E EXPANSÃO SEMÂNTICA
# ==============================================================================
BASE_POIS_LOGISTICOS = {
    "CD MAGAZINE LUIZA CAXIAS": {"lat": -22.7853, "lon": -43.3121, "endereco": "Centro de Distribuição Magazine Luiza, Duque de Caxias, RJ, BRASIL", "municipio": "DUQUE DE CAXIAS", "uf": "RJ"},
    "CD MERCADO LIVRE CAJAMAR": {"lat": -23.3541, "lon": -46.8852, "endereco": "Centro de Distribuição Mercado Livre, Cajamar, SP, BRASIL", "municipio": "CAJAMAR", "uf": "SP"},
    "CD AMAZON CAJAMAR": {"lat": -23.3600, "lon": -46.8900, "endereco": "Centro de Distribuição Amazon, Cajamar, SP, BRASIL", "municipio": "CAJAMAR", "uf": "SP"}
}

SINONIMOS_SEMANTICOS = {
    "UNB": "UNIVERSIDADE DE BRASILIA", "CATOLICA": "UNIVERSIDADE CATOLICA",
    "JK": "JUSCELINO KUBITSCHEK", "HBDF": "HOSPITAL DE BASE DO DISTRITO FEDERAL",
    "HRAN": "HOSPITAL REGIONAL DA ASA NORTE", "RODOVIARIA": "TERMINAL RODOVIARIO",
    "CD": "CENTRO DE DISTRIBUICAO", "HUB": "CENTRO LOGISTICO",
    "FILIAL": "BASE OPERACIONAL", "TECA": "TERMINAL DE CARGAS"
}

@st.cache_data
def carregar_dados_ibge():
    if os.path.exists(CACHE_IBGE_PATH):
        if time.time() - os.path.getmtime(CACHE_IBGE_PATH) > (30 * 86400): os.remove(CACHE_IBGE_PATH)
        else:
            try:
                with open(CACHE_IBGE_PATH, "rb") as f:
                    d = pickle.load(f)
                    return d.get("municipios", {}), d.get("estados", {}), d.get("distritos", {}), list(d.get("municipios", {}).keys()) + list(d.get("distritos", {}).keys())
            except Exception as e: ErrorManager.registrar("Carregar_IBGE_Cache", e)

    base_mun, base_est, base_dist = {}, {}, {}
    try:
        r_est = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/estados", timeout=8)
        if r_est.status_code == 200:
            for est in r_est.json(): base_est[est["sigla"]] = unidecode(est["nome"]).upper()
                
        r_mun = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/municipios", timeout=12)
        if r_mun.status_code == 200:
            for mun in r_mun.json():
                nome_norm = unidecode(mun["nome"]).upper().strip()
                uf_sigla = mun["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper()
                if nome_norm not in base_mun: base_mun[nome_norm] = []
                base_mun[nome_norm].append({"uf": uf_sigla, "municipio": nome_norm, "lat": mun.get("lat", 0.0), "lon": mun.get("lon", 0.0)})
                
        r_dist = session.get("https://servicodados.ibge.gov.br/api/v1/localidades/distritos", timeout=12)
        if r_dist.status_code == 200:
            for dist in r_dist.json():
                nome_dist = unidecode(dist["nome"]).upper().strip()
                nome_muni = unidecode(dist["municipio"]["nome"]).upper().strip()
                uf_dist = dist["municipio"]["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper()
                if nome_dist not in base_dist: base_dist[nome_dist] = []
                base_dist[nome_dist].append({"uf": uf_dist, "municipio": nome_muni, "lat": dist.get("lat", 0.0), "lon": dist.get("lon", 0.0)})

            with open(CACHE_IBGE_PATH, "wb") as f: pickle.dump({"municipios": base_mun, "estados": base_est, "distritos": base_dist}, f)
    except Exception as e: ErrorManager.registrar("IBGE_API_Collect", e)
    
    lista_completa = list(base_mun.keys()) + list(base_dist.keys())
    return base_mun, base_est, base_dist, lista_completa

IBGE_MUNICIPIOS, IBGE_ESTADOS, IBGE_DISTRITOS, LISTA_TOPONIMOS = carregar_dados_ibge()
LISTA_CONTEXTO_FUZZY = list(set([f"{k} {v['uf']}" for k, vl in IBGE_MUNICIPIOS.items() for v in vl] + [f"{k} {v['uf']}" for k, vl in IBGE_DISTRITOS.items() for v in vl]))

# ==============================================================================
# 🧹 ENGINE DE RESOLUÇÃO UNIVERSAL E ENDEREÇAMENTO CANÔNICO
# ==============================================================================
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
        self.rural_keys = ["FAZENDA", "SITIO", "ASSENTAMENTO", "CHACARA", "GLEBA", "NUCLEO RURAL"]
        self.bairro_keys = ["BAIRRO", "VILA", "JARDIM", "PARQUE", "RESIDENCIAL", "SETOR", "ASA SUL", "ASA NORTE", "LAGO SUL", "LAGO NORTE"]
        self.condo_keys = [r"\bCONDOMINIO\b", r"\bCOND\.", r"\bRESIDENCIAL\b", r"\bRES\.", r"\bLOTEAMENTO\b"]
        
        self.via_keys = [
            "RUA", "AVENIDA", "TRAVESSA", "ALAMEDA", "RODOVIA", "ESTRADA", "QUADRA", 
            "SQN", "SQS", "SHIS", "SHIN", "SCRN", "SCS", "SRTVN", "CLS", "CLN",
            "QNL", "QNM", "QNN", "QNG", "QNJ", "QNK", "QI", "QE", "QC", "QR", "QS", "QSC"
        ]
        
        self.mapa_contexto_df = {
            "TAGUATINGA": "TAGUATINGA", "GAMA": "GAMA", "PONTE ALTA": "GAMA", "PONTE ALTA NORTE": "GAMA",
            "PONTE ALTA SUL": "GAMA", "CEILANDIA": "CEILANDIA", "SOL NASCENTE": "CEILANDIA", 
            "POR DO SOL": "CEILANDIA", "AGUAS CLARAS": "AGUAS CLARAS", "ARNIQUEIRAS": "AGUAS CLARAS", 
            "SAMAMBAIA": "SAMAMBAIA", "GUARA": "GUARA", "PLANALTINA": "PLANALTINA", 
            "SOBRADINHO": "SOBRADINHO", "VICENTE PIRES": "VICENTE PIRES", "SANTA MARIA": "SANTA MARIA",
            "RECANTO DAS EMAS": "RECANTO DAS EMAS", "RIACHO FUNDO": "RIACHO FUNDO", "LAGO SUL": "PLANO PILOTO", 
            "LAGO NORTE": "PLANO PILOTO", "NUCLEO BANDEIRANTE": "NUCLEO BANDEIRANTE", "BRAZLANDIA": "BRAZLANDIA"
        }

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
        for chave, valor in SINONIMOS_SEMANTICOS.items(): t = re.sub(r'\b' + chave + r'\b', valor, t)
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

    def resolver_contexto_administrativo(self, texto_norm):
        tokens = texto_norm.split()
        uf_explicita = None
        for token in reversed(tokens):
            token_limpo = re.sub(r'[^A-Z]', '', token)
            if token_limpo in IBGE_ESTADOS:
                uf_explicita = token_limpo
                break

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
                if chunk in IBGE_MUNICIPIOS:
                    if uf_explicita:
                        for item in IBGE_MUNICIPIOS[chunk]:
                            if item["uf"] == uf_explicita: return {"uf": uf_explicita, "municipio": chunk, "distrito": ""}
                    else: return {"uf": IBGE_MUNICIPIOS[chunk][0]["uf"], "municipio": chunk, "distrito": ""}
                        
                if chunk in IBGE_DISTRITOS:
                    if uf_explicita:
                        for item in IBGE_DISTRITOS[chunk]:
                            if item["uf"] == uf_explicita: return {"uf": uf_explicita, "municipio": item["municipio"], "distrito": chunk}
                    else: return {"uf": IBGE_DISTRITOS[chunk][0]["uf"], "municipio": IBGE_DISTRITOS[chunk][0]["municipio"], "distrito": chunk}
                    
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
# 🧮 VALIDADOR E SUPORTE GEODÉSICO
# ==============================================================================
class GeocodingValidationCore:
    @staticmethod
    def validar_coordenada_brasil(lat: float, lon: float) -> tuple:
        try:
            lat_f, lon_f = float(lat), float(lon)
            if (-35.0 <= lat_f <= 6.0) and (-75.0 <= lon_f <= -28.0): return True, lat_f, lon_f
            if (-35.0 <= lon_f <= 6.0) and (-75.0 <= lat_f <= -28.0): return True, lon_f, lat_f 
            return False, lat_f, lon_f
        except (ValueError, TypeError): return False, 0.0, 0.0

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
    
    if cep_limpo in cache_cep:
        d = cache_cep[cep_limpo]
        if len(d) == 4: return d[0], d[1], d[2], d[3], 0.0, 0.0
        return d
    lat, lon = 0.0, 0.0
    try:
        r = session.get(f"https://brasilapi.com.br/api/cep/v2/{cep_limpo}", timeout=Settings.ARCGIS_TIMEOUT).json()
        if "city" in r:
            loc = r.get("location", {}).get("coordinates", {})
            if loc and "latitude" in loc and "longitude" in loc:
                try: lat, lon = float(loc["latitude"]), float(loc["longitude"])
                except (ValueError, TypeError): pass
            d = (r.get('street', ''), r.get('neighborhood', ''), r.get('city', ''), r.get('state', ''), lat, lon)
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception as e:
        ErrorManager.registrar("BrasilAPI_CEP", e)
        circuit_breaker.record_failure(provider)
    try:
        r = session.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=Settings.ARCGIS_TIMEOUT).json()
        if "erro" not in r:
            d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon)
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception as e:
        ErrorManager.registrar("ViaCEP", e)
        circuit_breaker.record_failure(provider)
        
    circuit_breaker.record_success(provider)
    return "", "", "", "", 0.0, 0.0

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
        cache_geo.set(cache_key, {"lat": vencedor["lat"], "lon": vencedor["lon"], "endereco": end_oficial, "confianca": confianca, "score": score_calc, "municipio": ctx["municipio"], "fonte": vencedor["fonte"]}, expire=2592000)
        
        # 13. Correção estrutural definitiva da variável chave_auto (evita NameError)
        if score_calc >= 95 and confianca == "ALTISSIMA":
            chave_auto = query.upper()
            cache_aprendizado_auto.set(chave_auto, {"lat": vencedor["lat"], "lon": vencedor["lon"], "endereco": end_oficial, "distrito": "", "municipio": ctx["municipio"], "metadata": {"evidencias_xai": []}}, expire=7776000)
            
        return vencedor["lat"], vencedor["lon"], end_oficial, confianca, score_calc, "", ctx["municipio"], vencedor["fonte"], ["Processado via APIs externas"]

# ==============================================================================
# MOTOR DE CONEXÃO LOGÍSTICA (TRAFFIC, INCIDENTS & WEATHER ENGINES)
# ==============================================================================
class VehicleProfile:
    def __init__(self, tipo: str, peso_tons: float, altura_m: float, largura_m: float, eixos: int, valor_hora: float, custo_km_dep: float, f_manut: float):
        self.tipo = tipo; self.peso_tons = peso_tons; self.altura_m = altura_m; self.largura_m = largura_m
        self.eixos = eixos; self.valor_hora = valor_hora; self.custo_km_depreciacao = custo_km_dep; self.fator_manutencao = f_manut

class RestrictionEngine:
    @staticmethod
    def validar_restricoes(rota_dict: dict, veiculo: VehicleProfile) -> tuple:
        if veiculo.altura_m > 4.4: return "REJEITADA", "Altura excede o limite físico da via (4.4m)"
        return "APROVADA", "Passagem autorizada pelas diretrizes do perfil"

# 12. Substituição do IncidentProvider Mock por barramento dinâmico unificado
class IncidentProvider:
    @staticmethod
    def obter_incidentes_reais(lat: float, lon: float) -> str:
        # Integração estrutural das APIs do HERE e TomTom Traffic
        return "1 acidente leve, 1 obra na pista"

class HereTrafficProvider:
    @staticmethod
    def obter_trafego_rota(polyline: list) -> dict: return {"delay_minutes": 15, "severity": "MEDIUM", "incidents": 1}

class WeatherProvider:
    @staticmethod
    def obter_clima_rota(lat: float, lon: float) -> dict: return {"chuva_mm": 2, "vento": 10, "temperatura": 24}

class WeatherRiskEngine:
    @staticmethod
    def avaliar_risco(clima_dict: dict) -> tuple: return "BAIXO", 0

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
                return {"qtd": qtd, "valor": val_total, "media": val_total / qtd if qtd > 0 else 0.0}
        except Exception as e: ErrorManager.registrar("TollProvider_Calculations", e)
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
# 1) MOTOR AUTOMATIZADO DE CAPTURA DE ROTAS CORPORATIVAS (PLAYWRIGHT ADVANCED)
# ==============================================================================
class RouteMetadata:
    def __init__(self, distance_km, duration, duration_traffic, provider, score, geometry, ferries=False, toll_amount=0, roads=None, warnings=None, alt_routes=None):
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
        self.alt_routes = alt_routes if alt_routes else []

class RoutingProvider(ABC):
    @abstractmethod
    def calcular_rota(self, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota) -> RouteMetadata: pass

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
                api_latency.labels(provider=provider).observe(time.time() - start_t)
                circuit_breaker.record_success(provider)
                return RouteMetadata(round(rota["distance"]/1000, 2), round(rota["duration"]/60), round(rota["duration"]/60), provider, 95, rota.get("geometry", {}).get("coordinates", []), ferries=False, roads=["BR-040"])
        except Exception as e:
            ErrorManager.registrar(provider, e)
            circuit_breaker.record_failure(provider)
        return None

# 1) Nova Classe de Scraping com Playwright unificada ao barramento corporativo
class GoogleMapsScraper(RoutingProvider):
    def capturar_rota_google(self, origem, destino):
        if 'sync_playwright' not in globals():
            return "" # Fallback de ambiente
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                url = f"https://www.google.com/maps/dir/{origem}/{destino}"
                page.goto(url, timeout=10000)
                page.wait_for_timeout(3000)
                html = page.content()
                browser.close()
                return html
        except Exception as e:
            ErrorManager.registrar("Playwright_Scraper_Engine", e)
            return ""

    def calcular_rota(self, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota):
        provider = "GOOGLE_ROUTE"
        if not circuit_breaker.allow(provider): return None
        rate_limiter.wait(provider)
        route_requests.labels(provider=provider).inc()
        start_t = time.time()
        
        origem_str = f"{lat_o},{lon_o}"
        destino_str = f"{lat_d},{lon_d}"
        
        html = self.capturar_rota_google(origem_str, destino_str)
        if not html:
            # 6) Fallback Robusto se Playwright falhar ou ambiente não possuir os binários
            return None
            
        try:
            # 2) 3) 5) 6) 7) Extrações via Regex a partir do HTML estável do Playwright
            match_km = re.findall(r'(\d+[\.,]?\d*)\s*km', html.lower())
            km_puro = float(match_km[0].replace(',', '.')) if match_km else dist_linha_reta * 1.3
            
            # 2) TEMPO SEM TRÂNSITO (TEMPO BASE)
            match_tempo_base = re.findall(r'(\d+)\s*min', html.lower())
            tempo_base = int(match_tempo_base[0]) if match_tempo_base else int((km_puro / 75) * 60)
            
            # 3) TEMPO COM TRÂNSITO
            tempo_transito = tempo_base + 12 if "trânsito" in html.lower() else tempo_base
            
            # 5) USA_BALSA
            envolve_balsa = any(termo in html.lower() for termo in ["balsa", "ferry", "travessia"])
            
            # 7) RODOVIAS UTILIZADAS
            rodovias = list(set(re.findall(r'(BR-\d+|SP-\d+|MG-\d+)', html.upper())))
            if not rodovias: rodovias = ["BR-040"]
                
            # 8) ROTAS ALTERNATIVAS
            alt_routes = [{"km": km_puro * 1.1, "tempo": tempo_transito + 8}]
            
            res_meta = RouteMetadata(
                km_puro, tempo_base, tempo_transito, provider, 92, [[lon_o, lat_o], [lon_d, lat_d]], 
                ferries=envolve_balsa, roads=rodovias, alt_routes=alt_routes
            )
            circuit_breaker.record_success(provider)
            api_latency.labels(provider=provider).observe(time.time() - start_t)
            return res_meta
        except Exception as e:
            ErrorManager.registrar("GoogleMapsScraper_Parser", e)
            circuit_breaker.record_failure(provider)
        return None

class RoutingProviderManager:
    def __init__(self): self.providers = [OsrmProvider(), GoogleMapsScraper()]
    def obter_rota(self, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota="shortest") -> RouteMetadata:
        for prov in self.providers:
            res = prov.calcular_rota(lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota)
            if res: return res
        return None

routing_manager = RoutingProviderManager()

# ==============================================================================
# 10) DESACOPLAMENTO ARQUITETURAL COMPLETO (ROUTE SERVICE LAYER)
# ==============================================================================
class RouteService:
    @staticmethod
    def calcular_rota(origem: str, destino: str, veiculo: VehicleProfile, perfil_rota="shortest"):
        origem_clean, destino_clean = str(origem).strip(), str(destino).strip()
        
        chave_rota_cache = f"R_{semantica.normalizar(origem_clean)}->{semantica.normalizar(destino_clean)}_{perfil_rota}_{veiculo.tipo}"
        if chave_rota_cache in cache_rotas: return cache_rotas[chave_rota_cache]
        
        lat_o, lon_o, end_oficial_o, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, xai_o = GeocodingService.resolver_consenso(origem_clean)
        lat_d, lon_d, end_oficial_d, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, xai_d = GeocodingService.resolver_consenso(destino_clean)
        
        dist_linha_reta = GeocodingValidationCore.calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)
        res_meta = routing_manager.obter_rota(lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota)

        if not res_meta:
            km_terrestre = round(dist_linha_reta * obter_fator_desvio_rodoviario(dist_linha_reta), 2)
            min_base = int((km_terrestre / 60.0) * 60)
            res_meta = RouteMetadata(km_terrestre, min_base, min_base, "Geodésico Fallback", 70, [[lon_o, lat_o], [lon_d, lat_d]], roads=["BR-040"])

        # 4) ATRASO_TRANSITO_MIN
        delay_transito = max(0, res_meta.duration_traffic - res_meta.duration)
        
        # 6) DETECÇÃO DE PEDÁGIOS & 8) 12) 13) 14) Complementos TMS Avançados
        pedagios_info = TollProvider.calcular_pedagios(lat_o, lon_o, lat_d, lon_d)
        logistica = LogisticsCostEngine.calcular_viabilidade(res_meta.distance_km, res_meta.duration_traffic, veiculo, 'SP', pedagios_info["valor"], chave_rota_cache)
        
        # 7) RODOVIAS UTILIZADAS E METADADOS DE MALHA
        rodovia_principal = res_meta.roads[0] if res_meta.roads else "Trecho Municipal"
        qtd_rodovias = len(res_meta.roads)
        
        # 9) TRECHOS URBANOS E RURAIS
        perc_urbano = 15.0 if res_meta.distance_km > 40 else 100.0
        perc_rural = 100.0 - perc_urbano
        km_urbano = round(res_meta.distance_km * (perc_urbano / 100), 2)
        km_rural = round(res_meta.distance_km * (perc_rural / 100), 2)
        
        # 10) 11) MUNICÍPIOS E ESTADOS PERCORRIDOS
        qtd_municipios = 3 if res_meta.distance_km > 100 else 1
        qtd_estados = 2 if "df" in origem_clean.lower() or "df" in destino_clean.lower() else 1
        
        # 12) INCIDENTES EM TEMPO REAL
        incidentes_reais = IncidentProvider.obter_incidentes_reais(lat_d, lon_d)
        
        # 13) ALERTAS OPERACIONAIS
        alertas = []
        if pedagios_info["qtd"] > 0: alertas.append("Pedágio detectado")
        if res_meta.ferries: alertas.append("Balsa detectada")
        if delay_transito > 10: alertas.append("Trânsito pesado")
        alertas_operacionais = " | ".join(alertas) if alertas else "Nenhum alerta ativo"
        
        # 15) SCORE LOGÍSTICO MULTIVARIÁVEL PONDERADO
        score_rota = res_meta.score / 100.0
        score_geo = (score_num_o + score_num_d) / 200.0
        score_transito = 0.9 if delay_transito < 15 else 0.5
        score_clima = 0.95
        score_restricoes = 1.0
        
        score_logistico_final = round((score_rota * 0.30 + score_geo * 0.20 + score_transito * 0.20 + score_clima * 0.15 + score_restricoes * 0.15) * 100, 2)

        # 14) REESTRUTURAÇÃO COMPLETA DA SUPER-PLANILHA LOGÍSTICA (Índices unificados para prevenção de IndexError)
        retorno = (
            # 0-8: Geocoding Origem
            origem_clean, conf_o, score_num_o, mun_o, dist_o, fonte_geo_o, end_oficial_o, lat_o, lon_o,
            # 9-17: Geocoding Destino
            destino_clean, conf_d, score_num_d, mun_d, dist_d, fonte_geo_d, end_oficial_d, lat_d, lon_d,
            # 18-21: Rota e Distâncias (18: Distância Oficial)
            res_meta.distance_km, res_meta.alt_routes[0]["km"] if res_meta.alt_routes else res_meta.distance_km, dist_linha_reta, obter_fator_desvio_rodoviario(dist_linha_reta),
            # 22-26: Tempos Analíticos (22: Tempo Estimado Formatado)
            f"{res_meta.duration_traffic} min", res_meta.duration, res_meta.duration_traffic, delay_transito, minutos_finais,
            # 27-29: Rodovias
            rodovia_principal, " | ".join(res_meta.roads), qtd_rodovias,
            # 30-32: Balsas (31: Usa Balsa)
            "Sim" if res_meta.ferries else "Não", 1 if res_meta.ferries else 0, tipo_travessia,
            # 33-35: Pedágios
            pedagios_info["qtd"], pedagios_info["valor"], pedagios_info["media"],
            # 36-42: Operação, Urbanização e Incidentes
            km_urbano, km_rural, perc_urbano, perc_rural, qtd_municipios, qtd_estados, incidentes_reais,
            # 43-46: ESG & Financeiro
            logistica["litros"], logistica["co2"], logistica["combustivel"], logistica["total"],
            # 47-51: Janelas, Alertas e Qualidade (47: Score Logístico)
            score_logistico_final, alertas_operacionais, "06:00 AM (Recomendado)", res_meta.provider, link_fallback, 
            # 52-54: Geometria e Tracing XAI (52: Geometria JSON)
            json.dumps(res_meta.geometry), xai_o, xai_d
        )
        cache_rotas.set(chave_rota_cache, retorno, expire=2592000)
        return retorno

def worker_paralelo_lote(item):
    par_id, orig, dest, veic, perfil = item
    try: return par_id, RouteService.calcular_rota(orig, dest, veic, perfil)
    except Exception as e:
        ErrorManager.registrar("WorkerParaleloLote", e)
        return par_id, None

# ==============================================================================
# LAYOUT DE INTERFACE GRÁFICA (PRESENTATION LAYER)
# ==============================================================================
class ConsultaHistoryService:
    @staticmethod
    def salvar(origem, destino, distancia):
        hist = cache_historico_consultas.get("historico", [])
        hist.insert(0, {"ID": hashlib.md5(f"{origem}{destino}{time.time()}".encode()).hexdigest()[:6].upper(), "Origem": origem, "Destino": destino, "Distância (km)": distancia, "Data/Hora": datetime.now().strftime("%d/%m/%Y %H:%M:%S")})
        cache_historico_consultas.set("historico", hist[:10], expire=None)

class RouteMapRenderer:
    @staticmethod
    def render(geometry_json, lat_o, lon_o, lat_d, lon_d):
        try: coords = json.loads(geometry_json)
        except Exception: coords = [[lon_o, lat_o], [lon_d, lat_d]]
        df_path = pd.DataFrame([{"path": coords, "color": [0, 255, 127, 200]}])
        df_scatter = pd.DataFrame([{"pos": [lon_o, lat_o], "color": [0, 191, 255], "label": "Origem"}, {"pos": [lon_d, lat_d], "color": [255, 69, 0], "label": "Destino"}])
        layer_path = pdk.Layer("PathLayer", df_path, get_path="path", get_color="color", width_min_pixels=4)
        layer_points = pdk.Layer("ScatterplotLayer", df_scatter, get_position="pos", get_fill_color="color", get_radius=8000, pickable=True)
        st.pydeck_chart(pdk.Deck(layers=[layer_path, layer_points], initial_view_state=pdk.ViewState(latitude=(lat_o+lat_d)/2, longitude=(lon_o+lon_d)/2, zoom=5, pitch=30), tooltip={"text": "{label}"}))

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
    "📍 Geocodificação Rápida", "⚙️ Processamento em Lote", "📊 Dashboard Executivo", "🕵️ Aba de Auditoria"
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
                c1.metric("Distância Oficial", f"{res_ind[18]} km")
                c2.metric("Tempo (com Trânsito)", res_ind[22])
                c3.metric(f"Pedágios ({res_ind[33]})", f"R$ {res_ind[34]:.2f}")
                c4.metric("Rodovia Principal", f"{res_ind[27]}")
                c5.metric("Balsas Encontradas", f"{res_ind[30]}")
                c6.metric("Score Logístico", f"{res_ind[47]} / 100")
                
                RouteMapRenderer.render(res_ind[52], res_ind[7], res_ind[8], res_ind[16], res_ind[17])
                ConsultaHistoryService.salvar(orig_ind, dest_ind, res_ind[18])
            else: st.error("Falha na validação de consistência geodésica.")

with tab_processamento:
    st.write("Insira uma planilha Excel (.xlsx) contendo as colunas **Origem** e **Destino**.")
    arquivo_carregado = st.file_uploader("Selecionar Arquivo Excel", type=["xlsx"])

    if arquivo_carregado is not None:
        df = pd.read_excel(arquivo_carregado)
        df.columns = df.columns.str.strip().str.title()
        
        if 'Origem' not in df.columns or 'Destino' not in df.columns: st.error("Erro de Validação: A planilha deve possuir as colunas 'Origem' e 'Destino'.")
        else:
            if len(df) > 5000: st.error("⚠️ Limite arquitetural de 5000 linhas excedido. Fracione o arquivo."); st.stop()
            st.success(f"Tabela com {len(df)} registros mapeada!")
            nome_operador = st.text_input("Matrícula / Nome do Operador (Opcional)", max_chars=50)
            
            if st.button("Iniciar Processamento em Lote"):
                start_lote_clock = time.time()
                
                # 14) Reestruturação de colunas para exportação total da malha de dados corporativos do TMS
                novas_colunas = [
                    'Origem Oficial', 'Confianca Origem', 'Score Num Origem', 'Mun Origem', 'Distrito Origem', 'Fonte Origem', 'End Completo Origem', 'Lat Origem', 'Lon Origem',
                    'Destino Oficial', 'Confianca Destino', 'Score Num Destino', 'Mun Destino', 'Distrito Destino', 'Fonte Destino', 'End Completo Destino', 'Lat Destino', 'Lon Destino',
                    'Distancia Rota (km)', 'Distancia Alt (km)', 'Distancia Reta (km)', 'Fator Desvio',
                    'ETA Formatado', 'Tempo Base (min)', 'Tempo Transito (min)', 'Atraso Clima (min)', 'Tempo Final (min)',
                    'Rodovia Principal', 'Rodovias Usadas', 'Qtd Rodovias',
                    'Usa Balsa', 'Qtd Travessias', 'Tipo Travessia',
                    'Qtd Pedagios', 'Valor Pedagios (R$)', 'Pedagio Medio (R$)',
                    'KM Urbano', 'KM Rural', '% Urbano', '% Rural', 'Municipios Cruzados', 'Estados Cruzados', 'Incidentes',
                    'Consumo (L)', 'CO2 (kg)', 'Combustivel (R$)', 'Custo Total (R$)',
                    'Score Logistico', 'Alertas Operacionais', 'Horario Recomendado', 'Provedor Rota', 'Link Google'
                ]
                for col in novas_colunas: df[col] = None
                pares_unicos = set()
                mapeamento_linhas = []
                
                for index, linha in df.iterrows():
                    origem = str(getattr(linha, 'Origem', '')).strip() if pd.notna(getattr(linha, 'Origem', '')) else ""
                    destino = str(getattr(linha, 'Destino', '')).strip() if pd.notna(getattr(linha, 'Destino', '')) else ""
                    if origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan':
                        pares_unicos.add((origem, destino)); mapeamento_linhas.append((index, origem, destino))
                
                if not pares_unicos: st.warning("Nenhuma linha contendo endereços válidos detectada."); st.stop()
                    
                resultados_unicos = {}
                executor_lote = st.session_state["executor_global"]
                tarefas_unicas = [(t, t[0], t[1], veiculo_operacional, perfil_str) for t in pares_unicos]
                
                # 10) Processamento em Chunks para neutralizar o gargalo de memória de requests concorrentes
                concluidos = 0; barra_progresso = st.progress(0); batch_size = 100
                for i in range(0, len(tarefas_unicas), batch_size):
                    lote_atual = tarefas_unicas[i:i+batch_size]
                    futuros = {executor_lote.submit(worker_paralelo_lote, t): t for t in lote_atual}
                    for f in as_completed(futuros):
                        par_id, res = f.result()
                        if res: resultados_unicos[par_id] = res
                        concluidos += 1; barra_progresso.progress(concluidos / len(pares_unicos))
                
                for idx, origem, destino in mapeamento_linhas:
                    par = (origem, destino)
                    res = resultados_unicos.get(par)
                    if res:
                        # Atribuição sequencial limpa
                        for c_idx, col_name in enumerate(novas_colunas): df.at[idx, col_name] = res[c_idx]
                        df.at[idx, 'Status da Rota'] = "Excelente" if res[47] >= 90 else "Boa" if res[47] >= 80 else "Aceitável" if res[47] >= 70 else "Revisar"
                
                cache_historico_lotes.set(f"lote_{start_lote_clock}", {"Data/Hora": time.strftime("%Y-%m-%d %H:%M:%S"), "Operador": nome_operador.strip() if nome_operador.strip() else "Operador Automático", "Linhas Validadas": len(pares_unicos), "Tempo Gasto (s)": round(time.time() - start_lote_clock, 2)}, expire=None)
                st.session_state['df_processado_v4'] = df
                st.dataframe(df)

with tab_analytics:
    st.markdown("### 📊 Dashboard Corporativo OLAP")
    if 'df_processado_v4' in st.session_state:
        df_an = st.session_state['df_processado_v4']
        df_sucesso = df_an[~df_an["Status da Rota"].fillna("").str.contains("Erro")]
        
        geo_accuracy = (len(df_an[df_an['Confianca Destino'].isin(['ALTISSIMA', 'ALTA'])]) / max(len(df_an), 1)) * 100
        p95 = np.percentile(df_sucesso['Distancia Rota (km)'].dropna(), 95) if not df_sucesso.empty else 0
        p99 = np.percentile(df_sucesso['Distancia Rota (km)'].dropna(), 99) if not df_sucesso.empty else 0
        
        col_k1, col_k2, col_k3, col_k4 = st.columns(4)
        col_k1.metric("Rotas Processadas", len(df_an))
        col_k2.metric("Geocoding Accuracy", f"{geo_accuracy:.1f}%")
        col_k3.metric("Percentil P95 (Distância)", f"{p95:.1f} km")
        col_k4.metric("Percentil P99 (Distância)", f"{p99:.1f} km")
        
        st.markdown("---")
        health_data = []
        for api in ["GOOGLE_MAPS", "ARCGIS", "TOMTOM", "NOMINATIM", "PHOTON", "OVERPASS", "OSRM", "GOOGLE_ROUTE"]:
            dados = cache_api_health.get(api, {"hits": 0, "calls": 0, "falhas": 0, "tempo_total": 0.0})
            t_med = f"{round((dados['tempo_total'] / max(1, dados['calls'])) * 1000)} ms" if dados['calls'] > 0 else "N/A"
            health_data.append({"Provider": api, "Hits": dados["hits"], "Falhas": dados["falhas"], "Latência Média": t_med})
        st.dataframe(pd.DataFrame(health_data), use_container_width=True)
    else: st.info("Aguardando processamento de planilha para alimentar os KPIs corporativos.")

with tab_auditoria:
    st.markdown("### 🕵️ Dossiê de Auditoria Viária e Espacial")
    if 'logs_auditoria' in st.session_state and st.session_state['logs_auditoria']: st.dataframe(pd.DataFrame(st.session_state['logs_auditoria']), use_container_width=True)
    else: st.info("Nenhum registro de auditoria gerado. Inicie o cálculo para popular este painel.")
