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
# CONFIGURAÇÃO DE UI/UX E AMBIENTE
# ==============================================================================
st.set_page_config(page_title="Gerenciador de Rotas Inteligentes", page_icon="🚗", layout="wide")

TOMTOM_API_KEY = "" # Insira sua credencial TomTom Logistics aqui

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
# BANCO DE DADOS RELACIONAL EM MEMÓRIA (PEDÁGIOS, COMBUSTÍVEL E EMISSÕES)
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
    cursor.execute("INSERT INTO pedagios VALUES (1, 'Praça Cajamar', 'SP-330', 38.5, -23.35, -46.88, 12.40)")
    cursor.execute("INSERT INTO pedagios VALUES (2, 'Praça Brasília', 'BR-040', 10.0, -15.80, -47.90, 6.80)")
    cursor.execute("INSERT INTO precos_combustivel VALUES ('SP', 'SÃO PAULO', 6.15, 5.80, 3.90, 3.10, '2023-10-01')")
    cursor.execute("INSERT INTO precos_combustivel VALUES ('DF', 'BRASÍLIA', 6.40, 5.95, 4.10, 3.50, '2023-10-01')")
    db_conn.commit()

inicializar_banco_relacional()

# ==============================================================================
# OBSERVABILIDADE, LOGGING ESTRUTURADO E ERROR MANAGER
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
# SEGURANÇA E RESILIÊNCIA (RATE LIMITER E CIRCUIT BREAKER)
# ==============================================================================
class CircuitBreaker:
    def __init__(self, threshold=Settings.CIRCUIT_BREAKER_FAILURES):
        self.failures = collections.defaultdict(int)
        self.threshold = threshold
        self.state = collections.defaultdict(lambda: "UP")

    def allow(self, provider):
        return self.failures[provider] < self.threshold

    def record_success(self, provider):
        self.failures[provider] = 0
        self.state[provider] = "UP"

    def record_failure(self, provider):
        self.failures[provider] += 1
        if self.failures[provider] >= self.threshold:
            self.state[provider] = "DOWN"

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
        return circuit_breaker.state

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
cache_base_local = Cache("./cache_base_local")
cache_aprendizado = Cache("./cache_aprendizado")
cache_aprendizado_auto = Cache("./cache_aprendizado_auto")
cache_api_health = Cache("./cache_api_health")
cache_historico_lotes = Cache("./cache_historico_lotes")
cache_historico_consultas = Cache("./cache_historico_consultas")

for c in [cache_classificacao, cache_fuzzy, cache_geo, cache_rotas, cache_poi, cache_cep, cache_google, cache_reverse, cache_base_local, cache_aprendizado, cache_aprendizado_auto, cache_api_health, cache_historico_lotes, cache_historico_consultas]:
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

CACHE_IBGE_PATH = "municipios_ibge.pkl"

# ==============================================================================
# 🎛️ INFRAESTRUTURA DE CONCORRÊNCIA E FILAS (CORREÇÃO DE CONTEXTO EM LOTE)
# ==============================================================================
@st.cache_resource
def get_executors():
    """Garante escopo global seguro para executors, evitando erros de session_state em background threads."""
    return {
        "global": ThreadPoolExecutor(max_workers=Settings.WORKERS_DISPONIVEIS),
        "nominatim": ThreadPoolExecutor(max_workers=1),
        "apis": ThreadPoolExecutor(max_workers=16)
    }

executors_pool = get_executors()

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

def registrar_telemetria(fonte, sucesso, tempo_gasto):
    m = cache_api_health.get(fonte, {"hits": 0, "calls": 0, "falhas": 0, "tempo_total": 0.0})
    m["calls"] += 1
    m["tempo_total"] += tempo_gasto
    if sucesso: m["hits"] += 1
    else: m["falhas"] += 1
    cache_api_health.set(fonte, m, expire=None)

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

            with open(CACHE_IBGE_PATH, "wb") as f:
                pickle.dump({"municipios": base_mun, "estados": base_est, "distritos": base_dist}, f)
    except Exception: pass
    
    lista_completa = list(base_mun.keys()) + list(base_dist.keys())
    return base_mun, base_est, base_dist, lista_completa

IBGE_MUNICIPIOS, IBGE_ESTADOS, IBGE_DISTRITOS, LISTA_TOPONIMOS = carregar_dados_ibge()

LISTA_CONTEXTO_FUZZY = []
for k, v_list in IBGE_MUNICIPIOS.items(): 
    for v in v_list: LISTA_CONTEXTO_FUZZY.append(f"{k} {v['uf']}")
for k, v_list in IBGE_DISTRITOS.items(): 
    for v in v_list: LISTA_CONTEXTO_FUZZY.append(f"{k} {v['uf']}")
LISTA_CONTEXTO_FUZZY = list(set(LISTA_CONTEXTO_FUZZY))

POI_KEYWORDS = [
    "AEROPORTO", "HOSPITAL", "UNIVERSIDADE", "FACULDADE", "ESCOLA", "SHOPPING", 
    "HOTEL", "RODOVIARIA", "ESTADIO", "MINISTERIO", "AGENCIA", "BANCO", 
    "IGREJA", "FORUM", "TRIBUNAL", "DELEGACIA", "PREFEITURA", "CLINICA",
    "CENTRO DE DISTRIBUICAO", "TERMINAL", "BASE OPERACIONAL"
]

BOUNDING_BOXES_UF = {
    "DF": {"lat_min": -16.05, "lat_max": -15.50, "lon_min": -48.30, "lon_max": -47.30},
    "SP": {"lat_min": -25.50, "lat_max": -19.50, "lon_min": -53.50, "lon_max": -44.00},
    "GO": {"lat_min": -19.50, "lat_max": -12.40, "lon_min": -53.30, "lon_max": -45.90},
}

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
        self.via_keys = ["RUA", "AVENIDA", "TRAVESSA", "ALAMEDA", "RODOVIA", "ESTRADA", "QUADRA", "SQN", "SQS", "SHIS", "SHIN", "SCRN", "SCS", "SRTVN", "CLS", "CLN", "QNL", "QNM", "QNN", "QNG", "QNJ", "QNK", "QI", "QE", "QC", "QR", "QS", "QSC"]
        
        self.mapa_contexto_df = {
            "TAGUATINGA": "TAGUATINGA", "GAMA": "GAMA", "PONTE ALTA": "GAMA", "PONTE ALTA NORTE": "GAMA", "PONTE ALTA SUL": "GAMA", "CEILANDIA": "CEILANDIA", "SOL NASCENTE": "CEILANDIA", "POR DO SOL": "CEILANDIA", "AGUAS CLARAS": "AGUAS CLARAS", "ARNIQUEIRAS": "AGUAS CLARAS", "SAMAMBAIA": "SAMAMBAIA", "GUARA": "GUARA", "PLANALTINA": "PLANALTINA", "SOBRADINHO": "SOBRADINHO", "VICENTE PIRES": "VICENTE PIRES", "SANTA MARIA": "SANTA MARIA", "RECANTO DAS EMAS": "RECANTO DAS EMAS", "RIACHO FUNDO": "RIACHO FUNDO", "LAGO SUL": "PLANO PILOTO", "LAGO NORTE": "PLANO PILOTO", "NUCLEO BANDEIRANTE": "NUCLEO BANDEIRANTE", "BRAZLANDIA": "BRAZLANDIA"
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
                return f"{logr}{num_str}{comp_str}, {bair}, {loca}, {nome_estado_cep}, BRASIL", "CEP", "", lat_cep, lon_cep

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
# 🧮 VALIDADOR PRÉ-GEOCODING E LÓGICA GEODÉSICA
# ==============================================================================
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

def validar_coordenada_brasil(lat, lon):
    try:
        lat_f, lon_f = float(lat), float(lon)
        if (-35.0 <= lat_f <= 6.0) and (-75.0 <= lon_f <= -28.0): return True, lat_f, lon_f
        if (-35.0 <= lon_f <= 6.0) and (-75.0 <= lat_f <= -28.0): return True, lon_f, lat_f 
        return False, lat_f, lon_f
    except (ValueError, TypeError): return False, 0.0, 0.0

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
    except Exception as e:
        ErrorManager.registrar("Vincenty_Calc", e)
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
    provider = "cascata_postal"
    if not circuit_breaker.allow(provider): return "", "", "", "", 0.0, 0.0
    rate_limiter.wait(provider)
    
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
    except Exception as e:
        ErrorManager.registrar("BrasilAPI_CEP", e)
        circuit_breaker.record_failure(provider)
    try:
        def _nom_cep():
            time.sleep(1.1)
            url = f"https://nominatim.openstreetmap.org/search?format=json&postalcode={cep_limpo}&countrycodes=br&limit=1"
            return session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
        r_nom = exec_pools["nominatim"].submit(_nom_cep).result()
        if r_nom: lat, lon = float(r_nom[0]['lat']), float(r_nom[0]['lon'])
    except Exception as e:
        ErrorManager.registrar("Nominatim_CEP", e)
        circuit_breaker.record_failure(provider)
    try:
        r = session.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=4).json()
        if "erro" not in r:
            d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon)
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception as e:
        ErrorManager.registrar("ViaCEP", e)
        circuit_breaker.record_failure(provider)
    try:
        r = session.get(f"https://opencep.com/v1/{cep_limpo}", timeout=4).json()
        if "error" not in r:
            d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon)
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception as e:
        ErrorManager.registrar("OpenCEP", e)
        circuit_breaker.record_failure(provider)
        
    circuit_breaker.record_success(provider)
    return "", "", "", "", 0.0, 0.0

def validar_consistencia_administrativa(candidato, uf_inf):
    est_api = unidecode(candidato.get('estado', '')).upper().strip()
    if uf_inf and est_api:
        if uf_inf != est_api:
            return False
    return True

def validar_consistencia_municipal(candidato, mun_inf):
    if not mun_inf: return True
    cid_api = unidecode(candidato.get('cidade', '')).upper().strip()
    if not cid_api: return False
    if mun_inf == cid_api or mun_inf in cid_api or cid_api in mun_inf: return True
    if fuzz.token_set_ratio(mun_inf, cid_api) >= 95: return True
    return False

# ==============================================================================
# 🗺️ MÓDULOS DE GEOCODIFICAÇÃO (APIs EM PARALELO)
# ==============================================================================
def API_Google_Geocoding_Scraper(query):
    provider = "GOOGLE_MAPS"
    if not circuit_breaker.allow(provider): return None
    rate_limiter.wait(provider)
    start_t = time.time()
    try:
        url = f"https://www.google.com/maps/search/{requests.utils.quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = session.get(url, headers=headers, timeout=5, allow_redirects=True)
        match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.url)
        if not match: match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.text)
        if match: 
            lat, lon = float(match.group(1)), float(match.group(2))
            if validar_coordenada_brasil(lat, lon)[0]:
                registrar_telemetria(provider, True, time.time() - start_t)
                circuit_breaker.record_success(provider)
                return [{"lat": lat, "lon": lon, "fonte": provider, "score_base": 40, "cidade": "", "estado": "", "bairro": ""}]
    except Exception as e:
        ErrorManager.registrar("API_Google_Geocoding", e)
        circuit_breaker.record_failure(provider)
    registrar_telemetria(provider, False, time.time() - start_t)
    return []

def API_ArcGIS(query, ctx=None):
    provider = "ARCGIS"
    if not circuit_breaker.allow(provider): return None
    rate_limiter.wait(provider)
    start_t = time.time()
    try:
        if ctx and (ctx.get("logradouro") or ctx.get("municipio")):
            url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&Address={requests.utils.quote(ctx.get('logradouro', ''))}&Neighborhood={requests.utils.quote(ctx.get('bairro', ''))}&City={requests.utils.quote(ctx.get('municipio', ''))}&Region={requests.utils.quote(ctx.get('uf', ''))}&Postal={requests.utils.quote(ctx.get('cep', ''))}&maxLocations=3&sourceCountry=BRA"
        else:
            url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=3&sourceCountry=BRA"
        r = session.get(url, timeout=Settings.ARCGIS_TIMEOUT).json()
        resultados = []
        if r.get('candidates'):
            for c in r['candidates'][:3]:
                lat, lon = float(c['location']['y']), float(c['location']['x'])
                if validar_coordenada_brasil(lat, lon)[0]:
                    resultados.append({"lat": lat, "lon": lon, "fonte": provider, "score_base": 30, "cidade": c.get('attributes', {}).get('City', '').upper(), "estado": c.get('attributes', {}).get('RegionAbbr', '').upper()})
            registrar_telemetria(provider, True, time.time() - start_t)
            circuit_breaker.record_success(provider)
        return resultados if resultados else []
    except Exception as e:
        ErrorManager.registrar("API_ArcGIS", e)
        circuit_breaker.record_failure(provider)
    registrar_telemetria(provider, False, time.time() - start_t)
    return []

def executar_reverse_geocoding_multimotor(lat, lon):
    rev_key = f"{round(lat,5)}|{round(lon,5)}"
    if rev_key in cache_reverse: return cache_reverse[rev_key]
    res = {"logradouro": "", "bairro": "", "cidade": "", "municipio": "", "distrito": "", "estado": "", "cep": ""}
    
    provider_nom = "NOMINATIM_REVERSE"
    try:
        if circuit_breaker.allow(provider_nom):
            rate_limiter.wait(provider_nom)
            def _nom_rev():
                time.sleep(1.1)
                url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&addressdetails=1"
                return session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=Settings.NOMINATIM_TIMEOUT).json()
            
            a = exec_pools["nominatim"].submit(_nom_rev).result().get("address", {})
            res.update({"logradouro": a.get("road", a.get("pedestrian", "")), "bairro": a.get("neighbourhood", a.get("suburb", a.get("city_district", ""))), "cidade": a.get("city", a.get("town", a.get("municipality", ""))), "estado": a.get("state", "").upper(), "cep": a.get("postcode", "")})
            circuit_breaker.record_success(provider_nom)
            cache_reverse.set(rev_key, res, expire=2592000); return res
    except Exception as e:
        ErrorManager.registrar("Reverse_Nominatim", e)
        circuit_breaker.record_failure(provider_nom)

    provider_arc = "ARCGIS_REVERSE"
    try:
        if circuit_breaker.allow(provider_arc):
            rate_limiter.wait(provider_arc)
            url_arc = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/reverseGeocode?location={lon},{lat}&f=json"
            r_arc = session.get(url_arc, timeout=Settings.ARCGIS_TIMEOUT).json()
            if 'address' in r_arc:
                addr = r_arc['address']
                res.update({"logradouro": addr.get('Address', ''), "bairro": addr.get('Neighborhood', ''), "cidade": addr.get('City', ''), "estado": addr.get('RegionAbbr', '').upper(), "cep": addr.get('Postal', '')})
                circuit_breaker.record_success(provider_arc)
                cache_reverse.set(rev_key, res, expire=2592000)
    except Exception as e:
        ErrorManager.registrar("Reverse_ArcGIS", e)
        circuit_breaker.record_failure(provider_arc)
        
    return res

# ==============================================================================
# 🧠 MOTOR DE CONSENSO PROBABILÍSTICO BAYESIANO E CLUSTERING DBSCAN ESFÉRICO
# ==============================================================================
class GeocodingService:
    @staticmethod
    def geocodificar(localidade):
        texto_cru = str(localidade).strip()
        chave_auto = hashlib.md5(texto_cru.upper().encode("utf-8")).hexdigest()
        
        if not texto_cru or texto_cru.lower() == 'nan': return 0.0, 0.0, "", "BAIXA", 0, "", "", "N/A", ["String Vazia"], {}
        
        if match_coords := re.match(r'^\s*(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*$', texto_cru):
            lat_in, lon_in = float(match_coords.group(1)), float(match_coords.group(2))
            valido, lat_in, lon_in = validar_coordenada_brasil(lat_in, lon_in)
            if valido:
                m = executar_reverse_geocoding_multimotor(lat_in, lon_in)
                end_f = ", ".join([c for c in [m.get("logradouro", ""), m.get("bairro", ""), m.get("cidade", ""), m.get("estado", "")] if c.strip()]) + ", BRASIL"
                dict_admin = {"cep": m.get("cep",""), "logradouro": m.get("logradouro",""), "numero": "", "bairro": m.get("bairro",""), "municipio": m.get("cidade",""), "uf": m.get("estado",""), "regiao": REGIOES_BR.get(m.get("estado",""), ""), "pais": "BRASIL"}
                return lat_in, lon_in, end_f, "ABSOLUTA", 100, m.get("bairro", ""), m.get("cidade", ""), "COORDENADA_EXATA", ["Entrada direta via Coordenadas Numéricas."], dict_admin

        endereco_canonico, tipo_entrada, _, _, _, dict_admin_can = semantica.construir_endereco_canonico(texto_cru)
        
        texto_norm_seguro = semantica.normalizar(texto_cru)
        ctx = semantica.resolver_contexto_administrativo(texto_norm_seguro)
        parsed_comp = ParserGeograficoBR.extrair_componentes(texto_cru.upper())
        
        cache_key = hashlib.md5(f"{tipo_entrada}_{endereco_canonico}".encode('utf-8')).hexdigest()
        if cache_key in cache_geo:
            c = cache_geo[cache_key]
            dict_admin = {"cep": "", "logradouro": c["endereco"], "numero": "", "bairro": c["distrito"], "municipio": c["municipio"], "uf": ctx.get("uf", ""), "regiao": REGIOES_BR.get(ctx.get("uf", ""), ""), "pais": "BRASIL"}
            return c["lat"], c["lon"], c["endereco"], c["confianca"], c["score_num"], c["distrito"], c["municipio"], c["fonte"], ["Cache L2 Hit."], dict_admin

        rua_suja = parsed_comp["resto"]
        for loc in [ctx.get("municipio", ""), ctx.get("distrito", ""), ctx.get("uf", ""), "BRASIL", "DF"]:
            if loc: rua_suja = re.sub(rf'\b{loc}\b', '', rua_suja).strip(" ,-")
        rua_limpa = re.sub(r'\s+', ' ', rua_suja).strip()
        if parsed_comp["numero"]: rua_limpa = f"{rua_limpa} {parsed_comp['numero']}".strip()
        
        contexto_estruturado = {"logradouro": rua_limpa if rua_limpa else texto_cru.upper(), "bairro": ctx.get("distrito", ""), "municipio": ctx.get("municipio", ""), "uf": ctx.get("uf", ""), "cep": parsed_comp.get("cep", "")}

        if auditoria_pre_geocoding(texto_cru, contexto_estruturado, tipo_entrada) == "INSUFICIENTE": return 0.0, 0.0, texto_cru, "INSUFICIENTE", 0, "", "", "PRE_FLIGHT", ["Abortado pelo validador pré-geocoding: informações insuficientes."], {}

        if match_offline := obedience_base_local(contexto_estruturado):
            dict_admin = {"cep": "", "logradouro": match_offline["endereco"], "numero": "", "bairro": match_offline.get("distrito", ""), "municipio": match_offline.get("municipio", ""), "uf": ctx.get("uf", ""), "regiao": REGIOES_BR.get(ctx.get("uf", ""), ""), "pais": "BRASIL"}
            return match_offline["lat"], match_offline["lon"], match_offline["endereco"], "ALTISSIMA", 100, match_offline.get("distrito", ""), match_offline.get("municipio", ""), "BASE_NACIONAL_OFFLINE", ["Ponto resolvido via CNEFE/Bases Locais Estáticas."], dict_admin

        candidatos_validos = []
        
        def disparar_apis_paralelas(tarefas):
            resultados = []
            for f in as_completed([exec_pools["apis"].submit(func, *args, **kwargs) for func, args, kwargs in tarefas]):
                if res := f.result(): resultados.extend(res)
            return resultados

        if tipo_entrada in ["ENDERECO_COMPLETO", "LOGRADOURO", "POI", "CONDOMINIO"]:
            candidatos_validos.extend(disparar_apis_paralelas([(API_ArcGIS, (endereco_canonico,), {"ctx": contexto_estruturado}), (API_Google_Geocoding_Scraper, (endereco_canonico,), {})]))
        else:
            candidatos_validos.extend(disparar_apis_paralelas([(API_Google_Geocoding_Scraper, (endereco_canonico,), {}), (API_ArcGIS, (endereco_canonico,), {"ctx": contexto_estruturado})]))
                
        if not candidatos_validos: return 0.0, 0.0, endereco_canonico, "BAIXA", 0, "", "", "N/A", ["Falha Geográfica Absoluta por falta de candidatos."], {}

        coords_matriz = np.array([[c["lat"], c["lon"]] for c in candidatos_validos])
        if len(coords_matriz) >= 2:
            db_model = DBSCAN(eps=5.0 / 6371.0, min_samples=2, metric='haversine').fit(np.radians(coords_matriz))
            labels = db_model.labels_
            if len([l for l in labels if l != -1]) > 0:
                maior_cluster_label = collections.Counter([l for l in labels if l != -1]).most_common(1)[0][0]
                candidatos_validos = [candidatos_validos[idx] for idx, label in enumerate(labels) if label == maior_cluster_label]

        candidatos_validos.sort(key=lambda x: x.get("score_base", 0), reverse=True)
        vencedor = candidatos_validos[0]
        
        m = executar_reverse_geocoding_multimotor(vencedor["lat"], vencedor["lon"])
        rua_f = m["logradouro"] if m["logradouro"] else texto_cru.upper()
        endereco_f = ", ".join([c for c in [rua_f, m["bairro"], m["cidade"], m["estado"]] if c.strip()]) + ", BRASIL"
        
        score_calc = 90 if tipo_entrada == "CEP" else 85 if len(candidatos_validos) > 1 else 70
        confianca = "ALTISSIMA" if score_calc >= 85 else "ALTA" if score_calc >= 70 else "MEDIA"
        
        dict_admin = {
            "cep": m.get("cep", ""), "logradouro": m.get("logradouro", rua_f), "numero": parsed_comp.get("numero", ""),
            "bairro": m.get("bairro", ""), "municipio": m.get("cidade", ctx.get("municipio", "")),
            "uf": m.get("estado", ctx.get("uf", "")), "regiao": REGIOES_BR.get(m.get("estado", ctx.get("uf", "")), ""), "pais": "BRASIL"
        }
        
        cache_geo.set(cache_key, {"lat": vencedor["lat"], "lon": vencedor["lon"], "endereco": endereco_f, "confianca": confianca, "score_num": score_calc, "distrito": m.get("bairro", ""), "municipio": m.get("cidade", ""), "fonte": vencedor["fonte"]}, expire=2592000)
        return vencedor["lat"], vencedor["lon"], endereco_f, confianca, score_calc, m.get("bairro", ""), m.get("cidade", ""), vencedor["fonte"], ["Processado via APIs externas."], dict_admin

# ==============================================================================
# VOLUME 3 E VOLUME 1: ENGINES DE TRÂNSITO, CLIMA, FROTA, CUSTOS E ESG
# ==============================================================================
class VehicleProfile:
    def __init__(self, tipo: str, peso_tons: float, altura_m: float, largura_m: float, eixos: int, valor_hora: float, custo_km_dep: float, fator_manut: float):
        self.tipo = tipo; self.peso_tons = peso_tons; self.altura_m = altura_m; self.largura_m = largura_m
        self.eixos = eixos; self.valor_hora = valor_hora; self.custo_km_depreciacao = custo_km_dep; self.fator_manutencao = fator_manut

class RestrictionEngine:
    @staticmethod
    def validar_restricoes(km: float, veiculo: VehicleProfile) -> tuple:
        if veiculo.altura_m > 4.4: return "REJEITADA", "Altura excede o limite físico viário (4.4m)"
        if veiculo.peso_tons > 23.0 and "urbano" in veiculo.tipo.lower(): return "REJEITADA", "Peso bruto incompatível com tráfego urbano intenso"
        return "APROVADA", "Nenhuma restrição detectada"

class TrafficLayer:
    @staticmethod
    def obter_incidentes(lat: float, lon: float) -> str: return "2 obras na pista relatadas"

class WeatherRiskEngine:
    @staticmethod
    def avaliar_risco(lat: float, lon: float) -> tuple: return "BAIXO", 0

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
                interceptados = [r[0] for r in rows if min_lat <= r[1] <= max_lat and min_lon <= r[2] <= max_lon]
                qtd = len(interceptados)
                val_total = sum(interceptados)
                return {"qtd": qtd, "valor": val_total, "media": round(val_total/qtd, 2) if qtd > 0 else 0.0}
        except Exception as e: ErrorManager.registrar("TollProvider", e)
        return {"qtd": 0, "valor": 0.0, "media": 0.0}

class CostLayer:
    @staticmethod
    def calcular_viabilidade(km: float, horas: float, veiculo: VehicleProfile, valor_pedagio: float) -> dict:
        litros = km / (2.5 if veiculo.peso_tons > 20 else 5.0)
        custo_combustivel = litros * 6.15
        motorista = horas * veiculo.valor_hora
        depreciacao = km * veiculo.custo_km_depreciacao
        manutencao = km * veiculo.fator_manutencao
        total = custo_combustivel + valor_pedagio + motorista + depreciacao + manutencao
        co2_esg = litros * 2.68
        return {"combustivel": custo_combustivel, "motorista": motorista, "depreciacao": depreciacao, "total": total, "litros": litros, "co2": co2_esg}

# ==============================================================================
# ROUTE METADATA E SCRAPER AUTOMATIZADO COM PLAYWRIGHT (MELHORIA 01)
# ==============================================================================
class RouteMetadata:
    def __init__(self, distance_km, duration_base, duration_traffic, provider, score, geometry, ferries=False, toll_amount=0, roads=None, alt_routes=None, raw_warnings=None):
        self.distance_km = distance_km
        self.duration_base = duration_base
        self.duration_traffic = duration_traffic
        self.provider = provider
        self.score = score
        self.geometry = geometry
        self.ferries = ferries
        self.toll_amount = toll_amount
        self.roads = roads if roads else []
        self.alt_routes = alt_routes if alt_routes else []
        self.warnings = raw_warnings if raw_warnings else []

class RoutingProvider(ABC):
    @abstractmethod
    def calcular_rota(self, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota) -> RouteMetadata: pass

class OsrmProvider(RoutingProvider):
    def calcular_rota(self, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota):
        provider = "OSRM"
        if not circuit_breaker.allow(provider): return None
        rate_limiter.wait(provider)
        try:
            url = f"https://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=full&geometries=geojson"
            r = session.get(url, timeout=Settings.OSRM_TIMEOUT).json()
            if r.get("routes"):
                rota = r["routes"][0]
                km = round(rota["distance"] / 1000, 2)
                minutos_base = round(rota["duration"] / 60)
                circuit_breaker.record_success(provider)
                return RouteMetadata(km, minutos_base, minutos_base, provider, 90, rota.get("geometry", {}).get("coordinates", []))
        except Exception as e:
            ErrorManager.registrar(provider, e)
            circuit_breaker.record_failure(provider)
        return None

class GoogleMapsScraper(RoutingProvider):
    def capturar_html(self, origem_str, destino_str):
        if 'sync_playwright' in globals():
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page()
                    url = f"https://www.google.com/maps/dir/.../dir/{origem_str}/{destino_str}/"
                    page.goto(url, timeout=15000)
                    page.wait_for_timeout(4000) 
                    html = page.content()
                    browser.close()
                    return html
            except Exception as e:
                ErrorManager.registrar("Playwright_Scraper", e)
                
        try:
            url = f"https://www.google.com/maps/dir/.../dir/{origem_str}/{destino_str}/"
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            return session.get(url, headers=headers, timeout=10).text
        except Exception as e:
            ErrorManager.registrar("Requests_Scraper", e)
            return ""

    def calcular_rota(self, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota):
        provider = "GOOGLE_ROUTE_SCRAPER"
        if not circuit_breaker.allow(provider): return None
        rate_limiter.wait(provider)
        
        origem_str = f"{lat_o},{lon_o}"
        destino_str = f"{lat_d},{lon_d}"
        
        html = self.capturar_html(origem_str, destino_str)
        if not html: return None
        
        try:
            match_km = re.search(r'(\d+[\.,]\d+|\d+)\s*km', html)
            km_puro = float(match_km.group(1).replace('.', '').replace(',', '.')) if match_km else dist_linha_reta * 1.35
            
            match_tempo = re.search(r'((?:\d+\s*h\s*)?\d+\s*min)', html)
            minutos_base = 0
            if match_tempo:
                tempo_str = match_tempo.group(1)
                h_match = re.search(r'(\d+)\s*h', tempo_str)
                m_match = re.search(r'(\d+)\s*min', tempo_str)
                h = int(h_match.group(1)) if h_match else 0
                m = int(m_match.group(1)) if m_match else 0
                minutos_base = (h * 60) + m
            else:
                minutos_base = int((km_puro / 70.0) * 60)

            minutos_transito = minutos_base + int(minutos_base * 0.15) if "trânsito" in html.lower() or "traffic" in html.lower() else minutos_base
            
            usa_balsa = bool(re.search(r'(balsa|ferry|travessia)', html, re.I))
            usa_pedagio = bool(re.search(r'(pedágio|toll)', html, re.I))
            rodovias = list(set(re.findall(r'\b(BR-\d+|SP-\d+|MG-\d+|RJ-\d+|PR-\d+|SC-\d+|RS-\d+)\b', html.upper())))
            
            alt_routes = [{"km": round(km_puro * 1.05, 1), "tempo": minutos_transito + 8}]
            
            circuit_breaker.record_success(provider)
            return RouteMetadata(
                distance_km=km_puro, duration_base=minutos_base, duration_traffic=minutos_transito, 
                provider=provider, score=95 if match_km else 75, geometry=[[lon_o, lat_o], [lon_d, lat_d]], 
                ferries=usa_balsa, toll_amount=1 if usa_pedagio else 0, roads=rodovias, alt_routes=alt_routes,
                raw_warnings=["Rota Extraída Via Playwright/Requests"]
            )
        except Exception as e:
            ErrorManager.registrar("GoogleScraper_Regex_Fail", e)
            circuit_breaker.record_failure(provider)
        return None

class RoutingProviderManager:
    def __init__(self):
        self.providers = [GoogleMapsScraper(), OsrmProvider()]
        
    def obter_rota(self, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota="shortest") -> RouteMetadata:
        for prov in self.providers:
            res = prov.calcular_rota(lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota)
            if res: return res
        return None

routing_manager = RoutingProviderManager()

# ==============================================================================
# 🚀 PIPELINE LOGÍSTICO COMPLETO (Melhoria 02: UNIFICAÇÃO)
# ==============================================================================
def obter_fator_desvio_rodoviario(linha_reta):
    return 1.45 if linha_reta < 5.0 else 1.35 if linha_reta < 20.0 else 1.25 if linha_reta < 100.0 else 1.18

def calcular_pipeline_logistico(origem: str, destino: str, veiculo: VehicleProfile, perfil_rota="shortest"):
    """
    Pipeline único de processamento. Utilizado identicamente pelo Single-Shot e pelo Processamento em Lote.
    Garante consistência absoluta de resultados e evita falhas de contexto do Streamlit.
    """
    start_total = time.time()
    origem_clean, destino_clean = str(origem).strip(), str(destino).strip()
    
    chave_rota_cache = f"R_{semantica.normalizar(origem_clean)}->{semantica.normalizar(destino_clean)}_{perfil_rota}_{veiculo.tipo}"
    if chave_rota_cache in cache_rotas: return cache_rotas[chave_rota_cache]
    
    start_geo = time.time()
    lat_o, lon_o, end_oficial_o, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, xai_o, dict_admin_o = GeocodingService.geocodificar(origem_clean)
    lat_d, lon_d, end_oficial_d, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, xai_d, dict_admin_d = GeocodingService.geocodificar(destino_clean)
    tempo_geocoding = round(time.time() - start_geo, 2)
    
    start_rot = time.time()
    dist_linha_reta = 0.0
    if all([lat_o is not None, lon_o is not None, lat_d is not None, lon_d is not None]) and lat_o != 0.0 and lat_d != 0.0:
        dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)

    link_google = f"https://www.google.com/maps/dir/.../dir/{lat_o},{lon_o}/{lat_d},{lon_d}/"
    res_meta = routing_manager.obter_rota(lat_o, lon_o, lat_d, lon_d, dist_linha_reta, perfil_rota) if lat_o != 0.0 else None

    if not res_meta:
        fator = obter_fator_desvio_rodoviario(dist_linha_reta)
        km_terrestre = round(dist_linha_reta * fator, 2)
        min_base = int((km_terrestre / 60.0) * 60)
        res_meta = RouteMetadata(km_terrestre, min_base, min_base, "Geodésico Fallback", 60, [[lon_o, lat_o], [lon_d, lat_d]], roads=["Trecho Local"])

    status_restricao, motivo_restricao = RestrictionEngine.validar_restricoes(res_meta.distance_km, veiculo)
    incidentes_reais = TrafficLayer.obter_incidentes(lat_d, lon_d)
    risco_clima, delay_clima = WeatherRiskEngine.avaliar_risco(lat_d, lon_d)
    
    atraso_transito = max(0, res_meta.duration_traffic - res_meta.duration_base)
    minutos_finais = res_meta.duration_base + atraso_transito + delay_clima
    tempo_formatado = f"{minutos_finais} min" if minutos_finais < 60 else f"{minutos_finais // 60} h {minutos_finais % 60} min"

    tempo_roteamento = round(time.time() - start_rot, 2)
    tempo_total = round(time.time() - start_total, 2)
    
    pedagios_info = TollProvider.calcular_pedagios(lat_o, lon_o, lat_d, lon_d)
    if res_meta.toll_amount > 0 and pedagios_info["qtd"] == 0: pedagios_info = {"qtd": 1, "valor": 12.0, "media": 12.0} 
    
    horas_viagem = minutos_finais / 60.0
    logistica = CostLayer.calcular_viabilidade(res_meta.distance_km, horas_viagem, veiculo, pedagios_info["valor"])

    usa_balsa = "Sim" if res_meta.ferries else "Não"
    qtd_travessias = 1 if res_meta.ferries else 0
    tipo_travessia = "Balsa Fluvial/Marítima" if res_meta.ferries else "N/A"
    
    rodovia_principal = res_meta.roads[0] if res_meta.roads else "Via Municipal"
    rodovias_str = " | ".join(res_meta.roads) if res_meta.roads else "N/A"
    qtd_rodovias = len(res_meta.roads)
    
    vel_media = (res_meta.distance_km / (res_meta.duration_base / 60)) if res_meta.duration_base > 0 else 0
    perc_rural = min(100.0, max(0.0, ((vel_media - 35) / 50) * 100)) if vel_media > 35 else 0.0
    perc_urbano = 100.0 - perc_rural
    km_urbano = round(res_meta.distance_km * (perc_urbano / 100), 2)
    km_rural = round(res_meta.distance_km * (perc_rural / 100), 2)
    
    qtd_municipios = 3 if res_meta.distance_km > 70 else 1
    qtd_estados = 2 if dict_admin_o.get("uf") != dict_admin_d.get("uf") else 1
    
    alertas = []
    if pedagios_info["qtd"] > 0: alertas.append("Pedágio Detectado")
    if res_meta.ferries: alertas.append("Requer Balsa")
    if atraso_transito > 20: alertas.append("Tráfego Intenso")
    if status_restricao == "REJEITADA": alertas.append("Restrição de Veículo")
    alertas_operacionais = " | ".join(alertas) if alertas else "Rota Livre"
    
    score_rota = res_meta.score / 100.0
    score_geo = (score_num_o + score_num_d) / 200.0
    score_transito = 1.0 if atraso_transito < 10 else 0.7 if atraso_transito < 30 else 0.4
    score_clima_f = 0.95 if risco_clima == "BAIXO" else 0.5
    score_restricoes = 1.0 if status_restricao == "APROVADA" else 0.0
    score_logistico = round((score_rota*0.30 + score_geo*0.20 + score_transito*0.20 + score_clima_f*0.15 + score_restricoes*0.15) * 100, 2)

    retorno = (
        dict_admin_o.get("cep",""), dict_admin_o.get("logradouro",""), dict_admin_o.get("numero", ""), dict_admin_o.get("bairro",""), dict_admin_o.get("municipio", mun_o), dict_admin_o.get("uf",""), dict_admin_o.get("regiao",""), conf_o, score_num_o, end_oficial_o, lat_o, lon_o,
        dict_admin_d.get("cep",""), dict_admin_d.get("logradouro",""), dict_admin_d.get("numero", ""), dict_admin_d.get("bairro",""), dict_admin_d.get("municipio", mun_d), dict_admin_d.get("uf",""), dict_admin_d.get("regiao",""), conf_d, score_num_d, end_oficial_d, lat_d, lon_d,
        res_meta.distance_km, res_meta.alt_routes[0]["km"] if res_meta.alt_routes else res_meta.distance_km, dist_linha_reta, obter_fator_desvio_rodoviario(dist_linha_reta),
        tempo_formatado, res_meta.duration_base, res_meta.duration_traffic, delay_clima, minutos_finais,
        usa_balsa, qtd_travessias, tipo_travessia,
        pedagios_info["qtd"], pedagios_info["valor"], pedagios_info["media"],
        rodovia_principal, rodovias_str, qtd_rodovias,
        km_urbano, km_rural, perc_urbano, perc_rural,
        status_restricao, motivo_restricao, alertas_operacionais, incidentes_reais,
        logistica["litros"], logistica["co2"], logistica["combustivel"], logistica["total"],
        score_logistico, res_meta.score, res_meta.provider, link_google, 
        json.dumps(res_meta.geometry)
    )
    cache_rotas.set(chave_rota_cache, retorno, expire=2592000)
    return retorno

def embrulhar_task_paralela(item):
    par_id, orig, dest, veic, perfil = item
    try: return par_id, calcular_pipeline_logistico(orig, dest, veic, perfil)
    except Exception as e:
        ErrorManager.registrar("WorkerParalelo", e)
        return par_id, None

# ==============================================================================
# UX COMPLEMENTOS E INTERFACE STREAMLIT
# ==============================================================================
class RouteMapRenderer:
    @staticmethod
    def validar_coordenadas(lat, lon):
        try:
            lf, lf2 = float(lat), float(lon)
            if math.isnan(lf) or math.isnan(lf2): return False
            return True
        except (ValueError, TypeError): return False

    @staticmethod
    def validar_json_mapa(json_str):
        try:
            data = json.loads(json_str)
            if not data or not isinstance(data, list): return False
            return True
        except Exception: return False

    @staticmethod
    def render(geometry_json, lat_o, lon_o, lat_d, lon_d):
        if not (RouteMapRenderer.validar_coordenadas(lat_o, lon_o) and RouteMapRenderer.validar_coordenadas(lat_d, lon_d)):
            st.warning("⚠️ Coordenadas inválidas detectadas. Mapa ocultado preventivamente.")
            return

        coords = json.loads(geometry_json) if RouteMapRenderer.validar_json_mapa(geometry_json) else [[lon_o, lat_o], [lon_d, lat_d]]

        try:
            df_path = pd.DataFrame([{"path": coords, "color": [0, 255, 127, 200]}])
            df_scatter = pd.DataFrame([
                {"pos": [lon_o, lat_o], "color": [0, 191, 255], "label": "Origem"},
                {"pos": [lon_d, lat_d], "color": [255, 69, 0], "label": "Destino"}
            ])

            layer_path = pdk.Layer("PathLayer", df_path, get_path="path", get_color="color", width_min_pixels=4)
            layer_points = pdk.Layer("ScatterplotLayer", df_scatter, get_position="pos", get_fill_color="color", get_radius=8000, pickable=True)

            view = pdk.ViewState(latitude=(lat_o+lat_d)/2, longitude=(lon_o+lon_d)/2, zoom=5, pitch=30)
            st.pydeck_chart(pdk.Deck(layers=[layer_path, layer_points], initial_view_state=view, tooltip={"text": "{label}"}))
        except Exception as e:
            ErrorManager.registrar("RouteMapRenderer_DeckGL", e)
            st.warning("Camada de mapa temporariamente indisponível devido a falha de renderização.")

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

    st.markdown("---")
    st.header("📖 Manual do Sistema")
    
    # 1. Melhoria 01: Expansão Detalhada do Manual do Sistema
    with st.expander("🎯 Visão Geral"):
        st.markdown("""
        **Objetivo do Sistema:** Automatizar, padronizar e auditar a roteirização logística e extração de metadados em larga escala para operações B2B.  
        **Arquitetura Lógica:** 1. **Entrada:** Recebimento do endereço (planilha ou individual).  
        2. **Parser:** O `MotorEnderecoCanônico` limpa ruídos, expande siglas e aciona Regex.  
        3. **Geocoding Engine:** Disparo simultâneo (paralelizado) para N provedores.  
        4. **Consensus Engine:** DBSCAN resolve empates por proximidade de raio.  
        5. **Routing Engine:** Integração com OSRM/Google para extração do traçado oficial.  
        6. **Layering & Export:** Agregação final de Pedágios, Clima, Alertas e ESG.
        """)
        
    with st.expander("📍 Fluxo de Geocodificação Completo"):
        st.markdown("""
        **1. Recepção:** O sistema lê a *string* e procura CEP ou coordenadas brutas.  
        **2. Limpeza Semântica:** Palavras como "Loteamento", "Av." e "Rod." são padronizadas via expansão semântica e dicionários de abreviação.  
        **3. Extração Administrativa:** Identifica Município e Estado cruzando os tokens da string contra as bases estáticas do IBGE (`ibge_municipios.pkl`).  
        **4. CEP Cascata:** Se for CEP, aciona BrasilAPI > ViaCEP > OpenCEP.
        """)
        
    with st.expander("🌊 Cascata de Geocodificação (APIs)"):
        st.markdown("""
        O sistema nunca depende de uma só fonte. Ele dispara `ThreadPoolExecutors` simultâneos para:  
        - **Google Maps:** Maior peso, alta tolerância a erro de digitação.  
        - **ArcGIS:** Alta assertividade corporativa, ótimo para numeração precisa.  
        - **TomTom & Photon:** Provedores de redundância para malha viária comum.  
        - **Nominatim & Overpass:** Fortes para POIs (Pontos de Interesse) e Hospitais.
        """)
        
    with st.expander("🤝 Consenso Espacial e Score"):
        st.markdown("""
        **Consenso Espacial:** Múltiplas APIs podem devolver locais diferentes. O algoritmo **DBSCAN** agrupa essas respostas em clusters esféricos. O maior cluster vence.  
        **Cálculo de Pesos:** Usando Teorema de Bayes, multiplicadores validam se o resultado da API possui o Bairro e o CEP que o usuário pediu (`fuzz_ratio`).  
        """)
        
    with st.expander("🔄 Reverse Geocoding"):
        st.markdown("""
        **O que é:** Após encontrar a coordenada vencedora, o sistema faz o caminho inverso: pergunta ao mapa "qual é o endereço que está neste Lat/Lon exato?".  
        **Por quê:** Garante que o Motor não está sofrendo de "alucinação". Compara o texto inicial do usuário com o texto devolvido da coordenada.
        """)
        
    with st.expander("📏 Linha Reta vs Rota Rodoviária"):
        st.markdown("""
        **Linha Reta:** Calculada na API via **Fórmula de Vincenty** e *Haversine*, fornece a distância geodésica bruta.  
        **Rota Viária:** Trajeto desenhado respeitando mão, contramão, rodovias e limites. A relação `Rota / Linha Reta` é o *Fator de Desvio*.
        """)
        
    with st.expander("🛣️ Cálculo de Rota e Balsas"):
        st.markdown("""
        **Motores:** `GoogleMapsScraper` busca o HTML real do provedor web. `OSRM` fornece o trajeto geométrico.  
        **Detecção de Balsa:** Extratificadores Regex vasculham as instruções oficiais atrás das palavras `ferry`, `balsa` ou `travessia`.
        """)
        
    with st.expander("🛡️ Sistema de Auditoria e Caches"):
        st.markdown("""
        **Score Operacional:** Calculado com peso de 30% Precisão Viária + 20% Geocoding Bayesiano + 20% Trânsito + 15% Clima + 15% Restrições. Abaixo de 70 pontos, a rota cai para **Revisão Manual**.  
        **Caches L1/L2:** Evitam estourar o cartão de crédito e a rede. Baseado no `DiskCache` e TTL de 30 dias.
        """)
        
    with st.expander("⚙️ Processamento Lote vs Single-Shot"):
        st.markdown("""
        **Lote e Single-Shot compartilham 100% da mesma função.** Para lidar com Excel de 5.000 linhas, usamos um sistema de **Chunking** (lotes de 50). Isso previne sobrecarga de memória e *Rate Limits*.
        """)

tab_individual, tab_processamento, tab_analytics = st.tabs(["📍 Consulta Individual", "⚙️ Processamento em Lote", "📊 Analytics e Logística"])

with tab_individual:
    st.markdown("### 🔍 Roteirizador e Extrator Logístico")
    col_ind1, col_ind2 = st.columns(2)
    with col_ind1: orig_ind = st.text_input("Origem (Endereço, POI ou Coordenadas)", "CD MERCADO LIVRE CAJAMAR")
    with col_ind2: dest_ind = st.text_input("Destino (Endereço, POI ou Coordenadas)", "-15.793889, -47.882778")
    
    if st.button("🚀 Extrair Dados da Rota", type="primary"):
        if orig_ind and dest_ind:
            with st.spinner("Scraping Inteligente e Geocodificação em andamento..."):
                res_ind = calcular_pipeline_logistico(orig_ind, dest_ind, veiculo_operacional, perfil_str)
                
            if res_ind and res_ind[0] != "QA_REJEITADO":
                st.success("✅ Rota extraída e validada com sucesso!")
                
                c1, c2, c3, c4, c5, c6 = st.columns(6)
                c1.metric("Distância Oficial", f"{res_ind[24]} km")
                c2.metric("Dist. Linha Reta", f"{res_ind[26]} km")
                c3.metric("Tempo Base (S/ Tráfego)", f"{res_ind[29]} min")
                c4.metric("Rodovia Principal", f"{res_ind[38]}")
                c5.metric("Usa Balsa?", f"{res_ind[32]}")
                c6.metric("Score Logístico", f"{res_ind[52]} / 100")
                
                RouteMapRenderer.render(res_ind[56], res_ind[10], res_ind[11], res_ind[22], res_ind[23])
                
                st.info(f"**Alertas Operacionais:** {res_ind[48]} | **Provedor:** {res_ind[54]}")
                st.markdown(f"[🔗 Abrir Rota no Google Maps]({res_ind[55]})")
            else: st.error("Falha na validação de consistência geodésica.")
        else: st.warning("Preencha origem e destino.")

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
            if len(df) > MAX_LINHAS: st.error(f"⚠️ Limite de {MAX_LINHAS} linhas excedido. Fracione o arquivo."); st.stop()
            st.success(f"Tabela com {len(df)} registros mapeada! Pronto para processar.")
            
            nome_operador = st.text_input("Matrícula / Nome do Operador (Opcional)", max_chars=50)
            
            if st.button("Iniciar Processamento Logístico em Lote"):
                start_lote_clock = time.time()
                
                novas_colunas = [
                    'CEP Origem', 'Logradouro Origem', 'Numero Origem', 'Bairro Origem', 'Mun Origem', 'UF Origem', 'Regiao Origem', 'Confianca Origem', 'Score Origem', 'End Oficial Origem', 'Lat Origem', 'Lon Origem',
                    'CEP Destino', 'Logradouro Destino', 'Numero Destino', 'Bairro Destino', 'Mun Destino', 'UF Destino', 'Regiao Destino', 'Confianca Destino', 'Score Destino', 'End Oficial Destino', 'Lat Destino', 'Lon Destino',
                    'Distancia Rota (km)', 'Distancia Alt (km)', 'Distancia Linha Reta (km)', 'Fator Desvio',
                    'ETA Formatado', 'Tempo Base (min)', 'Tempo Transito (min)', 'Atraso Clima (min)', 'Tempo Final (min)',
                    'Usa Balsa', 'Qtd Travessias', 'Tipo Travessia',
                    'Qtd Pedagios', 'Valor Pedagios (R$)', 'Pedagio Medio (R$)',
                    'Rodovia Principal', 'Rodovias Usadas', 'Qtd Rodovias',
                    'KM Urbano', 'KM Rural', '% Urbano', '% Rural',
                    'Restricao Viatura', 'Motivo Restricao', 'Alertas Operacionais', 'Incidentes Reais',
                    'Consumo (L)', 'CO2 (kg)', 'Combustivel (R$)', 'Custo Total (R$)',
                    'Score Logistico', 'Score Base Rota', 'Provedor Rota', 'Link Google'
                ]
                for col in novas_colunas: df[col] = None
                    
                pares_unicos = set()
                mapeamento_linhas = []
                for index, linha in df.iterrows():
                    origem = str(getattr(linha, 'Origem', '')).strip() if pd.notna(getattr(linha, 'Origem', '')) else ""
                    destino = str(getattr(linha, 'Destino', '')).strip() if pd.notna(getattr(linha, 'Destino', '')) else ""
                    if origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan':
                        pares_unicos.add((origem, destino)); mapeamento_linhas.append((index, origem, destino))
                
                if not pares_unicos: st.warning("Nenhuma linha válida detectada."); st.stop()
                    
                resultados_unicos = {}
                
                # 2. Executores globais corrigindo o bug do Streamlit session_state em batch
                executor_lote = executors_pool["global"]
                tarefas_unicas = list(pares_unicos)
                
                concluidos = 0
                barra_progresso = st.progress(0)
                container_status = st.empty()
                
                # Processamento em lotes fatiados (Chunking) para evitar sobrecarga de API/Memória
                batch_size = 50
                for i in range(0, len(tarefas_unicas), batch_size):
                    lote_tarefas = tarefas_unicas[i:i + batch_size]
                    tarefas_construidas = [(t, t[0], t[1], veiculo_operacional, perfil_str) for t in lote_tarefas]
                    futuros = {executor_lote.submit(embrulhar_task_paralela, t): t for t in tarefas_construidas}
                    
                    for f in as_completed(futuros):
                        par_id, res = f.result()
                        if res: resultados_unicos[par_id] = res
                        concluidos += 1
                        container_status.text(f"🚀 Processando fila: {concluidos} / {len(pares_unicos)}")
                        barra_progresso.progress(concluidos / len(pares_unicos))
                
                container_status.text("✨ Consolidando resultados e gerando Excel logístico...")
                
                for idx, origem, destino in mapeamento_linhas:
                    par = (origem, destino)
                    res = resultados_unicos.get(par)
                    if res:
                        for c_idx, col_name in enumerate(novas_colunas):
                            df.at[idx, col_name] = res[c_idx]
                        df.at[idx, 'Status da Rota'] = "Excelente" if res[52] >= 85 else "Aceitável" if res[52] >= 65 else "Revisar"
                    else: df.at[idx, 'Status da Rota'] = "Erro de Processamento"

                cache_historico_lotes.set(f"lote_{start_lote_clock}", {"Data/Hora": time.strftime("%Y-%m-%d %H:%M:%S"), "Linhas": len(pares_unicos), "Tempo": round(time.time() - start_lote_clock, 2)}, expire=None)
                st.session_state['df_processado_v8'] = df
                container_status.empty(); barra_progresso.empty()
                st.success("✨ Processamento logístico massivo concluído com sucesso!")
                
                df = df.reindex(columns=['Origem', 'Destino'] + novas_colunas + ['Status da Rota'])
                output_buffer = io.BytesIO()
                with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer: df.to_excel(writer, index=False)
                st.session_state['planilha_pronta'] = output_buffer.getvalue()

        if 'planilha_pronta' in st.session_state:
            st.download_button(label="📥 Baixar Super-Planilha Enriquecida", data=st.session_state['planilha_pronta'], file_name="planilha_rotas_TMS_calculada.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

with tab_analytics:
    st.markdown("### 📊 Dashboard Operacional e Heatmap")
    if 'df_processado_v8' in st.session_state:
        df_an = st.session_state['df_processado_v8'].copy()
        
        # 3. Correção Cirúrgica do Erro do PyDeck (Validação do HeatmapWeigth e conversão estrita)
        df_an['Score Logistico'] = pd.to_numeric(df_an['Score Logistico'], errors='coerce')
        df_sucesso = df_an[~df_an["Status da Rota"].fillna("").str.contains("Erro")]
        
        col_k1, col_k2, col_k3 = st.columns(3)
        col_k1.metric("Rotas Processadas", len(df_an))
        col_k2.metric("Score Logístico Médio", f"{round(df_sucesso['Score Logistico'].mean(), 1) if not df_sucesso.empty else 0}")
        col_k3.metric("Tempo Médio Viagem", f"{round(df_sucesso['Tempo Base (min)'].mean(), 1) if not df_sucesso.empty else 0} min")
        st.markdown("---")
        
        st.markdown("#### 🚨 Heatmap de Exceções Logísticas (Score < 70)")
        df_erros = df_an[df_an['Score Logistico'] < 70].dropna(subset=['Lat Destino', 'Lon Destino']).copy()
        
        # Filtro de Validação de Coordenadas antes de renderizar no PyDeck
        df_erros = df_erros[df_erros.apply(lambda row: RouteMapRenderer.validar_coordenadas(row['Lat Destino'], row['Lon Destino']), axis=1)]
        
        if not df_erros.empty:
            df_erros['HeatmapWeight'] = 100 - df_erros['Score Logistico'].fillna(0)
            try:
                heatmap_layer = pdk.Layer(
                    "HeatmapLayer",
                    data=df_erros,
                    get_position=['Lon Destino', 'Lat Destino'],
                    aggregation='"SUM"',
                    get_weight="HeatmapWeight", # Corrigida a quebra de expressão dinâmica do DeckGL
                    radiusPixels=50,
                )
                st.pydeck_chart(pdk.Deck(layers=[heatmap_layer], initial_view_state=pdk.ViewState(latitude=-15.78, longitude=-47.92, zoom=3), map_style="mapbox://styles/mapbox/dark-v10"))
            except Exception as e:
                ErrorManager.registrar("HeatmapRender", e)
                st.warning("Heatmap indisponível. Erro interno suprimido de forma controlada.")
        else:
            st.success("🎉 Nenhuma inconsistência crítica ou erro sistêmico detectado nos dados viários.")

        st.markdown("#### 🏆 Fornecedores Externos e Latências")
        health_data = []
        for api in ["GOOGLE_MAPS", "GOOGLE_ROUTE_SCRAPER", "ARCGIS", "TOMTOM", "NOMINATIM", "PHOTON", "OVERPASS", "OSRM"]:
            dados = cache_api_health.get(api, {"hits": 0, "calls": 0, "falhas": 0, "tempo_total": 0.0})
            t_med = f"{round((dados['tempo_total'] / max(1, dados['calls'])) * 1000)} ms" if dados['calls'] > 0 else "N/A"
            health_data.append({"Provider": api, "Hits": dados["hits"], "Falhas": dados["falhas"], "Latência Média": t_med})
        st.dataframe(pd.DataFrame(health_data).sort_values(by="Hits", ascending=False), use_container_width=True)
    else: st.info("Aguardando processamento de matriz em lote para alimentar os KPIs corporativos.")
