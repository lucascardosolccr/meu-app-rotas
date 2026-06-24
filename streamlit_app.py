import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import numpy as np
import requests
import time
import math
import io
import re
import os
import pickle
import collections
import hashlib
import json
import urllib.parse
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import altair as alt
import plotly.express as px
import plotly.graph_objects as go
from unidecode import unidecode
from rapidfuzz import process, fuzz
from diskcache import Cache
from sklearn.cluster import DBSCAN
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Motores Geodésicos Estratificados
try:
    from geographiclib.geodesic import Geodesic
    GEOGRAPHICLIB_DISPONIVEL = True
except ImportError:
    GEOGRAPHICLIB_DISPONIVEL = False

try:
    from geopy.distance import geodesic
    GEOPY_DISPONIVEL = True
except ImportError:
    GEOPY_DISPONIVEL = False

# ==============================================================================
# CONFIGURAÇÃO DE LOGS E AUDITORIA CRÍTICA
# ==============================================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MotorGeodesicoCorp")

METRICAS_DISTANCIA = {
    "total_calculos": 0,
    "sucesso_geographiclib": 0,
    "sucesso_geopy": 0,
    "fallback_haversine": 0,
    "correcoes_automaticas": 0,
    "falhas_criticas": 0,
    "cache_unpoisoned": 0,
    "barreira_territorial": 0,
    "desambiguacoes_estritas": 0
}

# ==============================================================================
# CONFIGURAÇÃO DE UI/UX E AMBIENTE
# ==============================================================================
st.set_page_config(page_title="Gerenciador de Rotas Inteligentes", page_icon="🚗", layout="wide")

st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    
    html, body, [class*="css"]  {
        font-family: 'Inter', sans-serif !important;
    }
    
    .stApp {
        background-color: #0E1117;
    }
    
    [data-testid="stSidebar"] {
        background-color: #161A25;
        border-right: 1px solid #2D3342;
    }
    
    [data-testid="stMetric"] {
        background-color: #1E232F;
        border: 1px solid #2D3342;
        padding: 1.2rem;
        border-radius: 8px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        border-left: 4px solid #3B82F6;
        transition: transform 0.2s ease-in-out, box-shadow 0.2s ease-in-out;
    }
    [data-testid="stMetric"]:hover {
        transform: translateY(-3px);
        box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.15), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
    }
    [data-testid="stMetricLabel"] {
        color: #9CA3AF !important;
        font-weight: 500;
        font-size: 0.95rem;
        margin-bottom: 0.5rem;
    }
    [data-testid="stMetricValue"] {
        color: #F9FAFB !important;
        font-weight: 700;
        font-size: 1.8rem;
    }
    [data-testid="stMetricDelta"] {
        font-size: 0.85rem;
    }
    
    [data-baseweb="tab-list"] {
        gap: 8px;
        background-color: transparent;
    }
    [data-baseweb="tab"] {
        background-color: #161A25;
        border: 1px solid #2D3342;
        border-bottom: none;
        border-radius: 8px 8px 0 0;
        padding: 12px 24px;
        color: #9CA3AF;
        font-weight: 600;
        transition: all 0.2s;
    }
    [data-baseweb="tab"]:hover {
        color: #F9FAFB;
        background-color: #1E232F;
    }
    [data-baseweb="tab"][aria-selected="true"] {
        background-color: #3B82F6;
        color: #FFFFFF;
        border-color: #3B82F6;
    }
    
    .stButton > button {
        border-radius: 6px;
        font-weight: 600;
        transition: all 0.2s;
    }
    .stButton > button[kind="primary"] {
        background-color: #3B82F6;
        color: white;
        border: none;
    }
    .stButton > button[kind="primary"]:hover {
        background-color: #2563EB;
        box-shadow: 0 4px 6px -1px rgba(59, 130, 246, 0.5);
    }
    
    [data-testid="stExpander"] {
        background-color: #1E232F;
        border: 1px solid #2D3342;
        border-radius: 8px;
    }
    [data-testid="stExpander"] summary {
        font-weight: 600;
        color: #E5E7EB;
    }
    
    [data-testid="stDataFrame"] {
        border: 1px solid #2D3342;
        border-radius: 8px;
        overflow: hidden;
    }
    
    .corporate-header {
        background: linear-gradient(135deg, #161A25 0%, #1E232F 100%);
        padding: 24px;
        border-radius: 12px;
        margin-bottom: 30px;
        border-left: 6px solid #3B82F6;
        box-shadow: 0 4px 6px rgba(0,0,0,0.2);
    }
    .corporate-title {
        color: #F9FAFB;
        margin: 0;
        font-weight: 700;
        font-size: 24px;
        letter-spacing: -0.5px;
    }
    .corporate-subtitle {
        color: #9CA3AF;
        margin: 5px 0 0 0;
        font-size: 15px;
        font-weight: 400;
    }
    
    .filter-badge {
        display: inline-block;
        background-color: #3B82F6;
        color: white;
        padding: 4px 10px;
        border-radius: 20px;
        font-size: 13px;
        font-weight: 600;
        margin-right: 8px;
        margin-bottom: 8px;
    }
</style>
""", unsafe_allow_html=True)

TOMTOM_API_KEY = "" # Insira sua credencial TomTom Logistics aqui

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

if "cache_limpo_v59" not in st.session_state:
    for c in [cache_classificacao, cache_fuzzy, cache_geo, cache_rotas, cache_poi, cache_cep, cache_google, cache_reverse, cache_base_local, cache_aprendizado, cache_aprendizado_auto, cache_api_health, cache_historico_lotes]:
        c.clear()
    st.session_state["cache_limpo_v59"] = True

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
retry_strategy = Retry(total=5, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)

session.cookies.set("CONSENT", "YES+cb.20230101-00-p0.pt-BR+FX+902", domain=".google.com.br")
session.cookies.set("CONSENT", "YES+cb.20230101-00-p0.pt-BR+FX+902", domain=".google.com")

CACHE_IBGE_PATH = "municipios_ibge.pkl"

# ==============================================================================
# 🎛️ INFRAESTRUTURA DE CONCORRÊNCIA E FILAS (THREAD-SAFE GLOBALS)
# ==============================================================================
WORKERS_DISPONIVEIS = 8

EXECUTOR_GLOBAL = ThreadPoolExecutor(max_workers=WORKERS_DISPONIVEIS)
FILA_NOMINATIM = ThreadPoolExecutor(max_workers=1)
EXECUTOR_APIS = ThreadPoolExecutor(max_workers=16)

# ==============================================================================
# 🎛️ DADOS GLOBAIS THREAD-SAFE E EXPANSÃO SEMÂNTICA
# ==============================================================================
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
                    if len(d.get("municipios", {})) > 1000:
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

                if len(base_mun) > 1000:
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
        
        self.via_keys = [
            "RUA", "AVENIDA", "TRAVESSA", "ALAMEDA", "RODOVIA", "ESTRADA", "QUADRA", 
            "SQN", "SQS", "SHIS", "SHIN", "SCRN", "SCS", "SRTVN", "CLS", "CLN",
            "QNL", "QNM", "QNN", "QNG", "QNJ", "QNK", "QI", "QE", "QC", "QR", "QS", "QSC"
        ]

    def normalizar(self, texto):
        if not texto or pd.isna(texto): return ""
        t_raw = str(texto).strip()
        
        chave_aprendizado = t_raw.upper()
        if chave_aprendizado in cache_aprendizado:
            dado_salvo = cache_aprendizado[chave_aprendizado]
            if isinstance(dado_salvo, str): 
                t_raw = dado_salvo

        t_raw = t_raw.replace(',', ' ').replace(';', ' ')

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
        
        ctx_temp = self.resolver_contexto_administrativo(texto_norm)
        mun_temp = ctx_temp.get("municipio", "")
        uf_temp = ctx_temp.get("uf", "")
        
        texto_limpo_mun = re.sub(rf'\b{uf_temp}\b', '', texto_norm).strip() if uf_temp else texto_norm
        texto_limpo_mun = texto_limpo_mun.replace("BRASIL", "").strip()

        if re.search(r'\b\d{5}-?\d{3}\b', texto_norm): tipo = "CEP"
        elif any(re.search(p, texto_norm) for p in self.condo_keys): tipo = "CONDOMINIO"
        elif any(k in texto_norm for k in POI_KEYWORDS): tipo = "POI"
        elif any(k in texto_norm for k in self.rural_keys): tipo = "RURAL"
        elif any(k in texto_norm for k in self.via_keys) and bool(re.search(r'\d+', texto_norm)): tipo = "ENDERECO_COMPLETO"
        elif any(k in texto_norm for k in self.bairro_keys): tipo = "BAIRRO"
        elif mun_temp and (texto_limpo_mun == mun_temp or texto_norm == mun_temp or texto_norm == f"{mun_temp} {uf_temp}"): tipo = "MUNICIPIO"
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
        uf_explicita = None
        
        for sigla in IBGE_ESTADOS.keys():
            if re.search(rf'\b{sigla}\b', texto_norm):
                uf_explicita = sigla
                break
        
        if not uf_explicita:
            for sigla, nome in IBGE_ESTADOS.items():
                if re.search(rf'\b{nome}\b', texto_norm):
                    uf_explicita = sigla
                    break

        resultado = {"uf": uf_explicita if uf_explicita else "", "municipio": "", "distrito": ""}

        cidades_para_busca = IBGE_MUNICIPIOS
        if uf_explicita:
            cidades_filtradas = {}
            for mun, lista_itens in IBGE_MUNICIPIOS.items():
                itens_uf = [i for i in lista_itens if i["uf"] == uf_explicita]
                if itens_uf:
                    cidades_filtradas[mun] = itens_uf
            cidades_para_busca = cidades_filtradas

        tokens = texto_norm.split()
        for i in range(len(tokens)):
            for j in range(i + 1, len(tokens) + 1):
                chunk = " ".join(tokens[i:j])
                if chunk in cidades_para_busca:
                    resultado.update({"uf": cidades_para_busca[chunk][0]["uf"], "municipio": chunk})
                    return resultado

        if uf_explicita and not resultado["municipio"]:
            chaves = list(cidades_para_busca.keys())
            if chaves:
                melhor_match = process.extractOne(texto_norm, chaves, scorer=fuzz.token_set_ratio)
                if melhor_match and melhor_match[1] >= 65:
                    resultado.update({"municipio": melhor_match[0]})
                    return resultado
        
        if not resultado["municipio"] and not uf_explicita and len(texto_norm) > 4:
            melhor_match_global = process.extractOne(texto_norm, LISTA_CONTEXTO_FUZZY, scorer=fuzz.WRatio)
            if melhor_match_global and melhor_match_global[1] >= 85:
                cidade_uf = melhor_match_global[0]
                resultado.update({"uf": cidade_uf.rsplit(' ', 1)[1], "municipio": cidade_uf.rsplit(' ', 1)[0]})
                    
        return resultado

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

        contexto_pre = self.resolver_contexto_administrativo(texto_norm)
        if not contexto_pre.get("municipio"):
            texto_fuzzy = self.aplicar_fuzzy_multidimensional(texto_norm)
            contexto = self.resolver_contexto_administrativo(texto_fuzzy)
        else:
            texto_fuzzy = texto_norm
            contexto = contexto_pre

        tipo = self.classificar_entrada(texto_fuzzy)
        endereco_canonico = texto_fuzzy if texto_fuzzy else texto_norm
        
        return endereco_canonico, tipo, "", 0.0, 0.0

semantica = MotorEnderecoCanônico()

# ==============================================================================
# 🧮 VALIDADOR PRÉ-GEOCODING E LÓGICA GEODÉSICA CORPORATIVA (MULTI-CAMADA)
# ==============================================================================

def parse_tempo_minutos(t_str):
    if not isinstance(t_str, str): return 999999
    try:
        h = re.search(r'(\d+)\s*h', t_str)
        m = re.search(r'(\d+)\s*min', t_str)
        horas = int(h.group(1)) if h else 0
        mins = int(m.group(1)) if m else 0
        if not h and not m:
            nums = re.findall(r'\d+', t_str)
            if nums: return int(nums[0])
            return 999999
        return horas * 60 + mins
    except Exception:
        return 999999

def validar_coordenada_brasil(lat, lon):
    try:
        lat_f, lon_f = float(lat), float(lon)
        if (-35.0 <= lat_f <= 6.0) and (-75.0 <= lon_f <= -28.0):
            return True, lat_f, lon_f
        if (-35.0 <= lon_f <= 6.0) and (-75.0 <= lat_f <= -28.0):
            return True, lon_f, lat_f 
        return False, lat_f, lon_f
    except (ValueError, TypeError):
        return False, 0.0, 0.0

def calcular_distancia_linha_reta(lat1, lon1, lat2, lon2, contexto=""):
    global METRICAS_DISTANCIA
    METRICAS_DISTANCIA["total_calculos"] += 1
    
    try:
        lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
        dist_final = 0.0
        status_final = ""
        
        if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0: 
            return 0.0, "Falha Operacional (Coordenadas Ausentes)"
            
        if lat1 == lat2 and lon1 == lon2: 
            return 0.0, "Calculada Normalmente (Pontos Coincidentes)"
            
        calculado_sucesso = False
        if GEOGRAPHICLIB_DISPONIVEL:
            try:
                dist_metros = Geodesic.WGS84.Inverse(lat1, lon1, lat2, lon2)['s12']
                dist_km = dist_metros / 1000.0
                if dist_km > 0:
                    METRICAS_DISTANCIA["sucesso_geographiclib"] += 1
                    dist_final, status_final = round(dist_km, 2), "Calculada via GeographicLib WGS-84"
                    calculado_sucesso = True
            except Exception as e:
                logger.warning(f"GeographicLib falhou: {e}")
                
        if not calculado_sucesso and GEOPY_DISPONIVEL:
            try:
                dist_km = geodesic((lat1, lon1), (lat2, lon2)).km
                if dist_km > 0:
                    METRICAS_DISTANCIA["sucesso_geopy"] += 1
                    dist_final, status_final = round(dist_km, 2), "Calculada via Geopy Geodesic"
                    calculado_sucesso = True
            except Exception as e:
                logger.warning(f"Geopy falhou: {e}")

        if not calculado_sucesso:
            lat1_r, lon1_r, lat2_r, lon2_r = map(math.radians, [lat1, lon1, lat2, lon2])
            dlat = lat2_r - lat1_r
            dlon = lon2_r - lon1_r
            a = math.sin(dlat / 2)**2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2)**2
            c = 2 * math.asin(math.sqrt(a))
            dist_haversine = 6371.0 * c
            
            if dist_haversine >= 0.01:
                METRICAS_DISTANCIA["fallback_haversine"] += 1
                dist_final, status_final = round(dist_haversine, 2), "Calculada via Fallback Haversine"
            else:
                logger.error(f"FALHA CRÍTICA PREVENIDA: Distância zerada para pontos diferentes. {lat1},{lon1} a {lat2},{lon2} | Ctx: {contexto}")
                METRICAS_DISTANCIA["correcoes_automaticas"] += 1
                dist_final, status_final = 0.01, "Calculada após reprocessamento (Correção Anti-Zero)"

        if dist_final > 5000.0:
            logger.error(f"ANOMALIA TERRITORIAL: Distância de {dist_final}km excede fisicamente os limites do Brasil. Ctx: {contexto}")
            METRICAS_DISTANCIA["barreira_territorial"] += 1
            return 0.01, "Falha de Bounding Box (Distância Transcontinental Impossível)"

        return dist_final, status_final

    except Exception as e:
        logger.error(f"Erro fatal no motor de distância geodésica ({contexto}): {e}")
        METRICAS_DISTANCIA["falhas_criticas"] += 1
        return 0.0, "Falha Operacional Crítica no Motor Geodésico"

def cascata_postal_tripla(cep_limpo):
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
    except Exception: pass
    try:
        def _nom_cep():
            time.sleep(1.1)
            url = f"https://nominatim.openstreetmap.org/search?format=json&postalcode={cep_limpo}&countrycodes=br&limit=1"
            return session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
        r_nom = FILA_NOMINATIM.submit(_nom_cep).result()
        if r_nom: lat, lon = float(r_nom[0]['lat']), float(r_nom[0]['lon'])
    except Exception: pass
    try:
        r = session.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=4).json()
        if "erro" not in r:
            d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon)
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception: pass
    try:
        r = session.get(f"https://opencep.com/v1/{cep_limpo}", timeout=4).json()
        if "error" not in r:
            d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon)
            cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception: pass
    return "", "", "", "", 0.0, 0.0

def validar_consistencia_administrativa(candidato, uf_inf):
    est_api = unidecode(candidato.get('estado', '')).upper().strip()
    if uf_inf and est_api:
        nome_estado_inf = unidecode(IBGE_ESTADOS.get(uf_inf, uf_inf)).upper()
        if uf_inf not in est_api and nome_estado_inf not in est_api:
            return False
    return True

def validar_consistencia_municipal(candidato, mun_inf):
    if not mun_inf: return True
    cid_api = unidecode(candidato.get('cidade', '')).upper().strip()
    if not cid_api: return True
    if mun_inf == cid_api or mun_inf in cid_api or cid_api in mun_inf: return True
    if fuzz.token_set_ratio(mun_inf, cid_api) >= 95: return True
    return False

def obter_coordenada_centroide_supremo(mun_nome, uf_nome):
    url_arc = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?City={requests.utils.quote(mun_nome)}&Region={requests.utils.quote(uf_nome)}&CountryCode=BRA&f=json&maxLocations=1"
    try:
        r = session.get(url_arc, timeout=5).json()
        if r.get('candidates'):
            cand = r['candidates'][0]
            lat_c, lon_c = float(cand['location']['y']), float(cand['location']['x'])
            if validar_coordenada_brasil(lat_c, lon_c)[0]: return lat_c, lon_c, "ARCGIS_CENTROIDE_SUPREMO"
    except: pass
    
    url_nom = f"https://nominatim.openstreetmap.org/search?city={requests.utils.quote(mun_nome)}&state={requests.utils.quote(uf_nome)}&country=Brazil&format=json&limit=1"
    try:
        r = session.get(url_nom, headers={"User-Agent": "RotasCorp/11.0"}, timeout=5).json()
        if r:
            lat_c, lon_c = float(r[0]['lat']), float(r[0]['lon'])
            if validar_coordenada_brasil(lat_c, lon_c)[0]: return lat_c, lon_c, "NOMINATIM_CENTROIDE_SUPREMO"
    except: pass
    return 0.0, 0.0, None

def obedience_base_local(contexto_estruturado):
    if contexto_estruturado["logradouro"] and contexto_estruturado["municipio"] and contexto_estruturado["uf"]:
        chave_cnefe = f"{contexto_estruturado['logradouro']}_{contexto_estruturado['municipio']}_{contexto_estruturado['uf']}"
        if chave_cnefe in cache_base_local:
            return cache_base_local[chave_cnefe]
    return None

# ==============================================================================
# 🗺️ MÓDULOS DE GEOCODIFICAÇÃO COM TELEMETRIA E MOTOR ANTI-COLISÃO
# ==============================================================================

def API_TomTom(query):
    if not TOMTOM_API_KEY: return None
    start_t = time.time()
    try:
        url = f"https://api.tomtom.com/search/2/geocode/{requests.utils.quote(query)}.json?key={TOMTOM_API_KEY}&countrySet=BR&limit=5"
        r = session.get(url, timeout=4).json()
        resultados = []
        if r.get("results"):
            for res in r["results"][:5]:
                pos = res.get("position", {})
                addr = res.get("address", {})
                resultados.append({
                    "lat": float(pos["lat"]), "lon": float(pos["lon"]), "fonte": "TOMTOM", "score_base": 35,
                    "cidade": addr.get("municipality", "").upper(), "estado": addr.get("countrySubdivision", "").upper(),
                    "bairro": addr.get("neighbourhood", addr.get("subdivision", "")).upper(), "logradouro": addr.get("streetName", "").upper(),
                    "numero": str(addr.get("streetNumber", "")).upper(), "cep": addr.get("postalCode", "").replace("-", "")
                })
            registrar_telemetria("TOMTOM", True, time.time() - start_t)
        return resultados if resultados else None
    except Exception: pass
    registrar_telemetria("TOMTOM", False, time.time() - start_t)
    return None

def executar_reverse_geocoding_multimotor(lat, lon):
    rev_key = f"V5_{round(lat,5)}|{round(lon,5)}"
    if rev_key in cache_reverse: return cache_reverse[rev_key]
    res = {"logradouro": "", "bairro": "", "cidade": "", "municipio": "", "distrito": "", "estado": "", "cep": ""}
    try:
        def _nom_rev():
            time.sleep(1.1)
            url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&addressdetails=1"
            return session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
        a = r_nom = FILA_NOMINATIM.submit(_nom_rev).result().get("address", {})
        res.update({"logradouro": a.get("road", a.get("pedestrian", "")), "bairro": a.get("neighbourhood", a.get("suburb", a.get("city_district", ""))), "cidade": a.get("city", a.get("town", a.get("municipality", ""))), "estado": a.get("state", "").upper(), "cep": a.get("postcode", "")})
        cache_reverse.set(rev_key, res, expire=2592000); return res
    except Exception: pass
    try:
        url_arc = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/reverseGeocode?location={lon},{lat}&f=json"
        r_arc = session.get(url_arc, timeout=4).json()
        if 'address' in r_arc:
            addr = r_arc['address']
            res.update({"logradouro": addr.get('Address', ''), "bairro": addr.get('Neighborhood', ''), "cidade": addr.get('City', ''), "estado": addr.get('RegionAbbr', '').upper(), "cep": addr.get('Postal', '')})
            cache_reverse.set(rev_key, res, expire=2592000)
    except Exception: pass
    return res

def API_ArcGIS(query, ctx=None):
    start_t = time.time()
    try:
        if ctx and (ctx.get("logradouro") or ctx.get("municipio")):
            end = requests.utils.quote(ctx.get("logradouro", ""))
            cid = requests.utils.quote(ctx.get("municipio", ""))
            uf = requests.utils.quote(ctx.get("uf", ""))
            bair = requests.utils.quote(ctx.get("bairro", ""))
            cep = requests.utils.quote(ctx.get("cep", ""))
            url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&Address={end}&Neighborhood={bair}&City={cid}&Region={uf}&Postal={cep}&maxLocations=5&sourceCountry=BRA&outFields=*"
        else:
            url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=5&sourceCountry=BRA&outFields=*"
            
        r = session.get(url, timeout=4).json()
        resultados = []
        if r.get('candidates'):
            for c in r['candidates'][:5]:
                attr = c.get('attributes', {})
                resultados.append({"lat": float(c['location']['y']), "lon": float(c['location']['x']), "fonte": "ARCGIS", "score_base": 30, "cidade": attr.get('City', '').upper(), "estado": attr.get('RegionAbbr', '').upper(), "bairro": attr.get('Neighborhood', '').upper(), "logradouro": attr.get('StName', attr.get('Address', '')).upper(), "numero": str(attr.get('AddNum', '')).upper(), "cep": attr.get('Postal', '')})
            registrar_telemetria("ARCGIS", True, time.time() - start_t)
        return resultados if resultados else None
    except Exception: pass
    registrar_telemetria("ARCGIS", False, time.time() - start_t)
    return None

def API_Nominatim(query, ctx=None):
    start_t = time.time()
    try:
        def _call_nom():
            time.sleep(1.1)
            if ctx and ctx.get("logradouro") and ctx.get("municipio"):
                rua = requests.utils.quote(ctx["logradouro"])
                cid = requests.utils.quote(ctx["municipio"])
                est = requests.utils.quote(ctx.get("uf", ""))
                url = f"https://nominatim.openstreetmap.org/search?format=json&street={rua}&city={cid}&state={est}&limit=5&addressdetails=1&countrycodes=br"
            else:
                url = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query)}&limit=5&addressdetails=1&countrycodes=br"
            return session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
            
        r = FILA_NOMINATIM.submit(_call_nom).result()
        resultados = []
        if r:
            for a in r[:5]:
                addr = a.get("address", {})
                resultados.append({"lat": float(a['lat']), "lon": float(a['lon']), "fonte": "NOMINATIM", "score_base": 25, "cidade": addr.get('city', addr.get('town', '')).upper(), "estado": addr.get('state', '').upper(), "bairro": addr.get('neighbourhood', addr.get('suburb', '')).upper(), "logradouro": addr.get('road', '').upper(), "numero": str(addr.get('house_number', '')).upper(), "cep": addr.get('postcode', '').replace("-", "")})
            registrar_telemetria("NOMINATIM", True, time.time() - start_t)
        return resultados if resultados else None
    except Exception: pass
    registrar_telemetria("NOMINATIM", False, time.time() - start_t)
    return None

def API_Photon(query):
    start_t = time.time()
    try:
        url = f"https://photon.komoot.io/api/?q={requests.utils.quote(query)}&limit=5&filter=countrycode:br"
        r = session.get(url, timeout=4).json()
        resultados = []
        if r.get("features"):
            for f in r["features"][:5]:
                lon, lat = f["geometry"]["coordinates"]
                props = f.get("properties", {})
                resultados.append({"lat": lat, "lon": lon, "fonte": "PHOTON", "score_base": 20, "cidade": props.get("city", "").upper(), "estado": props.get("state", "").upper(), "bairro": props.get("district", "").upper(), "logradouro": props.get("street", "").upper(), "numero": str(props.get("housenumber", "")).upper(), "cep": props.get("postcode", "").replace("-", "")})
            registrar_telemetria("PHOTON", True, time.time() - start_t)
        return resultados if resultados else None
    except Exception: pass
    registrar_telemetria("PHOTON", False, time.time() - start_t)
    return None

def forcar_geocodificacao_hierarquica_estrita(texto_cru):
    texto_norm = semantica.normalizar(texto_cru)
    candidatos = []
    
    with ThreadPoolExecutor(max_workers=3) as executor:
        f1 = executor.submit(API_ArcGIS, texto_norm)
        f2 = executor.submit(API_Nominatim, texto_norm)
        f3 = executor.submit(API_Photon, texto_norm)
        
        for f in as_completed([f1, f2, f3]):
            res = f.result()
            if res: candidatos.extend(res)
            
    if not candidatos: return None
    
    candidatos.sort(key=lambda x: (x.get('score_base', 0) + (40 if x.get('bairro') else 0) + (50 if x.get('logradouro') else 0)), reverse=True)
    melhor = candidatos[0]
    
    end_f = ", ".join([c for c in [melhor.get('logradouro', ''), melhor.get('bairro', ''), melhor.get('cidade', ''), melhor.get('estado', '')] if c.strip()]) + ", BRASIL"
    return (melhor['lat'], melhor['lon'], end_f, "DESAMBIGUACAO_ESTRITA", 95, melhor.get('bairro', ''), melhor.get('cidade', ''), f"{melhor['fonte']} (Strict-Mode)", ["Desambiguação Espacial Anti-Colisão acionada em Nuvem. Resolução estrita aplicada."])

def API_OSRM_Routing(lat_o, lon_o, lat_d, lon_d):
    start_t = time.time()
    try:
        url = f"http://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=false&steps=true"
        headers = {"User-Agent": "GerenciadorLogisticoCorp/2.0"}
        r = session.get(url, headers=headers, timeout=6).json()
        if r.get("code") == "Ok" and r.get("routes"):
            rota = r["routes"][0]
            distancia_km = round(rota["distance"] / 1000.0, 2)
            tempo_min = round(rota["duration"] / 60.0)
            
            usa_balsa = "Não"
            for leg in rota.get("legs", []):
                for step in leg.get("steps", []):
                    if step.get("mode") == "ferry" or step.get("maneuver", {}).get("type") == "ferry":
                        usa_balsa = "Sim"
                        break
            registrar_telemetria("OSRM", True, time.time() - start_t)
            return (distancia_km, tempo_min, usa_balsa)
    except Exception: pass
    registrar_telemetria("OSRM", False, time.time() - start_t)
    return None

# ==============================================================================
# 🧠 MOTOR DE CONSENSO PROBABILÍSTICO BAYESIANO E CLUSTERING DBSCAN ESFÉRICO
# ==============================================================================
def processar_consenso_dinamico(candidatos, tipo_entrada, texto_cru):
    candidatos_validos = []
    candidatos_para_avaliacao = candidatos.copy()
    
    texto_norm = semantica.normalizar(texto_cru)
    ctx_inf = semantica.resolver_contexto_administrativo(texto_norm)
    uf_inf, mun_inf, dist_inf = ctx_inf.get("uf", ""), ctx_inf.get("municipio", ""), ctx_inf.get("distrito", "")
    box = BOUNDING_BOXES_UF.get(uf_inf) if uf_inf else None
    
    for c in candidatos:
        valido, lat_c, lon_c = validar_coordenada_brasil(c["lat"], c["lon"])
        if valido:
            if box:
                if not (box["lat_min"] <= lat_c <= box["lat_max"] and box["lon_min"] <= lon_c <= box["lon_max"]):
                    continue
            c["lat"], c["lon"] = lat_c, lon_c 
            candidatos_validos.append(c)
            
    if not candidatos_validos: return None
    
    if uf_inf:
        candidatos_rigorosos = []
        nome_estado_inf = unidecode(IBGE_ESTADOS.get(uf_inf, uf_inf)).upper()
        for c in candidatos_validos:
            est_api = unidecode(c.get('estado', '')).upper().strip()
            if est_api:
                if uf_inf in est_api or nome_estado_inf in est_api:
                    candidatos_rigorosos.append(c)
            else:
                candidatos_rigorosos.append(c)
        candidatos_validos = candidatos_rigorosos

    if not candidatos_validos: return None

    validados_semantica = []
    for c in candidatos_validos:
        cidade_api = unidecode(c.get('cidade', '')).upper().strip()
        estado_api = unidecode(c.get('estado', '')).upper().strip()
        if cidade_api and estado_api:
            pertence_municipio = cidade_api in IBGE_MUNICIPIOS and any(item["uf"] == estado_api for item in IBGE_MUNICIPIOS[cidade_api])
            pertence_distrito = cidade_api in IBGE_DISTRITOS and any(item["uf"] == estado_api for item in IBGE_DISTRITOS[cidade_api])
            
            if pertence_municipio or pertence_distrito: validados_semantica.append(c)
            elif cidade_api not in IBGE_MUNICIPIOS and cidade_api not in IBGE_DISTRITOS: validados_semantica.append(c)
        elif cidade_api:
            if cidade_api in IBGE_MUNICIPIOS or cidade_api in IBGE_DISTRITOS: validados_semantica.append(c)
        else: validados_semantica.append(c)
    candidatos_validos = validados_semantica
    if not candidatos_validos: return None

    if tipo_entrada in ["ENDERECO_COMPLETO", "POI", "CEP", "CONDOMINIO"]: raio_cluster_km = 0.5
    elif tipo_entrada in ["BAIRRO", "RURAL"]: raio_cluster_km = 2.0
    else: raio_cluster_km = 10.0
        
    coords_matriz = np.array([[c["lat"], c["lon"]] for c in candidatos_validos])
    if len(coords_matriz) >= 2:
        coords_rad = np.radians(coords_matriz)
        eps_angular = raio_cluster_km / 6371.0
        db_model = DBSCAN(eps=eps_angular, min_samples=2, metric='haversine').fit(coords_rad)
        labels = db_model.labels_
        valid_labels = [l for l in labels if l != -1]
        if valid_labels:
            contagem_clusters = collections.Counter(valid_labels).most_common(2)
            maior_cluster_label = contagem_clusters[0][0]
            candidatos_validos = [candidatos_validos[idx] for idx, label in enumerate(labels) if label == maior_cluster_label]
            
    if not candidatos_validos: return None

    tolerancia_km = raio_cluster_km
    input_usuario = ParserGeograficoBR.extrair_componentes(texto_norm)

    candidatos_consistentes_mun = [c for c in candidatos_validos if validar_consistencia_municipal(c, mun_inf)]
    if candidatos_consistentes_mun: candidatos_validos = candidatos_consistentes_mun
        
    PESO_FONTES = {}
    DEFAULT_WEIGHTS = {"ARCGIS": 0.95, "TOMTOM": 0.90, "OVERPASS": 0.85, "NOMINATIM": 0.80, "PHOTON": 0.75}
    for fonte, d_w in DEFAULT_WEIGHTS.items():
        m_api = cache_api_health.get(fonte, {"hits": 0, "calls": 0})
        PESO_FONTES[fonte] = round(max(0.5, m_api["hits"] / m_api["calls"]), 2) if m_api["calls"] >= 50 else d_w

    BAYES_MULTIPLIERS = {
        "CEP": {"mun": 1.5, "uf": 1.2, "cep": 4.0, "bairro": 1.0, "numero": 1.0, "rua_peso": 0.2},
        "ENDERECO_COMPLETO": {"mun": 1.8, "uf": 1.3, "cep": 1.5, "bairro": 1.2, "numero": 2.5, "rua_peso": 1.5},
        "CONDOMINIO": {"mun": 1.8, "uf": 1.3, "cep": 1.2, "bairro": 1.5, "numero": 1.0, "rua_peso": 1.8},
        "DEFAULT": {"mun": 1.5, "uf": 1.2, "cep": 1.2, "bairro": 1.2, "numero": 1.2, "rua_peso": 0.8}
    }
    bm = BAYES_MULTIPLIERS.get(tipo_entrada, BAYES_MULTIPLIERS["DEFAULT"])

    for c1 in candidatos_validos:
        p_prior = min(c1["score_base"] / 100.0, 0.50)
        
        feat_mun = mun_inf and c1.get("cidade") and (mun_inf in c1["cidade"] or fuzz.token_set_ratio(mun_inf, c1["cidade"]) >= 95)
        feat_uf = uf_inf and c1.get("estado") and uf_inf in c1["estado"]
        feat_cep = input_usuario.get("cep") and c1.get("cep") and input_usuario["cep"] in c1["cep"].replace("-", "")
        feat_bairro = dist_inf and c1.get("bairro") and dist_inf in c1["bairro"]
        feat_numero = input_usuario.get("numero") and c1.get("numero") and input_usuario["numero"] in c1["numero"]
        fuzz_rua = fuzz.token_set_ratio(texto_norm, c1.get("logradouro", "")) / 100.0 if c1.get("logradouro") else 0.1
        
        PADROES_RODOVIA = [r'\bBR[- ]?\d+\b', r'\bSP[- ]?\d+\b', r'\bMG[- ]?\d+\b', r'\bGO[- ]?\d+\b', r'\bDF[- ]?\d+\b', r'\bRJ[- ]?\d+\b', r'\bPR[- ]?\d+\b', r'\bSC[- ]?\d+\b', r'\bRS[- ]?\d+\b']
        input_tem_rodovia = any(re.search(p, texto_norm) for p in PADROES_RODOVIA)
        api_tem_rodovia = any(re.search(p, c1.get("logradouro", "").upper()) for p in PADROES_RODOVIA) or bool(re.search(r'\b(RODOVIA|KM|ESTRADA)\b', c1.get("logradouro", "").upper()))
        feat_punicao_rodovia = not input_tem_rodovia and api_tem_rodovia
        
        api_end_str = f"{c1.get('logradouro','')} {c1.get('bairro','')} {c1.get('cidade','')} {c1.get('estado','')}".upper()
        l_conf_rural = 0.2 if (tipo_entrada == "RURAL" and any(urb in api_end_str for urb in ["QUADRA ", "SQN ", "SQS ", "APARTAMENTO ", "EDIFICIO ", "BLOCO "])) else 1.0
        l_conf_urbano = 0.4 if (tipo_entrada in ["ENDERECO_COMPLETO", "BAIRRO"] and any(rur in api_end_str for rur in ["CHACARA ", "FAZENDA ", "GLEBA "])) else 1.0

        probabilidades_cluster = [p_prior]
        apis_concordantes = set([c1["fonte"]])
        for c2 in candidatos_validos:
            if c1["fonte"] != c2["fonte"]:
                dist, _ = calcular_distancia_linha_reta(c1["lat"], c1["lon"], c2["lat"], c2["lon"], contexto="Consenso API")
                if dist <= tolerancia_km: 
                    apis_concordantes.add(c2["fonte"])
                    probabilidades_cluster.append(PESO_FONTES.get(c2["fonte"], 0.5))
        
        falha_combinada = 1.0
        for prob in probabilidades_cluster:
            falha_combinada *= (1.0 - prob)
        prob_ensemble = 1.0 - falha_combinada
        
        l_mun = bm["mun"] if feat_mun else 0.4
        l_uf = bm["uf"] if feat_uf else 0.7
        l_cep = bm["cep"] if feat_cep else 0.9
        l_bairro = bm["bairro"] if feat_bairro else 0.9
        l_numero = bm["numero"] if feat_numero else 0.8
        l_rua = 0.5 + (fuzz_rua * bm["rua_peso"])
        l_rodovia = 0.1 if feat_punicao_rodovia else 1.0
        
        odds = (prob_ensemble / (1 - prob_ensemble)) * l_mun * l_uf * l_cep * l_bairro * l_numero * l_rua * l_rodovia * l_conf_rural * l_conf_urbano
        probabilidade_final = odds / (1 + odds)
        
        c1["score_final"] = min(probabilidade_final * 100, 99.9)
        c1["xai_data"] = {"mun": bool(feat_mun), "uf": bool(feat_uf), "cep": bool(feat_cep), "num": bool(feat_numero), "fuzz": round(fuzz_rua * 100, 1), "apis": list(apis_concordantes)}
        
    candidatos_validos.sort(key=lambda x: x["score_final"], reverse=True)
    
    vencedor = None
    top3_candidatos = candidatos_validos[:3]
    for cand in top3_candidatos:
        m = executar_reverse_geocoding_multimotor(cand["lat"], cand["lon"])
        estado_comp = m.get("estado", cand.get("estado", "")).upper().strip()
        cidade_comp = m.get("cidade", cand.get("cidade", "")).upper().strip()
        
        if uf_inf and estado_comp:
            nome_estado_inf = unidecode(IBGE_ESTADOS.get(uf_inf, uf_inf)).upper()
            if uf_inf not in estado_comp and nome_estado_inf not in estado_comp: continue 
            
        if mun_inf and cidade_comp:
            match_cid = (mun_inf in cidade_comp) or (cidade_comp in mun_inf) or (fuzz.token_set_ratio(mun_inf, cidade_comp) >= 85)
            if not match_cid: continue
        
        bairro_comp = m.get("bairro", cand.get("bairro", "")).upper().strip()
        logr_comp = m.get("logradouro", cand.get("logradouro", "")).upper().strip()
        
        end_reverse = ", ".join([c for c in [logr_comp, bairro_comp, cidade_comp, estado_comp] if c.strip()])
        similaridade = fuzz.token_set_ratio(texto_norm, end_reverse.upper())
        
        if similaridade >= 30 or tipo_entrada in ["BAIRRO", "MUNICIPIO", "RURAL"] or len(texto_norm.split()) <= 4:
            vencedor = cand
            break
            
    if not vencedor: return None
    
    for cand in candidatos_para_avaliacao:
        if cand.get("lat", 0.0) == 0.0 or cand.get("lon", 0.0) == 0.0: continue
        f_n = cand.get("fonte", "")
        metr = cache_api_health.get(f_n, {"hits": 0, "calls": 0, "falhas": 0, "tempo_total": 0.0})
        dist_auditoria, _ = calcular_distancia_linha_reta(cand["lat"], cand["lon"], vencedor["lat"], vencedor["lon"], contexto="Auditoria Health")
        if dist_auditoria <= 0.05:
            metr["hits"] += 1
        cache_api_health.set(f_n, metr, expire=None)

    score_consenso = min(int(vencedor["score_final"]), 100)
    
    m = {"logradouro": vencedor.get("logradouro", ""), "bairro": vencedor["bairro"], "cidade": vencedor["cidade"], "municipio": vencedor["cidade"], "distrito": "", "estado": vencedor["estado"], "cep": vencedor.get("cep", "")}
    
    if tipo_entrada in ["MUNICIPIO", "BAIRRO", "ESTADO", "DISTRITO", "RURAL"]:
        m["logradouro"] = ""
        m["numero"] = ""
        m["cep"] = ""
        
    score_completude = 80
    if tipo_entrada == "CEP": score_completude = 100
    elif tipo_entrada == "ENDERECO_COMPLETO":
        tem_numero = bool(input_usuario.get("numero") or input_usuario.get("complemento"))
        tem_cidade = bool(mun_inf); tem_uf = bool(uf_inf)
        if tem_numero and tem_cidade and tem_uf: score_completude = 100
        elif tem_cidade and tem_uf: score_completude = 95
        elif tem_cidade: score_completude = 85
        else: score_completude = 75
    elif tipo_entrada == "POI": score_completude = 95
    elif tipo_entrada == "CONDOMINIO": score_completude = 95
    elif tipo_entrada == "RURAL": score_completude = 90
    elif tipo_entrada in ["BAIRRO", "MUNICIPIO", "DISTRITO"]: score_completude = 95

    score_limitado = min(score_consenso, score_completude)
    if m.get("cep") and score_limitado < 100: score_limitado = min(score_limitado + 10, 100 if tipo_entrada == "CEP" else 95)

    explicacoes_humanas = []
    explicacoes_humanas.append(f"Análise inicial baseada em {len(candidatos_validos)} candidato(s) da Nuvem.")
    
    xd = vencedor["xai_data"]
    if len(xd["apis"]) >= 2:
        explicacoes_humanas.append(f"Consenso espacial estabelecido via Ensemble Multi-API ({' + '.join(xd['apis'])}).")
    else:
        explicacoes_humanas.append(f"Inferência baseada unicamente na resposta isolada da fonte {vencedor['fonte']}.")
        
    if not ctx_inf.get("municipio"): explicacoes_humanas.append("Aviso: Validação IBGE local substituída por inteligência e preenchimento em Nuvem.")
    if xd["mun"]: explicacoes_humanas.append("Município validado na malha de referência oficial IBGE.")
    if xd["uf"]: explicacoes_humanas.append("Correspondência administrativa de Estado confirmada.")
    if xd["cep"]: explicacoes_humanas.append("Código Postal cruzado e confirmado por cascades.")
    if xd["num"]: explicacoes_humanas.append("Assinatura de número predial reconhecida na porta do cliente.")
    if xd["fuzz"] >= 80.0: explicacoes_humanas.append(f"Similaridade léxica de logradouro em {xd['fuzz']}% de aprovação.")

    match_logr = fuzz.token_set_ratio(texto_norm, m.get("logradouro", "").upper())
    match_bairro = fuzz.token_set_ratio(dist_inf, m.get("bairro", "").upper()) if dist_inf else 100
    match_cep = 100 if input_usuario.get("cep") and m.get("cep") and input_usuario["cep"] in m.get("cep", "").replace("-", "") else 0 if input_usuario.get("cep") else 100
    
    if tipo_entrada in ["MUNICIPIO", "BAIRRO", "RURAL"]:
        confianca = "ALTA"
        score_limitado = max(score_limitado, 85)
        explicacoes_humanas.append("Busca por localidade abrangente. Score reajustado para nível de cidade/bairro.")
    elif (match_logr * 0.5) + (match_bairro * 0.3) + (match_cep * 0.2) < 65.0:
        confianca = "REVISAO_MANUAL"
        explicacoes_humanas.append("⚠️ Alerta Anti-Fantasma: Integridade semântica de logradouro inadequada.")
        score_limitado = min(score_limitado, 49)
    else:
        confianca = "ALTISSIMA" if score_limitado >= 85 else "ALTA" if score_limitado >= 75 else "MEDIA" if score_limitado >= 60 else "BAIXA"

    rua_f = m["logradouro"] if m["logradouro"] else ""
    endereco_f = ", ".join([c for c in [rua_f, m["bairro"], m["cidade"], m["estado"]] if c.strip()]) + ", BRASIL"
    
    if vencedor["lat"] == 0.0 or vencedor["lon"] == 0.0:
        return None
        
    return vencedor["lat"], vencedor["lon"], endereco_f, confianca, score_limitado, m["distrito"], m["municipio"], vencedor["fonte"], explicacoes_humanas

# ==============================================================================
# 🎚️ ORQUESTRADOR EM CASCATA HIERÁRQUICA E OFFLINE-FIRST
# ==============================================================================
def _obter_coordenadas_e_endereco_oficial_core(localidade):
    texto_cru = str(localidade).strip()
    if not texto_cru or texto_cru.lower() == 'nan': return 0.0, 0.0, "", "BAIXA", 0, "", "", "N/A", ["String Vazia"]
    
    texto_norm = semantica.normalizar(texto_cru)
    
    if match_coords := re.match(r'^\s*(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*$', texto_cru):
        lat_in, lon_in = float(match_coords.group(1)), float(match_coords.group(2))
        valido, lat_in, lon_in = validar_coordenada_brasil(lat_in, lon_in)
        if valido:
            m = executar_reverse_geocoding_multimotor(lat_in, lon_in)
            end_f = ", ".join([c for c in [m.get("logradouro", ""), m.get("bairro", ""), m.get("cidade", ""), m.get("estado", "")] if c.strip()]) + ", BRASIL"
            return lat_in, lon_in, end_f, "ABSOLUTA", 100, m.get("bairro", ""), m.get("cidade", ""), "COORDENADA_EXATA", ["Entrada direta via Coordenadas Numéricas."]

    if texto_norm in cache_aprendizado:
        dado_salvo = cache_aprendizado[texto_norm]
        if isinstance(dado_salvo, dict) and "lat" in dado_salvo and "lon" in dado_salvo:
            return dado_salvo["lat"], dado_salvo["lon"], dado_salvo.get("endereco", texto_norm), "ALTISSIMA", 100, dado_salvo.get("distrito", ""), dado_salvo.get("municipio", ""), "APRENDIZADO_LOCAL", ["Ponto quente extraído do cache local enriquecido."]

    endereco_canonico, tipo_entrada, _, _, _ = semantica.construir_endereco_canonico(texto_norm)
    parsed_comp = ParserGeograficoBR.extrair_componentes(texto_norm)
    
    cache_key = hashlib.md5(f"GEO_V54_{tipo_entrada}_{endereco_canonico}".encode('utf-8')).hexdigest()
    
    if cache_key in cache_geo:
        c = cache_geo[cache_key]
        if c.get("lat", 0.0) != 0.0 and c.get("lon", 0.0) != 0.0:
            return c["lat"], c["lon"], c["endereco"], c["confianca"], c["score_num"], c["distrito"], c["municipio"], c["fonte"], ["Cache L2 Hit."]

    ctx = semantica.resolver_contexto_administrativo(texto_norm)

    if ctx.get("municipio") and ctx.get("uf"):
        mun_nome = ctx["municipio"]
        uf_nome = ctx["uf"]
            
        if tipo_entrada == "MUNICIPIO":
            if mun_nome in IBGE_MUNICIPIOS:
                for item in IBGE_MUNICIPIOS[mun_nome]:
                    if item["uf"] == uf_nome and item.get("lat", 0.0) != 0.0:
                        endereco_ibge = f"{mun_nome}, {IBGE_ESTADOS.get(uf_nome, uf_nome)}, BRASIL"
                        res_final = (item["lat"], item["lon"], endereco_ibge, "MUNICIPAL", 100, ctx.get("distrito", ""), mun_nome, "BASE_IBGE_OFFLINE", ["Otimização Direta IBGE: Busca por cidade detectada. Coordenda exata do Centróide Brasileiro extraída sem rede."])
                        cache_geo.set(cache_key, {"lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], "confianca": res_final[3], "score_num": res_final[4], "distrito": res_final[5], "municipio": res_final[6], "fonte": res_final[7]}, expire=2592000)
                        return res_final

    rua_suja = parsed_comp["resto"]
    for loc in [ctx.get("municipio", ""), ctx.get("distrito", ""), ctx.get("uf", ""), "BRASIL", "DF"]:
        if loc: rua_suja = re.sub(rf'\b{loc}\b', '', rua_suja).strip(" ,-")
        
    rua_limpa = re.sub(r'\s+', ' ', rua_suja).strip()
    if parsed_comp["numero"]: rua_limpa = f"{rua_limpa} {parsed_comp['numero']}".strip()
    
    contexto_estruturado = {
        "logradouro": rua_limpa if rua_limpa else texto_norm,
        "bairro": ctx.get("distrito", ""),
        "municipio": ctx.get("municipio", ""),
        "uf": ctx.get("uf", ""),
        "cep": parsed_comp.get("cep", "")
    }

    if match_offline := obedience_base_local(contexto_estruturado):
        return match_offline["lat"], match_offline["lon"], match_offline["endereco"], "ALTISSIMA", 100, match_offline.get("distrito", ""), match_offline.get("municipio", ""), "BASE_NACIONAL_OFFLINE", ["Ponto resolvido via CNEFE/Bases Locais Estáticas."]

    candidatos_validos = []

    if tipo_entrada == "CEP":
        cep_estrito = re.search(r'\b\d{5}-?\d{3}\b', texto_norm)
        if cep_estrito:
            cep_limpo = cep_estrito.group(0).replace("-", "")
            logr, bair, loca, uf, lat_c, lon_c = cascata_postal_tripla(cep_limpo)
            if loca:
                nome_est_cep = IBGE_ESTADOS.get(uf, uf) if uf else ""
                addr_c = f"{logr}, {bair}, {loca}, {nome_est_cep}, CEP {cep_estrito.group(0)}, BRASIL"
                addr_c = re.sub(r',\s*,', ',', addr_c).strip(' ,')
                
                val_c, lat_corrigida_c, lon_corrigida_c = validar_coordenada_brasil(lat_c, lon_c)
                if lat_c != 0.0 and lon_c != 0.0 and val_c:
                    res_final = (lat_corrigida_c, lon_corrigida_c, addr_c, "ALTISSIMA", 100, bair, loca, "BrasilAPI/OSM Postal", ["Cascata Postal Direta."])
                    cache_geo.set(cache_key, {"lat": lat_corrigida_c, "lon": lon_corrigida_c, "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca, "fonte": "BrasilAPI/OSM Postal"}, expire=2592000)
                    return res_final
                
                res_arc = API_ArcGIS(addr_c)
                if res_arc:
                    if isinstance(res_arc, list): res_arc = res_arc[0]
                    val_arc, lat_corrigida_arc, lon_corrigida_arc = validar_coordenada_brasil(res_arc["lat"], res_arc["lon"])
                    if val_arc:
                        res_final = (lat_corrigida_arc, lon_corrigida_arc, addr_c, "ALTISSIMA", 100, bair, loca, "ViaCEP/ArcGIS", ["Cascata Postal Complementada por ArcGIS."])
                        cache_geo.set(cache_key, {"lat": lat_corrigida_arc, "lon": lon_corrigida_arc, "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca, "fonte": "ViaCEP/ArcGIS"}, expire=2592000)
                        return res_final

    def disparar_apis_paralelas(tarefas):
        resultados = []
        for f in as_completed([EXECUTOR_APIS.submit(func, *args, **kwargs) for func, args, kwargs in tarefas]):
            if res := f.result(): resultados.extend(res)
        return resultados

    if tipo_entrada == "POI" or tipo_entrada == "CONDOMINIO":
        candidatos_validos.extend(disparar_apis_paralelas([(API_TomTom, (endereco_canonico,), {})]))
    elif tipo_entrada in ["ENDERECO_COMPLETO", "LOGRADOURO"]:
        candidatos_validos.extend(disparar_apis_paralelas([(API_ArcGIS, (endereco_canonico,), {"ctx": contexto_estruturado}), (API_TomTom, (endereco_canonico,), {})]))
    elif tipo_entrada in ["BAIRRO", "MUNICIPIO", "DISTRITO"]:
        candidatos_validos.extend(disparar_apis_paralelas([(API_Photon, (endereco_canonico,), {})]))
    else:
        candidatos_validos.extend(disparar_apis_paralelas([(API_Photon, (endereco_canonico,), {}), (API_ArcGIS, (endereco_canonico,), {"ctx": contexto_estruturado}), (API_TomTom, (endereco_canonico,), {})]))
            
    res_final = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru)
    
    if not res_final:
        res_nom = API_Nominatim(endereco_canonico, ctx=contexto_estruturado)
        if not res_nom: res_nom = API_Photon(endereco_canonico)
        if res_nom:
            candidatos_validos.extend(res_nom)
            res_final = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru)

    if not res_final and ctx.get("municipio") and ctx.get("uf"):
        mun_nome = ctx["municipio"]
        uf_nome = ctx["uf"]
        
        if mun_nome in IBGE_MUNICIPIOS:
            for item in IBGE_MUNICIPIOS[mun_nome]:
                if item["uf"] == uf_nome and item.get("lat", 0.0) != 0.0:
                    endereco_ibge = f"{mun_nome}, {IBGE_ESTADOS.get(uf_nome, uf_nome)}, BRASIL"
                    res_final = (item["lat"], item["lon"], endereco_ibge, "MUNICIPAL", 90, ctx.get("distrito", ""), mun_nome, "BASE_IBGE_OFFLINE", ["Blindagem Ativa IBGE: APIs falharam, coordenada estrita recuperada da base local offline para a UF."])
                    break
                    
        if not res_final:
            lat_c, lon_c, fonte_c = obter_coordenada_centroide_supremo(mun_nome, uf_nome)
            if lat_c != 0.0 and lon_c != 0.0:
                val_rev = executar_reverse_geocoding_multimotor(lat_c, lon_c)
                est_rev = unidecode(val_rev.get("estado", "")).upper()
                nome_estado_inf = unidecode(IBGE_ESTADOS.get(uf_nome, uf_nome)).upper()
                if uf_nome in est_rev or nome_estado_inf in est_rev:
                    endereco_ibge = f"{mun_nome}, {IBGE_ESTADOS.get(uf_nome, uf_nome)}, BRASIL"
                    res_final = (lat_c, lon_c, endereco_ibge, "MUNICIPAL", 85, ctx.get("distrito", ""), mun_nome, fonte_c, [f"Resgatado via Centróide Supremo ({fonte_c}) e Estado Confirmado."])

    if res_final:
        cache_geo.set(cache_key, {"lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], "confianca": res_final[3], "score_num": res_final[4], "distrito": res_final[5], "municipio": res_final[6], "fonte": res_final[7]}, expire=2592000)
        return res_final
        
    return 0.0, 0.0, endereco_canonico, "BAIXA", 0, "", "", "N/A", ["Falha Geográfica Absoluta por falta de candidatos e centróides na nuvem."]

def obter_coordenadas_e_endereco_oficial(localidade):
    if str(localidade).strip() == "FALHA_GEO_DESTINO" or str(localidade).strip() == "NENHUM_HUB_VALIDO" or str(localidade).strip() == "FALHA_GEO_ORIGEM":
        return 0.0, 0.0, "Falha de Geocodificação ou Alocação", "BAIXA", 0, "", "", "N/A", ["Ponto geográfico inválido retornado na pré-geocodificação de Hubs."]
        
    lat, lon, end_f, conf, score, dist, mun, fonte, xai = _obter_coordenadas_e_endereco_oficial_core(localidade)
    
    if lat != 0.0 and lon != 0.0:
        if not end_f or not mun or not dist or end_f.strip() == "" or mun.strip() == "" or dist.strip() == "":
            rev = executar_reverse_geocoding_multimotor(lat, lon)
            
            if not end_f or end_f.strip() == "":
                end_f = ", ".join([c for c in [rev.get("logradouro", ""), rev.get("bairro", ""), rev.get("cidade", ""), rev.get("estado", "")] if c.strip()]) + ", BRASIL"
            if not mun or mun.strip() == "":
                mun = rev.get("cidade", "")
            if not dist or dist.strip() == "":
                dist = rev.get("bairro", "")

    if not end_f or end_f.strip() == "": end_f = f"Localidade não mapeável: {localidade}"
    if not mun or mun.strip() == "": mun = "Município Não Mapeado"
    if not dist or dist.strip() == "": dist = "Distrito Não Mapeado"
    if not conf or conf.strip() == "": conf = "BAIXA"
    if score is None: score = 0
    if not fonte or fonte.strip() == "": fonte = "Dedução Heurística"
    if not xai: xai = ["Auditoria preenchida via Fallback Estrutural do Motor."]

    return lat, lon, end_f, conf, score, dist, mun, fonte, xai

# ==============================================================================
# 🚀 MOTOR DE ROTEAMENTO EXTREMO E PIPELINE UNIFICADO
# ==============================================================================
def extrair_dados_reais_google(origem_texto, destino_texto, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=True):
    cache_key = f"GOOG_V54_{origem_texto}|{destino_texto}|{usar_coordenadas}"
    if cache_key in cache_google: return cache_google[cache_key]

    orig_link_txt = requests.utils.quote(origem_texto)
    dest_link_txt = requests.utils.quote(destino_texto)

    origem_param_scraper = f"{lat_o},{lon_o}" if usar_coordenadas else orig_link_txt
    destino_param_scraper = f"{lat_d},{lon_d}" if usar_coordenadas else dest_link_txt
    url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param_scraper}!1m2!1m1!1s{destino_param_scraper}!3e0"
    
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={orig_link_txt}&destination={dest_link_txt}&travelmode=driving"
    link_embed = f"https://maps.google.com/maps?saddr={orig_link_txt}&daddr={dest_link_txt}&output=embed"
    
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    try:
        resposta = session.get(url_api, headers=headers, timeout=12)
        
        texto_resposta = resposta.text.replace('\u202f', ' ').replace('\u200b', '')
        if len(texto_resposta) < 500: return None
        
        dist_matches = re.findall(r'\"([\d\.,]+)\s*km\"', texto_resposta)
        if not dist_matches: dist_matches = re.findall(r'([\d\.,]+)\s*km', texto_resposta)
        if not dist_matches: dist_matches = re.findall(r'\\x22([\d\.,]+)\s*km\\x22', texto_resposta)
        if not dist_matches: dist_matches = re.findall(r'(\d+)\s*km', texto_resposta)
        
        time_matches = re.findall(r'\"(\d+\s*h\s*\d+\s*min|\d+\s*h|\d+\s*min)\"', texto_resposta)
        if not time_matches: time_matches = re.findall(r'(\d+\s*h\s*\d+\s*min|\d+\s*h|\d+\s*min)', texto_resposta)
        if not time_matches: time_matches = re.findall(r'\\x22(\d+\s*h\s*\d+\s*min|\d+\s*h|\d+\s*min)\\x22', texto_resposta)
        
        if dist_matches and time_matches:
            km_str = dist_matches[0]
            if km_str.count('.') == 1 and ',' not in km_str:
                if len(km_str.split('.')[1]) == 3: km_str = km_str.replace('.', '')
                else: km_str = km_str.replace('.', '.')
            elif ',' in km_str and '.' in km_str:
                km_str = km_str.replace('.', '').replace(',', '.')
            elif ',' in km_str:
                km_str = km_str.replace(',', '.')
            else:
                km_str = km_str.replace('.', '')
                
            try:
                km_puro = float(km_str)
            except ValueError:
                km_puro = 0.0
            
            balsa_patterns = [r'esta rota inclui uma balsa', r'pegar a balsa', r'ferry route', r'travessia de balsa']
            envolve_balsa = "Sim" if any(re.search(p, texto_resposta.lower()) for p in balsa_patterns) else "Não"
            
            if dist_linha_reta > 0 and km_puro > (dist_linha_reta * 2.5):
                envolve_balsa = "Não"

            score_google = 80 + (10 if km_puro > 0 else 0) + (10 if time_matches[0] else 0)
            score_google = min(score_google, 100)
            res = (km_puro, time_matches[0], link_maps, envolve_balsa, score_google, link_embed)
            cache_google.set(cache_key, res, expire=2592000); return res
    except Exception: pass
    return None

def obter_fator_desvio_rodoviario(linha_reta):
    return 1.45 if linha_reta < 5.0 else 1.35 if linha_reta < 20.0 else 1.25 if linha_reta < 100.0 else 1.18

def calcular_pipeline_logistico(origem, destino, perfil_rota="shortest"):
    start_total = time.time()
    origem_clean, destino_clean = str(origem).strip(), str(destino).strip()
    
    chave_rota_cache = f"ROTA_V54_{semantica.normalizar(origem_clean)}->{semantica.normalizar(destino_clean)}"
    
    if chave_rota_cache in cache_rotas: 
        ret_cache = cache_rotas[chave_rota_cache]
        if len(ret_cache) >= 30:
            dist_cache = ret_cache[4]
            lat_o_cache, lon_o_cache = ret_cache[19], ret_cache[20]
            lat_d_cache, lon_d_cache = ret_cache[21], ret_cache[22]
            
            if dist_cache == 0.0 and lat_o_cache != 0.0 and lat_d_cache != 0.0 and (lat_o_cache != lat_d_cache or lon_o_cache != lon_d_cache):
                global METRICAS_DISTANCIA
                METRICAS_DISTANCIA["cache_unpoisoned"] += 1
                logger.warning(f"♻️ Cache Poisoning Interceptado em: {origem_clean} -> {destino_clean}. Recalculando Linha Reta.")
                nova_dist, novo_status = calcular_distancia_linha_reta(lat_o_cache, lon_o_cache, lat_d_cache, lon_d_cache, contexto="Unpoisoning de Cache")
                retorno_mutavel = list(ret_cache)
                retorno_mutavel[4] = nova_dist
                if len(retorno_mutavel) == 30:
                    retorno_mutavel.append(novo_status)
                else:
                    retorno_mutavel[30] = novo_status
                retorno_novo = tuple(retorno_mutavel)
                cache_rotas.set(chave_rota_cache, retorno_novo, expire=2592000)
                return retorno_novo
                
            if len(ret_cache) == 30:
                return (*ret_cache, "Calculada via Cache Hit Estável")
            return ret_cache
    
    start_geo = time.time()
    lat_o, lon_o, end_oficial_o, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, xai_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    lat_d, lon_d, end_oficial_d, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, xai_d = obter_coordenadas_e_endereco_oficial(destino_clean)
    
    # BARREIRA TOPOLÓGICA DE COLISÃO E DESAMBIGUAÇÃO ESTRITA
    if lat_o == lat_d and lon_o == lon_d and lat_o != 0.0:
        if semantica.normalizar(origem_clean) != semantica.normalizar(destino_clean):
            METRICAS_DISTANCIA["desambiguacoes_estritas"] += 1
            logger.warning(f"Colisão de Centróide Detectada: '{origem_clean}' e '{destino_clean}' reduzidos ao mesmo ponto. Forçando regeocodificação hierárquica.")
            
            res_o = forcar_geocodificacao_hierarquica_estrita(origem_clean)
            if res_o: lat_o, lon_o, end_oficial_o, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, xai_o = res_o
            
            res_d = forcar_geocodificacao_hierarquica_estrita(destino_clean)
            if res_d: lat_d, lon_d, end_oficial_d, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, xai_d = res_d
            
    tempo_geocoding = round(time.time() - start_geo, 2)
    start_rot = time.time()

    if all([lat_o is not None, lon_o is not None, lat_d is not None, lon_d is not None]) and lat_o != 0.0 and lat_d != 0.0:
        dist_linha_reta, status_linha_reta = calcular_distancia_linha_reta(lat_o, lon_o, lat_d, lon_d, contexto=f"Pipeline Principal: {origem_clean} a {destino_clean}")
    else:
        dist_linha_reta = 0.0
        status_linha_reta = "Falha de Geocodificação (Coordenadas Nulas)"

    orig_param_fb = requests.utils.quote(end_oficial_o) if end_oficial_o else f"{lat_o},{lon_o}"
    dest_param_fb = requests.utils.quote(end_oficial_d) if end_oficial_d else f"{lat_d},{lon_d}"
    
    link_fallback = f"https://www.google.com/maps/dir/?api=1&origin={orig_param_fb}&destination={dest_param_fb}&travelmode=driving"
    link_embed_fallback = f"https://maps.google.com/maps?saddr={orig_param_fb}&daddr={dest_param_fb}&output=embed"

    res_google = None
    res_osrm = None
    
    res_google = extrair_dados_reais_google(end_oficial_o, end_oficial_d, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=True)
    
    if not res_google:
        res_google = extrair_dados_reais_google(origem_clean, destino_clean, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=False)

    if lat_o != 0.0 and lat_d != 0.0:
        res_osrm = API_OSRM_Routing(lat_o, lon_o, lat_d, lon_d)

    if res_google or res_osrm:
        if res_google and res_osrm:
            km_g = res_google[0]
            km_o = res_osrm[0]
            if km_o > km_g * 1.5:
                balsa_rota = res_google[3]
                motivo_roteamento = f"Identidade Logística Suprema: Rota ({km_g}km) extraída com sucesso absoluto diretamente da nuvem oficial do Google Maps."
            else:
                balsa_rota = res_google[3] if res_google[3] == "Sim" else res_osrm[2]
                motivo_roteamento = f"Identidade Logística Suprema: Rota ({km_g}km) extraída com sucesso absoluto diretamente da nuvem oficial do Google Maps."
            
            km_rota, tempo_rota, link_rota, score_rota, link_embed = res_google[0], res_google[1], res_google[2], res_google[4], res_google[5]
            fonte_rota = "Google Maps"
            
        elif res_google:
            km_rota, tempo_rota, link_rota, balsa_rota, score_rota, link_embed = res_google[0], res_google[1], res_google[2], res_google[3], res_google[4], res_google[5]
            fonte_rota = "Google Maps"
            motivo_roteamento = f"Identidade Logística Suprema: Rota ({km_rota}km) extraída com sucesso absoluto diretamente da nuvem oficial do Google Maps."
            
        else:
            km_rota = res_osrm[0]
            tempo_m = res_osrm[1]
            tempo_rota = f"{tempo_m} min" if tempo_m < 60 else f"{tempo_m // 60} h {tempo_m % 60} min"
            link_rota = link_fallback
            link_embed = link_embed_fallback
            balsa_rota = res_osrm[2]
            fonte_rota = "OSRM Routing"
            score_rota = 85
            motivo_roteamento = f"Fallback Operacional: Google Maps indisponível (Timeout). Traçado exato ({km_rota}km) calculado matematicamente pela malha OSRM."
            
        tempo_roteamento = round(time.time() - start_rot, 2); tempo_total = round(time.time() - start_total, 2)
        retorno = (km_rota, tempo_rota, link_rota, balsa_rota, dist_linha_reta, fonte_rota, score_rota, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total, xai_o, xai_d, motivo_roteamento, link_embed, status_linha_reta)
        cache_rotas.set(chave_rota_cache, retorno, expire=2592000)
        return retorno

    km_terrestre = round(dist_linha_reta * obter_fator_desvio_rodoviario(dist_linha_reta), 2)
    v_comercial = 45.0 if km_terrestre < 50.0 else 65.0
    minutos_est = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0 else 0
    tempo_geo_str = f"{minutos_est} min" if minutos_est < 60 else f"{minutos_est // 60} h {minutos_est % 60} min"
    tempo_roteamento = round(time.time() - start_rot, 2); tempo_total = round(time.time() - start_total, 2)
    motivo_fallback = "Alerta Crítico: Motores viários em Nuvem e Open-Source rejeitaram a rota (Timeout ou Coordenadas Inválidas). Projeção Geodésica Adaptativa acionada baseada na Linha Reta."
    
    retorno = (km_terrestre, tempo_geo_str, link_fallback, "Não", dist_linha_reta, "Geodésico Adaptativo", 50, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total, xai_o, xai_d, motivo_fallback, link_embed_fallback, status_linha_reta)
    cache_rotas.set(chave_rota_cache, retorno, expire=2592000)
    return retorno

def executar_pipeline_unificado(origem_cru, destino_cru, runner_up_info=None):
    orig = str(origem_cru).strip() if pd.notna(origem_cru) else ""
    dest = str(destino_cru).strip() if pd.notna(destino_cru) else ""
    
    concorrente = "N/A"
    dist_conc = 0.0
    link_conc = "N/A"
    justificativa = "N/A"
    
    if orig == "FALHA_GEO_ORIGEM" or dest == "NENHUM_HUB_VALIDO":
        return (0.0, "0 min", "Link Indisponível", "Não", 0.0, "Input Inválido", 0, "BAIXA", 0, "Não Informado", "Não Informado", "N/A", orig, "BAIXA", 0, "Não Informado", "Não Informado", "N/A", dest, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ["Falha Espacial Origem"], ["Falha Espacial Destino"], "Falha de Roteamento: Hub Base ou Endereço Destino foi incapaz de resolver latitude/longitude em nuvem.", "N/A", "Falha Operacional (Input Inválido)", concorrente, dist_conc, link_conc, justificativa)
        
    if orig.lower() in ['nan', 'none', 'null', ''] or dest.lower() in ['nan', 'none', 'null', '']:
        return (0.0, "0 min", "Link Indisponível", "Não", 0.0, "Input Inválido", 0, "BAIXA", 0, "Não Informado", "Não Informado", "N/A", orig, "BAIXA", 0, "Não Informado", "Não Informado", "N/A", dest, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, [], [], "Falha na leitura da célula (Campo Vazio).", "N/A", "Falha Operacional (Célula Vazia)", concorrente, dist_conc, link_conc, justificativa)
    
    res = calcular_pipeline_logistico(orig, dest, perfil_rota="shortest")
    
    if runner_up_info and res and len(res) >= 31:
        dist_v_runner, r_nome, r_lat, r_lon = runner_up_info
        lat_o, lon_o = res[19], res[20]
        
        if lat_o != 0.0 and r_lat != 0.0:
            dist_v_real, _ = calcular_distancia_linha_reta(lat_o, lon_o, r_lat, r_lon, contexto="Runner-Up Validation")
            res_g_runner = extrair_dados_reais_google(origem_cru, r_nome, lat_o, lon_o, r_lat, r_lon, dist_v_real, usar_coordenadas=True)
            if not res_g_runner:
                 res_g_runner = extrair_dados_reais_google(origem_cru, r_nome, lat_o, lon_o, r_lat, r_lon, dist_v_real, usar_coordenadas=False)
            
            if res_g_runner:
                dist_conc = res_g_runner[0]
                link_conc = res_g_runner[2]
            else:
                dist_conc = round(dist_v_real * obter_fator_desvio_rodoviario(dist_v_real), 2)
                o_param = requests.utils.quote(origem_cru)
                d_param = requests.utils.quote(r_nome)
                link_conc = f"https://www.google.com/maps/dir/?api=1&origin={o_param}&destination={d_param}&travelmode=driving"
        
        concorrente = r_nome
        if dist_conc > 0.0:
            justificativa = f"Alocação definida por proximidade matemática em linha reta. O trajeto viário oficial do Google Maps resultou em {res[0]} km. O 2º município mais próximo em linha reta era '{r_nome}', que geraria um traçado viário de {dist_conc} km."
        else:
            justificativa = f"Alocação matemática por vizinho mais próximo. Rota viária oficial via Google Maps: {res[0]} km."
        
    return (*res, concorrente, dist_conc, link_conc, justificativa)

def embrulhar_task_paralela(item):
    if len(item) == 4:
        par_id, orig, dest, r_info = item
    else:
        par_id, orig, dest = item
        r_info = None
        
    try: 
        res = executar_pipeline_unificado(orig, dest, r_info)
        if res and isinstance(res, tuple) and len(res) < 35:
            res = tuple(list(res) + ["N/A"] * (35 - len(res)))
        return par_id, res
    except Exception as e: 
        msg_erro = f"FALHA INTERNA: {str(e)}"
        fallback = (0.0, "0 min", "Link Indisponível", "Não", 0.0, msg_erro, 0, "BAIXA", 0, "Erro", "Erro", "N/A", str(orig), "BAIXA", 0, "Erro", "Erro", "N/A", str(dest), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, [msg_erro], [msg_erro], msg_erro, "N/A", "Falha de Processamento Multithread", "N/A", 0.0, "N/A", "N/A")
        return par_id, fallback

def rodar_pipeline_lote(df, pares_unicos, tarefas_priorizadas, nome_operador, progress_bar, status_container, runner_up_map=None):
    resultados_unicos = {}
    executor_lote = EXECUTOR_GLOBAL
    
    if runner_up_map:
        tarefas_unicas = [(t[1], t[1][0], t[1][1], runner_up_map.get(t[1][0])) for t in tarefas_priorizadas]
    else:
        tarefas_unicas = [(t[1], t[1][0], t[1][1]) for t in tarefas_priorizadas]
        
    futuros = {executor_lote.submit(embrulhar_task_paralela, t): t for t in tarefas_unicas}
    
    concluidos = 0
    st.session_state['logs_auditoria'] = []
    
    for f in as_completed(futuros):
        par_id, res = f.result()
        resultados_unicos[par_id] = res
        concluidos += 1
        status_container.text(f"🚀 Fila de Prioridade Assíncrona: {concluidos} / {len(pares_unicos)}")
        progress_bar.progress(concluidos / len(pares_unicos))
        
    status_container.text("✨ Distribuindo resultados e consolidando auditoria...")
    
    for idx, linha in df.iterrows():
        origem = str(linha.get('Origem', '')).strip() if pd.notna(linha.get('Origem', '')) else ""
        destino = str(linha.get('Destino', '')).strip() if pd.notna(linha.get('Destino', '')) else ""
        
        if origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan':
            res = resultados_unicos.get((origem, destino))
            if res:
                try:
                    df.at[idx, 'Distancia'] = float(res[0]) if res[0] is not None else 0.0
                    df.at[idx, 'Linha Reta'] = float(res[4]) if res[4] is not None else 0.0
                    df.at[idx, 'Score da Rota'] = float(res[6]) if res[6] is not None else 0.0
                    df.at[idx, 'Score Num Origem'] = float(res[8]) if res[8] is not None else 0.0
                    df.at[idx, 'Score Num Destino'] = float(res[14]) if res[14] is not None else 0.0
                    df.at[idx, 'Lat Origem'] = float(res[19]) if res[19] is not None else 0.0
                    df.at[idx, 'Lon Origem'] = float(res[20]) if res[20] is not None else 0.0
                    df.at[idx, 'Lat Destino'] = float(res[21]) if res[21] is not None else 0.0
                    df.at[idx, 'Lon Destino'] = float(res[22]) if res[22] is not None else 0.0
                    df.at[idx, 'Tempo Geocoding (s)'] = float(res[23]) if res[23] is not None else 0.0
                    df.at[idx, 'Tempo Roteamento (s)'] = float(res[24]) if res[24] is not None else 0.0
                    df.at[idx, 'Tempo Total (s)'] = float(res[25]) if res[25] is not None else 0.0
                    
                    if runner_up_map:
                        df.at[idx, 'Distancia Concorrente'] = float(res[32]) if res[32] != "N/A" else 0.0
                except (ValueError, TypeError): pass

                df.at[idx, 'Tempo'] = res[1] if res[1] is not None else "0 min"
                df.at[idx, 'Link da Rota'] = res[2] if res[2] is not None else "Link Indisponível"
                df.at[idx, 'Balsas'] = res[3] if res[3] is not None else "Não Informado"
                df.at[idx, 'Fonte da Rota'] = res[5] if res[5] is not None else "Desconhecida"
                df.at[idx, 'Confianca Origem'] = res[7] if res[7] is not None else "BAIXA"
                df.at[idx, 'Distrito Origem'] = res[9] if res[9] is not None else "Não Identificado"
                df.at[idx, 'Municipio Origem'] = res[10] if res[10] is not None else "Não Identificado"
                df.at[idx, 'Fonte Geocoding Origem'] = res[11] if res[11] is not None else "Desconhecida"
                df.at[idx, 'Endereco Oficial Origem'] = res[12] if res[12] is not None else "Endereço Não Identificado"
                df.at[idx, 'Confianca Destino'] = res[13] if res[13] is not None else "BAIXA"
                df.at[idx, 'Distrito Destino'] = res[15] if res[15] is not None else "Não Identificado"
                df.at[idx, 'Municipio Destino'] = res[16] if res[16] is not None else "Não Identificado"
                df.at[idx, 'Fonte Geocoding Destino'] = res[17] if res[17] is not None else "Desconhecida"
                df.at[idx, 'Endereco Oficial Destino'] = res[18] if res[18] is not None else "Endereço Não Identificado"
                df.at[idx, 'Motivo Roteamento'] = res[28] if len(res) > 28 and res[28] is not None else "Sem Justificativa"
                
                df.at[idx, 'Status Linha Reta'] = res[30] if len(res) > 30 and res[30] is not None else "Não Mapeado"
                
                if runner_up_map:
                    df.at[idx, 'Concorrente Analisado'] = res[31] if len(res) > 31 and res[31] is not None else "N/A"
                    df.at[idx, 'Link Rota Concorrente'] = res[33] if len(res) > 33 and res[33] is not None else "N/A"
                    df.at[idx, 'Justificativa de Alocacao'] = res[34] if len(res) > 34 and res[34] is not None else "N/A"
                
                try:
                    if float(res[19]) == 0.0 and float(res[21]) == 0.0:
                        df.at[idx, 'Score Final Global'] = 0.0
                        df.at[idx, 'Status da Rota'] = "Erro"
                    else:
                        score_o, score_d, score_r = float(df.at[idx, 'Score Num Origem']), float(df.at[idx, 'Score Num Destino']), float(df.at[idx, 'Score da Rota'])
                        score_global = round((0.35 * score_o) + (0.35 * score_d) + (0.30 * score_r), 2)
                        df.at[idx, 'Score Final Global'] = score_global
                        df.at[idx, 'Status da Rota'] = "Excelente" if score_global >= 90 else "Boa" if score_global >= 80 else "Aceitável" if score_global >= 70 else "Revisar"
                except Exception:
                    df.at[idx, 'Score Final Global'] = 0.0
                    df.at[idx, 'Status da Rota'] = "Erro"
                
                st.session_state['logs_auditoria'].append({
                    "Endereco Informado": origem, "Endereco Canonico": df.at[idx, 'Endereco Oficial Origem'],
                    "Vencedor": df.at[idx, 'Fonte Geocoding Origem'], "Score": df.at[idx, 'Score Num Origem'], 
                    "XAI Explicabilidade": " | ".join(res[26]) if len(res) > 26 and isinstance(res[26], list) else "N/A"
                })
            else:
                df.at[idx, 'Status da Rota'] = "Erro Crítico de Processamento"
                df.at[idx, 'Status Linha Reta'] = "Omitida por Erro Estrutural"
                
    return df

# ==============================================================================
# ENGINE DE CROSS-FILTERING GLOBAL (ALTAIR -> PYTHON STATE)
# ==============================================================================
def extrair_selecoes_altair():
    sel = {'regiao': [], 'uf': [], 'mun': [], 'status': [], 'linha_mun': [], 'scatter_mun': [], 'brush': {}}
    
    def _get_sel(key, field, sel_key):
        if key in st.session_state and 'selection' in st.session_state[key]:
            items = st.session_state[key]['selection'].get(sel_key, [])
            return [i[field] for i in items if isinstance(i, dict) and field in i]
        return []
        
    sel['regiao'] = _get_sel("dash_reg", 'Regiao_Sintetica_Origem', 'Reg')
    sel['uf'] = _get_sel("dash_uf", 'UF_Sintetica_Origem', 'UF')
    sel['status'] = _get_sel("dash_status", 'Status da Rota', 'Status')
    sel['mun'] = _get_sel("dash_mun", 'Municipio Origem', 'Mun')
    sel['linha_mun'] = _get_sel("dash_lr", 'Municipio Origem', 'LinhaMun')
    sel['scatter_mun'] = _get_sel("dash_scatter", 'Municipio Origem', 'ScatterMun')
    
    if "dash_scatter" in st.session_state and 'selection' in st.session_state["dash_scatter"]:
        sel['brush'] = st.session_state["dash_scatter"]['selection'].get('Brush', {})
        
    return sel

def sync_altair_to_widgets():
    sel = extrair_selecoes_altair()
    prev_sel_key = "prev_altair_sel"
    prev_sel = st.session_state.get(prev_sel_key, {'regiao':[], 'uf':[], 'mun':[], 'status':[], 'linha_mun':[], 'scatter_mun':[], 'brush':{}})
    
    def check_update(field, widget_key, default_val):
        if sel[field] != prev_sel[field]:
            st.session_state[widget_key] = sel[field][0] if sel[field] else default_val

    check_update('regiao', 'widget_regiao', 'Todas')
    check_update('uf', 'widget_uf', 'Todas')
    check_update('status', 'widget_status', 'Todos')
    
    if sel['mun'] != prev_sel['mun']:
        st.session_state['widget_mun'] = sel['mun'][0] if sel['mun'] else 'Todos'
    elif sel['linha_mun'] != prev_sel['linha_mun']:
        st.session_state['widget_mun'] = sel['linha_mun'][0] if sel['linha_mun'] else 'Todos'
    elif sel['scatter_mun'] != prev_sel['scatter_mun']:
        st.session_state['widget_mun'] = sel['scatter_mun'][0] if sel['scatter_mun'] else 'Todos'

    st.session_state[prev_sel_key] = sel

def aplicar_filtro_global(df_base, sel):
    df_cf = df_base.copy()
    if st.session_state.widget_regiao != "Todas": df_cf = df_cf[df_cf['Regiao_Sintetica_Origem'] == st.session_state.widget_regiao]
    if st.session_state.widget_uf != "Todas": df_cf = df_cf[df_cf['UF_Sintetica_Origem'] == st.session_state.widget_uf]
    if st.session_state.widget_mun != "Todos": df_cf = df_cf[df_cf['Municipio Origem'] == st.session_state.widget_mun]
    if st.session_state.widget_status != "Todos": df_cf = df_cf[df_cf['Status da Rota'] == st.session_state.widget_status]
    if st.session_state.widget_fonte != "Todas": df_cf = df_cf[df_cf['Fonte Geocoding Origem'] == st.session_state.widget_fonte]
    
    if sel['brush']:
        b = sel['brush']
        if 'Distancia' in b:
            df_cf = df_cf[(df_cf['Distancia'] >= b['Distancia'][0]) & (df_cf['Distancia'] <= b['Distancia'][1])]
        if 'Tempo_Horas' in b:
            df_cf = df_cf[(df_cf['Tempo_Horas'] >= b['Tempo_Horas'][0]) & (df_cf['Tempo_Horas'] <= b['Tempo_Horas'][1])]
            
    return df_cf

def renderizar_indicador_filtros(brush_active):
    active_html = ""
    if st.session_state.get('widget_regiao', 'Todas') != 'Todas':
        active_html += f"<span class='filter-badge'>Região: {st.session_state.widget_regiao}</span>"
    if st.session_state.get('widget_uf', 'Todas') != 'Todas':
        active_html += f"<span class='filter-badge'>UF: {st.session_state.widget_uf}</span>"
    if st.session_state.get('widget_mun', 'Todos') != 'Todos':
        active_html += f"<span class='filter-badge'>Município: {st.session_state.widget_mun}</span>"
    if st.session_state.get('widget_status', 'Todos') != 'Todos':
        active_html += f"<span class='filter-badge'>Status: {st.session_state.widget_status}</span>"
    if st.session_state.get('widget_fonte', 'Todas') != 'Todas':
        active_html += f"<span class='filter-badge'>Fonte: {st.session_state.widget_fonte}</span>"
    if brush_active:
        active_html += f"<span class='filter-badge'>Filtro de Área (Scatter)</span>"
        
    if active_html:
        st.markdown(f"<div style='background:#1E232F; padding:15px; border-radius:8px; border: 1px solid #3B82F6; margin-bottom:15px'><b>🔍 Filtros Ativos no Dashboard:</b><br><br> {active_html}</div>", unsafe_allow_html=True)

def get_agg_func(op_name):
    if 'Contagem Distinta' in op_name: return 'nunique'
    if 'Contagem' in op_name: return 'count'
    if 'Soma' in op_name: return 'sum'
    if 'Média' in op_name: return 'mean'
    if 'Mínimo' in op_name: return 'min'
    if 'Máximo' in op_name: return 'max'
    if 'Mediana' in op_name: return 'median'
    if 'Desvio Padrão' in op_name: return 'std'
    if 'Variância' in op_name: return 'var'
    if 'Percentil 25' in op_name: return lambda x: x.quantile(0.25)
    if 'Percentil 50' in op_name: return lambda x: x.quantile(0.50)
    if 'Percentil 75' in op_name: return lambda x: x.quantile(0.75)
    return 'count'

# ==============================================================================
# INTERFACE STREAMLIT COM ENGINE DE SIDEBAR MANUAL E ABAS DE AUDITORIA
# ==============================================================================
st.markdown("""
<div class="corporate-header">
    <h1 class="corporate-title">🗺️ Motor Nacional de Roteirização Inteligente</h1>
    <p class="corporate-subtitle">Plataforma Corporativa B2B de Geocodificação, Inferência Bayesiana e Auditoria Logística Avançada.</p>
</div>
""", unsafe_allow_html=True)

with st.sidebar:
    st.header("📖 Documentação Corporativa", help="Diretrizes estruturais, matemáticas e logísticas completas do motor corporativo.")
    
    with st.expander("📊 Visão Geral e Filosofia"):
        st.markdown("""
        O **Motor Nacional de Roteirização Inteligente** é o sistema core de inteligência logística B2B da operação. Diferente de sistemas comuns que dependem de uma única API comercial (correndo risco de indisponibilidade e falsos positivos topológicos), esta plataforma foi projetada com a arquitetura de **Pipeline Híbrido Multimotor**.
        """)

    with st.expander("🌐 Inteligência de Busca e Componentes do Ensemble"):
        st.markdown("""
        O sistema atua sob o princípio do **Ensemble Espacial Geográfico**. Em vez de confiar em um motor, ele consulta paralelamente (`ThreadPoolExecutor`):
        * **ArcGIS (ESRI):** Padrão-ouro em cadastros prediais corporativos.
        * **Nominatim & Photon (OSM):** Baseados no OpenStreetMap. Insubstituíveis para o interior do Brasil.
        * **TomTom Logistics:** Base fundamental B2B de tráfego pesado.
        * **BrasilAPI/ViaCEP/OpenCEP:** Cascata "Postal-Tripla".
        * **Base Nacional Offline (IBGE):** Cache em memória contendo o centróide matemático de todas as 5.570 cidades.
        """)

    with st.expander("📐 Matemática, Geodésia e Linha Reta"):
        st.markdown("""
        * **GeographicLib (Padrão Ouro WGS-84):** Fórmula de Karney.
        * **Geopy (Geodesic):** Motor de contingência.
        * **Haversine (Fallback):** Esfera perfeita (6371 km).
        * **Validação Anti-Zero:** Previne *overflows*.
        * **Bounding Box Territorial:** Bloqueia automaticamente coordenadas impossíveis.
        """)

    st.markdown("---")
    st.subheader("💡 Suporte e Feedback")
    st.caption("Envie uma solicitação diretamente para a equipe de Engenharia (Requer SMTP).")
    
    with st.form(key="form_sugestao"):
        sugestao_texto = st.text_area("Descreva a anomalia ou melhoria:", height=100)
        remetente_email = st.text_input("Seu e-mail corporativo (opcional):")
        submit_button = st.form_submit_button("🚀 Enviar Ticket de Manutenção")
        
        if submit_button:
            if sugestao_texto.strip() == "":
                st.warning("O ticket não pode estar vazio.")
            else:
                try:
                    smtp_server = "smtp.gmail.com"
                    smtp_port = 587
                    smtp_user = st.secrets.get("EMAIL_SISTEMA", "seu_email_de_envio@gmail.com") 
                    smtp_pass = st.secrets.get("SENHA_APP", "sua_senha_de_aplicativo")
                    if smtp_user == "seu_email_de_envio@gmail.com":
                        st.info("⚠️ Modo de Demonstração: Configure 'EMAIL_SISTEMA' e 'SENHA_APP' nas variáveis de ambiente.")
                    else:
                        msg = MIMEMultipart()
                        msg['From'] = smtp_user
                        msg['To'] = "lucas.c.cruz@gmail.com"
                        msg['Subject'] = "Ticket de Manutenção - Motor Corporativo de Rotas"
                        corpo = f"Novo Ticket gerado no painel UX:\n\nRemetente: {remetente_email}\n\nDescrição:\n{sugestao_texto}"
                        msg.attach(MIMEText(corpo, 'plain'))
                        server = smtplib.SMTP(smtp_server, smtp_port)
                        server.starttls()
                        server.login(smtp_user, smtp_pass)
                        server.send_message(msg)
                        server.quit()
                        st.success("✅ Ticket transmitido com sucesso via backbone!")
                except Exception as e:
                    st.error(f"Erro ao tentar transmitir a solicitação via SMTP: {str(e)}")

tab_individual, tab_processamento, tab_alocacao, tab_analytics, tab_calculadora, tab_classificacao, tab_enciclopedia, tab_manual, tab_motores, tab_auditoria = st.tabs([
    "📍 Geocodificação", "⚙️ Processamento Lote", "📦 Alocação de Hubs", "📊 Enterprise Analytics", "🧮 Calculadora Analítica", "🚨 Classificação Territorial", "📚 Enciclopédia Core", "📘 Manual do Usuário", "🔌 Monitor APIs", "🕵️ Auditoria"
])

with tab_individual:
    st.info("💡 **Objetivo desta aba:** Validar rapidamente uma única rota. Digite a Origem e o Destino para obter a distância viária oficial do Google Maps, o desvio geodésico rigoroso e a explicabilidade do motor de geocodificação.")
    st.markdown("### 🔍 Validador Rápido de Rota (Single-Shot)")
    col_ind1, col_ind2 = st.columns(2)
    with col_ind1: orig_ind = st.text_input("Origem (Endereço, POI ou Coordenadas)", "Ribeirão Cascalheira , MT, Brasil", help="Insira o local de partida. O sistema bloqueará a busca apenas para o Estado cuja sigla for identificada.")
    with col_ind2: dest_ind = st.text_input("Destino (Endereço, POI ou Coordenadas)", "SAO MIGUEL DO ARAGUAIA , GO, Brasil", help="Insira o destino final. O uso de UF (Ex: GO) assegura máxima precisão contra localidades homônimas em outros estados.")
    
    if st.button("🚀 Calcular Rota Individual", type="primary", help="Inicia o pipeline Bayesiano para geocodificação e aciona os motores do Google Maps e OSRM para o trajeto."):
        if orig_ind and dest_ind:
            with st.spinner("Acionando motores de geocodificação e consenso unificado..."):
                res_ind = executar_pipeline_unificado(orig_ind, dest_ind)
                
            if res_ind and res_ind[28] != "Falha na leitura da célula (Campo Vazio)." and "FALHA INTERNA" not in res_ind[28]:
                st.success("✅ Rota estabelecida com sucesso na malha viária!")
                m_dist_via, m_dist_reta, m_time, m_balsa, m_score = st.columns(5)
                m_dist_via.metric("Distância Viária", f"{res_ind[0]} km" if isinstance(res_ind[0], float) else res_ind[0])
                m_dist_reta.metric("Distância Linha Reta", f"{res_ind[4]} km" if isinstance(res_ind[4], float) else res_ind[4])
                m_time.metric("Tempo Estimado", res_ind[1])
                m_balsa.metric("Uso de Balsas", res_ind[3])
                
                score_g = round((0.35 * res_ind[8]) + (0.35 * res_ind[14]) + (0.30 * res_ind[6]), 2)
                m_score.metric("Score Global", f"{score_g} / 100")
                
                st.info(f"🧠 **Estratégia de Roteamento (XAI):** {res_ind[28]}")
                st.caption(f"🔧 **Status da Linha Reta:** {res_ind[30] if len(res_ind) > 30 else 'Não Mapeado'}")
                
                with st.expander("🕵️ Auditoria Detalhada da Geocodificação e Consenso", expanded=False):
                    st.caption(f"Status da Base IBGE Local: {'Ativa e Carregada' if len(IBGE_MUNICIPIOS) > 1000 else '⚠️ CORROMPIDA/FALHA DE API'}")
                    col_aud1, col_aud2 = st.columns(2)
                    with col_aud1:
                        st.markdown("**🏁 Origem (Ponto A)**")
                        st.write(f"**Endereço Oficial:** {res_ind[12]}")
                        st.write(f"**Coordenadas:** {res_ind[19]}, {res_ind[20]}")
                        st.write(f"**Motor Vencedor:** {res_ind[11]}")
                        st.write(f"**Confiança & Score:** {res_ind[7]} ({res_ind[8]}/100)")
                        st.write(f"**Justificativa Espacial:**")
                        for just in res_ind[26]: st.caption(f"- {just}")
                    with col_aud2:
                        st.markdown("**🎯 Destino (Ponto B)**")
                        st.write(f"**Endereço Oficial:** {res_ind[18]}")
                        st.write(f"**Coordenadas:** {res_ind[21]}, {res_ind[22]}")
                        st.write(f"**Motor Vencedor:** {res_ind[17]}")
                        st.write(f"**Confiança & Score:** {res_ind[13]} ({res_ind[14]}/100)")
                        st.write(f"**Justificativa Espacial:**")
                        for just in res_ind[27]: st.caption(f"- {just}")

                url_iframe = res_ind[29]
                try: components.iframe(url_iframe, height=470, scrolling=True)
                except Exception: st.warning("Renderização de mapa localmente bloqueada pelas políticas de segurança do navegador.")
                st.markdown(f"[🔗 Abrir Rota Completa no Aplicativo do Google Maps]({res_ind[2]})")
            else:
                st.error("Falha na validação de consistência geodésica unificada.")
        else:
            st.warning("Preencha origem e destino para inicializar o cálculo.")

with tab_processamento:
    st.info("💡 **Objetivo desta aba:** Processamento em massa O(U). Envie uma planilha Excel com milhares de origens e destinos. O sistema extrairá rotas únicas, calculará os desvios de todas simultaneamente e devolverá a planilha rigorosamente preenchida.")
    arquivo_carregado = st.file_uploader("Selecionar Arquivo Excel", type=["xlsx"], key="lote_std", help="A planilha deve conter as colunas 'Origem' e 'Destino'.")

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
                
            st.success(f"Tabela com {len(df)} registros mapeada! Pronto para processar o Lote Unificado.")
            nome_operador = st.text_input("Matrícula / Nome do Operador (Opcional)", max_chars=50)
            
            if st.button("Iniciar Processamento em Lote", type="primary"):
                start_lote_clock = time.time()
                novas_colunas = [
                    'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Motivo Roteamento', 'Status Linha Reta', 'Linha Reta', 'Fonte da Rota', 'Score da Rota', 
                    'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem', 'Fonte Geocoding Origem', 'Endereco Oficial Origem',
                    'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino', 'Fonte Geocoding Destino', 'Endereco Oficial Destino',
                    'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)', 'Score Final Global', 'Status da Rota'
                ]
                colunas_numericas = ['Distancia', 'Linha Reta', 'Score da Rota', 'Score Num Origem', 'Score Num Destino', 'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)', 'Score Final Global']
                
                for col in novas_colunas:
                    if col in colunas_numericas:
                        if col not in df.columns: df[col] = 0.0
                        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0).astype(float)
                    else:
                        if col not in df.columns: df[col] = "Não Informado"
                        df[col] = df[col].astype(object)
                    
                pares_unicos = set()
                for index, linha in df.iterrows():
                    origem = str(linha.get('Origem', '')).strip() if pd.notna(linha.get('Origem', '')) else ""
                    destino = str(linha.get('Destino', '')).strip() if pd.notna(linha.get('Destino', '')) else ""
                    if origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan':
                        pares_unicos.add((origem, destino))
                
                if not pares_unicos:
                    st.warning("Nenhuma linha contendo endereços válidos detectada após sanitização.")
                    st.stop()
                    
                MAPA_PRIORIDADE = {"CEP": 1, "ENDERECO_COMPLETO": 2, "POI": 3, "CONDOMINIO": 3, "MUNICIPIO": 4, "BAIRRO": 5, "RURAL": 6, "LOGRADOURO": 7}
                tarefas_priorizadas = []
                for p in pares_unicos:
                    tipo_o = semantica.classificar_entrada(semantica.normalizar(p[0]))
                    tarefas_priorizadas.append((MAPA_PRIORIDADE.get(tipo_o, 99), p))
                tarefas_priorizadas.sort(key=lambda x: x[0])
                
                st.info(f"Otimização O(U) com Fila Inteligente Ativa: {len(pares_unicos)} rotas exclusivas na esteira de processamento pipeline-unificado.")
                
                barra_progresso = st.progress(0)
                container_status = st.empty()
                
                df_final = rodar_pipeline_lote(df, list(pares_unicos), tarefas_priorizadas, nome_operador, barra_progresso, container_status)
                
                def recalculate_haversine_lote(row):
                    lat_o_f, lon_o_f = float(row.get('Lat Origem', 0.0)), float(row.get('Lon Origem', 0.0))
                    lat_d_f, lon_d_f = float(row.get('Lat Destino', 0.0)), float(row.get('Lon Destino', 0.0))
                    if lat_o_f != 0.0 and lat_d_f != 0.0:
                        nova_dist, novo_status = calcular_distancia_linha_reta(lat_o_f, lon_o_f, lat_d_f, lon_d_f, contexto=f"DF Lote Post-Sweep: {row.get('Origem','')} a {row.get('Destino','')}")
                        if nova_dist > 0: return pd.Series([nova_dist, novo_status])
                    return pd.Series([row['Linha Reta'], row['Status Linha Reta']])
                
                df_final[['Linha Reta', 'Status Linha Reta']] = df_final.apply(recalculate_haversine_lote, axis=1)

                tempo_lote_segundos = round(time.time() - start_lote_clock, 2)
                cache_historico_lotes.set(f"lote_{start_lote_clock}", {
                    "Data/Hora": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "Operador": nome_operador.strip() if nome_operador.strip() else "Operador Padrão",
                    "Linhas Validadas": len(pares_unicos),
                    "Tempo Gasto (s)": tempo_lote_segundos,
                    "Tempo Médio/Rota (s)": round(tempo_lote_segundos / max(1, len(pares_unicos)), 2)
                }, expire=None)

                ordem_finais = list(df.columns)
                for c in novas_colunas:
                    if c not in ordem_finais: ordem_finais.append(c)
                df_final = df_final.reindex(columns=ordem_finais)
                
                st.session_state['df_processado'] = df_final
                container_status.empty(); barra_progresso.empty()
                st.success("✨ Processamento em lote corporativo concluído com êxito e Linhas Retas Auditadas!")
                
                output_buffer = io.BytesIO()
                with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer: df_final.to_excel(writer, index=False)
                st.session_state['planilha_pronta'] = output_buffer.getvalue()

        if 'df_processado' in st.session_state and 'planilha_pronta' in st.session_state:
            st.write("---")
            st.balloons()
            st.markdown("### 📋 Prévia Interativa da Planilha Final")
            st.dataframe(st.session_state['df_processado'], use_container_width=True, height=250)
            col_down1, col_down2 = st.columns(2)
            with col_down1:
                st.download_button(label="📥 Baixar Planilha (.xlsx)", data=st.session_state['planilha_pronta'], file_name="planilha_rotas_calculada.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
            with col_down2:
                st.markdown("""<a href="https://sheets.new/" target="_blank" style="display:inline-block; padding:0.5em 1em; background-color:#1E90FF; color:white; border-radius:5px; text-decoration:none; font-weight:bold; text-align:center; width:100%; transition: all 0.2s;">📊 Abrir Google Sheets Vazio</a>""", unsafe_allow_html=True)

with tab_alocacao:
    st.info("💡 **Objetivo desta aba:** Inteligência Logística de Hubs. Envie uma lista de clientes (Origens) e uma lista de Centros de Distribuição/Bases (Destinos). O sistema calculará todas as combinações espaciais e descobrirá automaticamente qual é a Base Logística mais próxima de cada cliente individualmente.")
    col_a1, col_a2 = st.columns(2)
    with col_a1: file_dest = st.file_uploader("1. Planilha de Endereços / Entregas (Origens)", type=["xlsx"], key="up_dests_v19")
    with col_a2: file_hubs = st.file_uploader("2. Planilha de Municípios / Bases (Destinos)", type=["xlsx"], key="up_hubs_v19")
    
    if file_hubs and file_dest:
        df_hubs = pd.read_excel(file_hubs)
        df_dest = pd.read_excel(file_dest)
        
        col_s1, col_s2 = st.columns(2)
        with col_s1: dest_col_name = st.selectbox("Selecione a coluna que contém os Endereços (Origens):", df_dest.columns)
        with col_s2: hub_col_name = st.selectbox("Selecione a coluna que contém os Municípios/Bases (Destinos):", df_hubs.columns)
        
        if st.button("🗺️ Processar Cruzamento Espacial e Roteamento Duplo", type="primary"):
            start_alo_clock = time.time()
            hubs_unicos = df_hubs[hub_col_name].dropna().astype(str).str.strip().unique().tolist()
            dests_unicos = df_dest[dest_col_name].dropna().astype(str).str.strip().unique().tolist()
            
            if not hubs_unicos or not dests_unicos:
                st.error("Uma das colunas selecionadas está vazia ou é inválida.")
            else:
                progress_alo = st.progress(0)
                status_alo = st.empty()
                if 'logs_auditoria_alocacao' not in st.session_state: st.session_state['logs_auditoria_alocacao'] = []
                st.session_state['logs_auditoria_alocacao'].clear()
                
                status_alo.text("Fase 1/3: Geocodificando e blindando Hubs Logísticos...")
                hub_coords = {}
                for i, h in enumerate(hubs_unicos):
                    progress_alo.progress((i + 1) / len(hubs_unicos))
                    lat, lon, end, conf, score, dist, mun, fonte, xai = obter_coordenadas_e_endereco_oficial(h)
                    hub_coords[h] = (lat, lon, end)
                    st.session_state['logs_auditoria_alocacao'].append({"Categoria": "Base/Hub (Destino)", "Nome Original": h, "Coordenada": f"{lat}, {lon}", "Endereço Oficializado": end, "Score": score, "Validação XAI": " | ".join(xai)})
                    time.sleep(0.05)
                
                hubs_validos = {k: v for k, v in hub_coords.items() if v[0] != 0.0}
                
                if not hubs_validos:
                    st.error("CRÍTICO: Nenhuma Base/Hub pôde ser geocodificada no mapa.")
                    status_alo.empty(); progress_alo.empty()
                else:
                    status_alo.text("Fase 2/3: Geocodificando Endereços de Origem...")
                    dest_coords = {}
                    for i, d in enumerate(dests_unicos):
                        progress_alo.progress((i + 1) / len(dests_unicos))
                        lat, lon, end, conf, score, dist, mun, fonte, xai = obter_coordenadas_e_endereco_oficial(d)
                        dest_coords[d] = (lat, lon, end)
                        st.session_state['logs_auditoria_alocacao'].append({"Categoria": "Endereço (Origem)", "Nome Original": d, "Coordenada": f"{lat}, {lon}", "Endereço Oficializado": end, "Score": score, "Validação XAI": " | ".join(xai)})
                        time.sleep(0.05)
                    
                    status_alo.text("Fase 3/3: Calculando Matriz Competitiva e montando Pipeline...")
                    dest_to_hub, dest_to_linha_reta, dest_to_status_lr, runner_up_map = {}, {}, {}, {}
                    
                    for o_nome, (o_lat, o_lon, o_end) in dest_coords.items():
                        if o_lat == 0.0 or o_lon == 0.0:
                            dest_to_hub[o_nome], dest_to_status_lr[o_nome] = "FALHA_GEO_ORIGEM", "Falha Espacial"
                            continue
                            
                        hubs_dist = []
                        for h_nome, (h_lat, h_lon, h_end) in hubs_validos.items():
                            dist_v, stat_v = calcular_distancia_linha_reta(o_lat, o_lon, h_lat, h_lon, contexto=f"Hub Allocation: {o_nome} a {h_nome}")
                            hubs_dist.append((dist_v, h_nome, h_lat, h_lon, stat_v))
                        hubs_dist.sort(key=lambda x: x[0])
                        
                        if hubs_dist:
                            dest_to_hub[o_nome], dest_to_linha_reta[o_nome], dest_to_status_lr[o_nome] = hubs_dist[0][1], hubs_dist[0][0], hubs_dist[0][4]
                            if len(hubs_dist) > 1: runner_up_map[o_nome] = (hubs_dist[1][0], hubs_dist[1][1], hubs_dist[1][2], hubs_dist[1][3])
                        else:
                            dest_to_hub[o_nome], dest_to_status_lr[o_nome] = "NENHUM_HUB_VALIDO", "Falha Estrutural de Hubs"
                    
                    df_pares = df_dest.copy()
                    df_pares['Origem'] = df_pares[dest_col_name].astype(str).str.strip()
                    df_pares['Destino'] = df_pares['Origem'].map(dest_to_hub).fillna("FALHA_GEO_ORIGEM")
                    
                    novas_colunas = [
                        'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Motivo Roteamento', 'Status Linha Reta', 'Linha Reta', 'Fonte da Rota', 'Score da Rota', 
                        'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem', 'Fonte Geocoding Origem', 'Endereco Oficial Origem',
                        'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino', 'Fonte Geocoding Destino', 'Endereco Oficial Destino',
                        'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)', 'Score Final Global', 'Status da Rota',
                        'Concorrente Analisado', 'Distancia Concorrente', 'Link Rota Concorrente', 'Justificativa de Alocacao'
                    ]
                    colunas_numericas = ['Distancia', 'Linha Reta', 'Score da Rota', 'Score Num Origem', 'Score Num Destino', 'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)', 'Score Final Global', 'Distancia Concorrente']
                    
                    for col in novas_colunas:
                        if col in colunas_numericas:
                            if col not in df_pares.columns: df_pares[col] = 0.0
                            df_pares[col] = pd.to_numeric(df_pares[col], errors='coerce').fillna(0.0).astype(float)
                        else:
                            if col not in df_pares.columns: df_pares[col] = "Não Informado"
                            df_pares[col] = df_pares[col].astype(object)

                    pares_unicos_alo = set()
                    MAPA_PRIORIDADE = {"CEP": 1, "ENDERECO_COMPLETO": 2, "POI": 3, "CONDOMINIO": 3, "MUNICIPIO": 4, "BAIRRO": 5, "RURAL": 6, "LOGRADOURO": 7}
                    tarefas_priorizadas_alo = []
                    
                    for index, linha in df_pares.iterrows():
                        o, d = str(linha['Origem']).strip(), str(linha['Destino']).strip()
                        if o and d and o != "FALHA_GEO_ORIGEM" and d != "NENHUM_HUB_VALIDO" and pd.notna(o) and pd.notna(d):
                            if (o, d) not in pares_unicos_alo:
                                pares_unicos_alo.add((o, d))
                                tipo_o = semantica.classificar_entrada(semantica.normalizar(o))
                                tarefas_priorizadas_alo.append((MAPA_PRIORIDADE.get(tipo_o, 99), (o, d)))
                    
                    tarefas_priorizadas_alo.sort(key=lambda x: x[0])
                    df_final_alo = rodar_pipeline_lote(df_pares, list(pares_unicos_alo), tarefas_priorizadas_alo, "Operador Matriz", progress_alo, status_alo, runner_up_map)
                    status_alo.empty(); progress_alo.empty()
                    
                    df_final_alo['Linha Reta'] = df_final_alo['Origem'].astype(str).str.strip().map(dest_to_linha_reta).fillna(df_final_alo['Linha Reta'])
                    df_final_alo['Status Linha Reta'] = df_final_alo['Origem'].astype(str).str.strip().map(dest_to_status_lr).fillna(df_final_alo['Status Linha Reta'])
                    
                    def recalculate_haversine_alo(row):
                        lat_o_f, lon_o_f = float(row.get('Lat Origem', 0.0)), float(row.get('Lon Origem', 0.0))
                        lat_d_f, lon_d_f = float(row.get('Lat Destino', 0.0)), float(row.get('Lon Destino', 0.0))
                        if lat_o_f != 0.0 and lat_d_f != 0.0:
                            nova_dist, novo_status = calcular_distancia_linha_reta(lat_o_f, lon_o_f, lat_d_f, lon_d_f, contexto=f"Alo DF Revalidação: {row.get('Origem','')} a {row.get('Destino','')}")
                            if nova_dist > 0: return pd.Series([nova_dist, novo_status])
                        return pd.Series([row['Linha Reta'], row['Status Linha Reta']])
                    
                    df_final_alo[['Linha Reta', 'Status Linha Reta']] = df_final_alo.apply(recalculate_haversine_alo, axis=1)

                    tempo_alo_segundos = round(time.time() - start_alo_clock, 2)
                    cache_historico_lotes.set(f"alocacao_{start_alo_clock}", {
                        "Data/Hora": time.strftime("%Y-%m-%d %H:%M:%S"), "Operador": "Motor de Alocação (Hubs)", "Linhas Validadas": len(df_final_alo),
                        "Tempo Gasto (s)": tempo_alo_segundos, "Tempo Médio/Rota (s)": round(tempo_alo_segundos / max(1, len(pares_unicos_alo)), 2)
                    }, expire=None)

                    st.session_state['df_processado'] = df_final_alo
                    st.success(f"✨ Matriz resolvida e Duelos concluídos! {len(df_final_alo)} linhas originais foram rigorosamente preservadas e preenchidas.")
                    
                    ordem_finais_alo = list(df_dest.columns)
                    for c in ['Origem', 'Destino'] + novas_colunas:
                        if c not in ordem_finais_alo: ordem_finais_alo.append(c)
                    df_final_alo = df_final_alo.reindex(columns=ordem_finais_alo)

                    st.dataframe(df_final_alo, use_container_width=True, height=250)
                    output_buffer = io.BytesIO()
                    with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer: df_final_alo.to_excel(writer, index=False)
                    st.download_button(label="📥 Baixar Planilha de Alocação Competitiva (.xlsx)", data=output_buffer.getvalue(), file_name="matriz_alocacao_competitiva.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

with tab_analytics:
    st.info("💡 **Objetivo desta aba:** Sistema Analítico Global estilo Power BI. Clique nas fatias, barras ou arraste o mouse no Scatter Plot para filtrar dinamicamente TODOS os indicadores, mapas e tabelas abaixo.")
    
    col_d_title, col_d_btn = st.columns([80, 20])
    with col_d_title: st.markdown("### 📊 Enterprise Analytics Dashboard")
    with col_d_btn:
        if st.button("🧹 Limpar Todos os Filtros", use_container_width=True):
            keys_to_clear = ['widget_regiao', 'widget_uf', 'widget_mun', 'widget_status', 'widget_fonte', 'dash_reg', 'dash_uf', 'dash_status', 'dash_mun', 'dash_lr', 'dash_scatter', 'prev_altair_sel']
            for k in keys_to_clear:
                if k in st.session_state: del st.session_state[k]
            st.rerun()

    if 'df_processado' in st.session_state:
        sel = extrair_selecoes_altair()
        
        if 'widget_regiao' not in st.session_state: st.session_state['widget_regiao'] = 'Todas'
        if 'widget_uf' not in st.session_state: st.session_state['widget_uf'] = 'Todas'
        if 'widget_mun' not in st.session_state: st.session_state['widget_mun'] = 'Todos'
        if 'widget_status' not in st.session_state: st.session_state['widget_status'] = 'Todos'
        if 'widget_fonte' not in st.session_state: st.session_state['widget_fonte'] = 'Todas'

        sync_altair_to_widgets()
        
        df_kpi = st.session_state['df_processado'].copy()
        df_kpi['Distancia'] = pd.to_numeric(df_kpi['Distancia'], errors='coerce').fillna(0)
        df_kpi['Linha Reta'] = pd.to_numeric(df_kpi['Linha Reta'], errors='coerce').fillna(0)
        df_kpi['Tempo_Minutos'] = df_kpi['Tempo'].apply(parse_tempo_minutos)
        df_kpi['Tempo_Horas'] = df_kpi['Tempo_Minutos'] / 60.0
        
        MAPA_ESTADOS_FULL = {
            "ACRE": "AC", "ALAGOAS": "AL", "AMAPA": "AP", "AMAZONAS": "AM", "BAHIA": "BA", "CEARA": "CE", "DISTRITO FEDERAL": "DF", 
            "ESPIRITO SANTO": "ES", "GOIAS": "GO", "MARANHAO": "MA", "MATO GROSSO": "MT", "MATO GROSSO DO SUL": "MS", "MINAS GERAIS": "MG", 
            "PARA": "PA", "PARAIBA": "PB", "PARANA": "PR", "PERNAMBUCO": "PE", "PIAUI": "PI", "RIO DE JANEIRO": "RJ", "RIO GRANDE DO NORTE": "RN",
            "RIO GRANDE DO SUL": "RS", "RONDONIA": "RO", "RORAIMA": "RR", "SANTA CATARINA": "SC", "SAO PAULO": "SP", "SERGIPE": "SE", "TOCANTINS": "TO"
        }

        REGIOES_BRASIL = {
            "Norte": ["AC", "AP", "AM", "PA", "RO", "RR", "TO"], "Nordeste": ["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"],
            "Centro-Oeste": ["DF", "GO", "MT", "MS"], "Sudeste": ["ES", "MG", "RJ", "SP"], "Sul": ["PR", "RS", "SC"]
        }
        
        def extrair_uf_precisa(endereco):
            if not isinstance(endereco, str): return "Indefinido"
            end_upper = unidecode(endereco.upper())
            for nome, sigla in MAPA_ESTADOS_FULL.items():
                if f" {nome} " in f" {end_upper} " or end_upper.endswith(nome) or f", {nome}," in end_upper: return sigla
            padrao_uf = r'\b(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b'
            partes = [p.strip() for p in end_upper.split(',')]
            for p in reversed(partes):
                match = re.search(padrao_uf, p)
                if match: return match.group(1)
            return "Indefinido"
            
        df_kpi['UF_Sintetica_Origem'] = df_kpi['Endereco Oficial Origem'].apply(extrair_uf_precisa)
        df_kpi['Regiao_Sintetica_Origem'] = df_kpi['UF_Sintetica_Origem'].apply(lambda uf: next((regiao for regiao, ufs in REGIOES_BRASIL.items() if uf in ufs), "Indefinido"))
        
        lista_regioes = ["Todas"] + sorted([x for x in df_kpi['Regiao_Sintetica_Origem'].unique() if pd.notna(x)])
        if st.session_state['widget_regiao'] not in lista_regioes: st.session_state['widget_regiao'] = 'Todas'
        lista_ufs = ["Todas"] + sorted([x for x in df_kpi['UF_Sintetica_Origem'].unique() if pd.notna(x)])
        if st.session_state['widget_uf'] not in lista_ufs: st.session_state['widget_uf'] = 'Todas'
        lista_municipios = ["Todos"] + sorted([str(x) for x in df_kpi['Municipio Origem'].unique() if pd.notna(x)])
        if st.session_state['widget_mun'] not in lista_municipios: st.session_state['widget_mun'] = 'Todos'
        lista_status = ["Todos"] + sorted([str(x) for x in df_kpi['Status da Rota'].unique() if pd.notna(x)])
        if st.session_state['widget_status'] not in lista_status: st.session_state['widget_status'] = 'Todos'
        lista_fontes = ["Todas"] + sorted([str(x) for x in df_kpi['Fonte Geocoding Origem'].unique() if pd.notna(x)])
        if st.session_state['widget_fonte'] not in lista_fontes: st.session_state['widget_fonte'] = 'Todas'
        
        st.markdown("#### 🎛️ Painel de Controle de Filtros Avançados (Bidirecional)")
        with st.expander("Filtros Globais Sincronizados", expanded=False):
            col_f0, col_f1, col_f2, col_f3, col_f4 = st.columns(5)
            regiao_selecionada = col_f0.selectbox("Região do Brasil", lista_regioes, key="widget_regiao")
            uf_selecionada = col_f1.selectbox("UF de Origem", lista_ufs, key="widget_uf")
            mun_selecionado = col_f2.selectbox("Município de Origem", lista_municipios, key="widget_mun")
            status_selecionado = col_f3.selectbox("Status Global da Rota", lista_status, key="widget_status")
            fonte_selecionada = col_f4.selectbox("Fonte de Geocoding", lista_fontes, key="widget_fonte")
            
            col_f5, col_f6, col_f7 = st.columns(3)
            min_dist_val, max_dist_val = float(df_kpi['Distancia'].min()), float(df_kpi['Distancia'].max())
            if max_dist_val <= min_dist_val: max_dist_val = min_dist_val + 1.0
            dist_range = col_f5.slider("Faixa de Distância Viária (km)", min_value=0.0, max_value=max_dist_val, value=(0.0, max_dist_val))
            
            min_time_val, max_time_val = float(df_kpi['Tempo_Horas'].min()), float(df_kpi['Tempo_Horas'].max())
            if max_time_val <= min_time_val: max_time_val = min_time_val + 1.0
            time_range = col_f6.slider("Faixa de Tempo Estimado (Horas)", min_value=0.0, max_value=max_time_val, value=(0.0, max_time_val))
            
            min_score_val, max_score_val = float(df_kpi['Score Final Global'].min()), float(df_kpi['Score Final Global'].max())
            if max_score_val <= min_score_val: max_score_val = min_score_val + 1.0
            score_range = col_f7.slider("Score de Integridade Geodésica", min_value=0.0, max_value=100.0, value=(min_score_val, 100.0))

        df_cf = aplicar_filtro_global(df_kpi, extrair_selecoes_altair())
        df_cf = df_cf[(df_cf['Distancia'] >= dist_range[0]) & (df_cf['Distancia'] <= dist_range[1])]
        df_cf = df_cf[(df_cf['Tempo_Horas'] >= time_range[0]) & (df_cf['Tempo_Horas'] <= time_range[1])]
        df_cf = df_cf[(df_cf['Score Final Global'] >= score_range[0]) & (df_cf['Score Final Global'] <= score_range[1])]
        
        df_cf['_is_selected'] = 1
        st.session_state['df_cf_master'] = df_cf

        renderizar_indicador_filtros(extrair_selecoes_altair()['brush'])

        if df_cf.empty:
            st.warning("A combinação de filtros cruzados selecionada não retornou nenhum registro neste lote. Limpe os filtros.")
        else:
            df_sucesso = df_cf[~df_cf["Status da Rota"].str.contains("Erro")]
            tab_kpi_nacional, tab_kpi_regional = st.tabs(["🌎 Visão Nacional Macro", "📍 Análise Regionalizada"])
            
            with tab_kpi_nacional:
                with st.container(border=True):
                    col_k1, col_k2, col_k3, col_k4, col_k5, col_k6 = st.columns(6)
                    total_distancia = df_sucesso['Distancia'].sum()
                    total_tempo_mins = df_sucesso['Tempo_Minutos'].sum()
                    tempo_total_str = f"{total_tempo_mins // 60}h {total_tempo_mins % 60}m"
                    dist_media = total_distancia / len(df_sucesso) if len(df_sucesso) > 0 else 0
                    tempo_medio = total_tempo_mins / len(df_sucesso) if len(df_sucesso) > 0 else 0
                    tempo_medio_str = f"{int(tempo_medio // 60)}h {int(tempo_medio % 60)}m"
                    
                    col_k1.metric("Rotas Selecionadas", f"{len(df_cf)}")
                    col_k2.metric("Distância Acumulada", f"{round(total_distancia, 1)} km")
                    col_k3.metric("Tempo Acumulado", f"{tempo_total_str}")
                    col_k4.metric("Distância Média/Rota", f"{round(dist_media, 1)} km")
                    col_k5.metric("Tempo Médio/Rota", f"{tempo_medio_str}")
                    col_k6.metric("Score Geodésico Médio", f"{round(df_sucesso['Score Final Global'].mean(), 1) if not df_sucesso.empty else 0}/100")

                    st.divider()
                    
                    col_k7, col_k8, col_k9, col_k10, col_k11, col_k12 = st.columns(6)
                    muns_atendidos = df_cf['Municipio Destino'].nunique()
                    ufs_atendidas = df_cf['Endereco Oficial Destino'].apply(extrair_uf_precisa).nunique()
                    rotas_balsa = len(df_cf[df_cf['Balsas'] == 'Sim'])
                    taxa_sucesso = round((len(df_sucesso) / len(df_cf)) * 100, 1) if len(df_cf) > 0 else 0
                    
                    col_k7.metric("Cidades Alcançadas", f"{muns_atendidos}")
                    col_k8.metric("Estados Alcançados (UFs)", f"{ufs_atendidas}")
                    col_k9.metric("Maior Viagem Mapeada", f"{round(df_cf['Distancia'].max(), 1)} km")
                    col_k10.metric("Rotas Fluviais (Balsa)", f"{rotas_balsa}")
                    col_k11.metric("Taxa de Sucesso (Roteamento)", f"{taxa_sucesso}%")
                    col_k12.metric("Confiança 'Altíssima'", f"{len(df_cf[df_cf['Confianca Destino'] == 'ALTISSIMA'])}")
                    
            with tab_kpi_regional:
                with st.container(border=True):
                    df_regioes = df_cf.groupby('Regiao_Sintetica_Origem').agg(
                        Rotas=('Origem', 'count'),
                        Dist_Media=('Distancia', 'mean'),
                        Tempo_Medio_Horas=('Tempo_Horas', 'mean'),
                        Score_Medio=('Score Final Global', 'mean'),
                        Muns_Unicos=('Municipio Origem', 'nunique')
                    ).reset_index()
                    df_regioes = df_regioes[df_regioes['Regiao_Sintetica_Origem'] != "Indefinido"]
                    df_regioes['Participacao_Nacional'] = round((df_regioes['Rotas'] / len(df_cf)) * 100, 1)
                    
                    if not df_regioes.empty:
                        col_r1, col_r2 = st.columns(2)
                        with col_r1:
                            bar_regiao = alt.Chart(df_regioes).mark_bar(color='#3B82F6').encode(
                                x=alt.X('Rotas:Q', title='Volume de Rotas'),
                                y=alt.Y('Regiao_Sintetica_Origem:N', sort='-x', title='Região do Brasil'),
                                tooltip=['Regiao_Sintetica_Origem', 'Rotas', 'Participacao_Nacional', 'Dist_Media', 'Muns_Unicos']
                            ).properties(height=280, title="Ranking de Volume por Região (Filtrado)")
                            st.altair_chart(bar_regiao, use_container_width=True)
                        with col_r2:
                            st.write("Tabela Mestre Regional")
                            st.dataframe(df_regioes.rename(columns={'Regiao_Sintetica_Origem': 'Região Geográfica', 'Dist_Media': 'Distância Média (km)', 'Tempo_Medio_Horas': 'Tempo Médio (h)', 'Score_Medio': 'Score Médio', 'Muns_Unicos': 'Municípios Atendidos', 'Participacao_Nacional': 'Share Selecionado (%)'}), use_container_width=True, hide_index=True)
                    else:
                        st.info("Não há dados regionais válidos mapeados neste lote/recorte.")
            
            st.markdown("#### 📈 Análise Operacional e Motor Interativo de Filtros")
            with st.container(border=True):
                click_reg = alt.selection_point(fields=['Regiao_Sintetica_Origem'], name='Reg')
                click_uf = alt.selection_point(fields=['UF_Sintetica_Origem'], name='UF')
                click_mun = alt.selection_point(fields=['Municipio Origem'], name='Mun')
                click_linha = alt.selection_point(fields=['Municipio Origem'], name='LinhaMun')
                click_status = alt.selection_point(fields=['Status da Rota'], name='Status')
                click_scatter = alt.selection_point(fields=['Municipio Origem'], name='ScatterMun')
                brush = alt.selection_interval(name='Brush')

                base_chart = alt.Chart(df_cf)

                chart_reg = base_chart.mark_bar(cornerRadiusEnd=4).encode(
                    x=alt.X('count():Q', title='Volume de Rotas', axis=alt.Axis(grid=False)),
                    y=alt.Y('Regiao_Sintetica_Origem:N', sort='-x', title='Região'),
                    color=alt.value('#60A5FA'),
                    opacity=alt.condition(click_reg & (alt.datum._is_selected == 1), alt.value(1.0), alt.value(0.2)),
                    tooltip=['Regiao_Sintetica_Origem', 'count()']
                ).add_params(click_reg).properties(height=320, title="Volume de Demanda por Região")

                chart_uf = base_chart.mark_arc(innerRadius=60).encode(
                    theta=alt.Theta("count():Q", stack=True),
                    color=alt.Color("UF_Sintetica_Origem:N", legend=alt.Legend(title="Estados (UF)", orient='bottom')),
                    opacity=alt.condition(click_uf & (alt.datum._is_selected == 1), alt.value(1.0), alt.value(0.2)),
                    tooltip=['UF_Sintetica_Origem', 'count()']
                ).add_params(click_uf).properties(height=320, title="Market Share por Estado")

                status_palette = alt.Scale(domain=['Excelente', 'Boa', 'Aceitável', 'Revisar', 'Erro'], range=['#2ECC71', '#3498DB', '#F1C40F', '#E67E22', '#E74C3C'])
                chart_status = base_chart.mark_bar(cornerRadiusEnd=4).encode(
                    x=alt.X('Status da Rota:N', title='Status de Confiança', sort=['Excelente', 'Boa', 'Aceitável', 'Revisar', 'Erro']),
                    y=alt.Y('count():Q', title='Volume'),
                    color=alt.Color('Status da Rota:N', scale=status_palette, legend=None),
                    opacity=alt.condition(click_status & (alt.datum._is_selected == 1), alt.value(1.0), alt.value(0.2)),
                    tooltip=['Status da Rota', 'count()']
                ).add_params(click_status).properties(height=320, title="Monitor de Qualidade Geodésica")

                df_linha = df_cf.groupby('Municipio Origem').agg(
                    Média=('Linha Reta', 'mean'), Mediana=('Linha Reta', 'median'), Minimo=('Linha Reta', 'min'), Maximo=('Linha Reta', 'max'),
                    Desvio_Padrao=('Linha Reta', 'std'), Qtd=('Origem', 'count'), _is_selected=('_is_selected', 'max')
                ).reset_index()
                df_linha['Desvio_Padrao'] = df_linha['Desvio_Padrao'].fillna(0)
                
                chart_lr_mun = alt.Chart(df_linha).mark_line(point=True, color='#10B981').encode(
                    x=alt.X('Municipio Origem:N', title='Município', sort='-y'),
                    y=alt.Y('Média:Q', title='Distância Média (km)'),
                    opacity=alt.condition(click_linha & (alt.datum._is_selected == 1), alt.value(1.0), alt.value(0.2)),
                    tooltip=[alt.Tooltip('Municipio Origem:N'), alt.Tooltip('Média:Q', format='.2f')]
                ).add_params(click_linha).properties(height=320, title="Evolução da Qualidade Geodésica por Município")

                top_muns = df_cf['Municipio Origem'].value_counts().head(15).index.tolist()
                bar_base = base_chart.transform_filter(alt.FieldOneOfPredicate(field='Municipio Origem', oneOf=top_muns))
                bar_mun = bar_base.mark_bar(color='#3B82F6', cornerRadiusEnd=4).encode(
                    x=alt.X('count():Q', title='Volume de Rotas', axis=alt.Axis(tickMinStep=1)),
                    y=alt.Y('Municipio Origem:N', title='Município', sort=alt.EncodingSortField(field='Municipio Origem', op='count', order='descending')),
                    opacity=alt.condition(click_mun & (alt.datum._is_selected == 1), alt.value(1.0), alt.value(0.2)),
                    tooltip=['Municipio Origem', 'count()']
                ).add_params(click_mun)
                text_bar = bar_base.mark_text(align='right', dx=-5, color='white', fontWeight='bold').encode(x=alt.X('count():Q'), y=alt.Y('Municipio Origem:N', sort=alt.EncodingSortField(field='Municipio Origem', op='count', order='descending')), text=alt.Text("count():Q"))
                chart_muns = alt.layer(bar_mun, text_bar).properties(height=350, title="Top 15 Municípios de Despacho Operacional")

                chart_scatter = base_chart.mark_circle(size=80).encode(
                    x=alt.X('Distancia:Q', title='Distância Viária Oficial (km)', scale=alt.Scale(zero=False, nice=True, padding=10)),
                    y=alt.Y('Tempo_Horas:Q', title='Tempo Estimado (Horas)', scale=alt.Scale(zero=False, nice=True, padding=10)),
                    color=alt.Color('Status da Rota:N', scale=status_palette),
                    opacity=alt.condition(brush & click_scatter & (alt.datum._is_selected == 1), alt.value(0.9), alt.value(0.1)),
                    tooltip=['Municipio Origem', 'Origem', 'Destino', 'Distancia', 'Tempo_Horas', 'Status da Rota']
                ).add_params(brush, click_scatter).properties(height=350, title="Matriz de Dispersão e Identificação de Outliers")

                col_p1, col_p2, col_p3 = st.columns(3)
                col_p1.altair_chart(chart_reg, use_container_width=True, on_select="rerun", key="dash_reg")
                col_p2.altair_chart(chart_uf, use_container_width=True, on_select="rerun", key="dash_uf")
                col_p3.altair_chart(chart_status, use_container_width=True, on_select="rerun", key="dash_status")
                st.divider()
                col_p4, col_p5 = st.columns(2)
                col_p4.altair_chart(chart_lr_mun, use_container_width=True, on_select="rerun", key="dash_lr")
                col_p5.altair_chart(chart_muns, use_container_width=True, on_select="rerun", key="dash_mun")
                st.altair_chart(chart_scatter, use_container_width=True, on_select="rerun", key="dash_scatter")

            st.markdown("#### 🗺️ Torre de Controle Espacial (Heatmap Dinâmico)")
            with st.container(border=True):
                col_m1, col_m2 = st.columns([80, 20])
                with col_m2: map_style_selection = st.radio("Tema Topológico:", ["Carto Dark Mode (Padrão)", "OpenStreetMap Clássico", "Satélite (Esri Imagens)"], index=0)
                
                df_mapa = df_cf.copy()
                df_mapa['Lat Destino'] = pd.to_numeric(df_mapa['Lat Destino'], errors='coerce')
                df_mapa['Lon Destino'] = pd.to_numeric(df_mapa['Lon Destino'], errors='coerce')
                df_mapa = df_mapa.dropna(subset=['Lat Destino', 'Lon Destino'])
                df_mapa = df_mapa[(df_mapa['Lat Destino'] != 0.0) & (df_mapa['Lon Destino'] != 0.0)]
                
                if not df_mapa.empty:
                    df_agg = df_mapa.groupby(['Municipio Destino', 'Endereco Oficial Destino', 'UF_Sintetica_Origem', 'Regiao_Sintetica_Origem']).agg(
                        Qtd_Rotas=('Origem', 'count'), Lat_Media=('Lat Destino', 'mean'), Lon_Media=('Lon Destino', 'mean'),
                        Dist_Media=('Distancia', 'mean'), Tempo_Medio=('Tempo_Horas', 'mean'), Score_Medio=('Score Final Global', 'mean')
                    ).reset_index()

                    total_rotas_mapa = df_agg['Qtd_Rotas'].sum()
                    df_agg['Participacao_Nacional_%'] = (df_agg['Qtd_Rotas'] / total_rotas_mapa) * 100
                    
                    estilo_mapbox = "carto-darkmatter"
                    if map_style_selection == "OpenStreetMap Clássico": estilo_mapbox = "open-street-map"
                    if map_style_selection == "Satélite (Esri Imagens)": estilo_mapbox = "white-bg"

                    fig = px.scatter_mapbox(
                        df_agg, lat='Lat_Media', lon='Lon_Media', size='Qtd_Rotas', color='Qtd_Rotas', color_continuous_scale=px.colors.sequential.Blues,
                        size_max=45, zoom=3.5, mapbox_style=estilo_mapbox, hover_name='Municipio Destino',
                        hover_data={'Lat_Media': False, 'Lon_Media': False, 'UF_Sintetica_Origem': True, 'Regiao_Sintetica_Origem': True, 'Qtd_Rotas': True, 'Participacao_Nacional_%': ':.2f', 'Dist_Media': ':.1f', 'Tempo_Medio': ':.1f', 'Score_Medio': False},
                        title="Densidade Operacional da Seleção Ativa"
                    )
                    if map_style_selection == "Satélite (Esri Imagens)": fig.update_layout(mapbox_layers=[{"below": 'traces', "sourcetype": "raster", "sourceattribution": "Esri World Imagery", "source": ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"]}])
                    fig.update_layout(margin={"r":0,"t":40,"l":0,"b":0}, height=600)
                    st.plotly_chart(fig, use_container_width=True)
                else: st.info("O filtro atual não retornou coordenadas válidas no Brasil para plotagem.")

            st.markdown("#### 🥇 Rankings e Extremos Logísticos da Seleção Atual (Top 10)")
            with st.container(border=True):
                tab_dist_max, tab_dist_min, tab_tempo = st.tabs(["Maiores Distâncias (+)", "Menores Distâncias (-)", "Maiores Tempos (Gargalos)"])
                with tab_dist_max: st.dataframe(df_cf.nlargest(10, 'Distancia')[['Origem', 'Destino', 'Distancia', 'Tempo', 'Status da Rota']], use_container_width=True)
                with tab_dist_min: st.dataframe(df_cf.nsmallest(10, 'Distancia')[['Origem', 'Destino', 'Distancia', 'Tempo', 'Status da Rota']], use_container_width=True)
                with tab_tempo: st.dataframe(df_cf.nlargest(10, 'Tempo_Minutos')[['Origem', 'Destino', 'Tempo', 'Distancia', 'Status da Rota']], use_container_width=True)

            st.markdown("#### 📋 Matriz de Dados Drill-Down da Seleção (Data Explorer)")
            with st.container(border=True):
                tabela_h = min(800, max(300, len(df_cf) * 35 + 43))
                st.dataframe(df_cf[['Origem', 'Destino', 'Distancia', 'Linha Reta', 'Tempo', 'Status da Rota', 'Status Linha Reta', 'Link da Rota']], use_container_width=True, height=tabela_h, column_config={"Link da Rota": st.column_config.LinkColumn("🔗 Abrir no Maps")}, hide_index=True)

            st.markdown("#### 🚨 Controle de Qualidade de Dados (Auditoria Geodésica e de Falhas)")
            with st.container(border=True):
                df_suspeitas = df_cf[(df_cf['Score Final Global'] < 70) | (df_cf['Status da Rota'] == "Erro") | (df_cf['Confianca Origem'] == "BAIXA") | ((df_cf['Linha Reta'] <= 0.01) & (df_cf['Origem'] != df_cf['Destino']))]
                if not df_suspeitas.empty:
                    st.warning(f"Atenção: Identificadas {len(df_suspeitas)} rotas requerendo revisão humana dentro do seu recorte atual.")
                    st.dataframe(df_suspeitas[['Origem', 'Destino', 'Linha Reta', 'Status Linha Reta', 'Score Final Global', 'Confianca Origem', 'Motivo Roteamento']], use_container_width=True)
                else: st.success("🎉 Excelente! Nenhuma anomalia geodésica ou operacional encontrada no recorte atual.")

    else:
        st.warning("Aguardando processamento de planilha corporativa na aba de Lotes (⚙️) para ativar e renderizar o Enterprise Data Analytics Engine.")

with tab_calculadora:
    st.info("💡 **Objetivo desta aba:** Uma ferramenta de autoatendimento Analítico (Self-Service BI). Realize extrações, crie tabelas dinâmicas e pivote informações de forma flexível utilizando a base que já passou pela blindagem e filtros globais.")
    col_c_title, col_c_btn = st.columns([80, 20])
    with col_c_title: st.markdown("### 🧮 Calculadora Analítica Corporativa")
    
    if 'df_cf_master' in st.session_state and not st.session_state['df_cf_master'].empty:
        df_base_calc = st.session_state['df_cf_master'].copy()
        
        st.markdown("#### 🎛️ Painel de Filtros da Calculadora (Cascata Local)")
        with st.container(border=True):
            c_f1, c_f2, c_f3, c_f4 = st.columns(4)
            op_regiao = sorted(df_base_calc['Regiao_Sintetica_Origem'].dropna().unique())
            calc_reg = c_f1.multiselect("Região", op_regiao)
            if calc_reg: df_base_calc = df_base_calc[df_base_calc['Regiao_Sintetica_Origem'].isin(calc_reg)]
            op_uf = sorted(df_base_calc['UF_Sintetica_Origem'].dropna().unique())
            calc_uf = c_f2.multiselect("UF", op_uf)
            if calc_uf: df_base_calc = df_base_calc[df_base_calc['UF_Sintetica_Origem'].isin(calc_uf)]
            op_mun = sorted(df_base_calc['Municipio Origem'].dropna().unique())
            calc_mun = c_f3.multiselect("Município Origem", op_mun)
            if calc_mun: df_base_calc = df_base_calc[df_base_calc['Municipio Origem'].isin(calc_mun)]
            op_distrito = sorted(df_base_calc['Distrito Origem'].dropna().unique())
            calc_distrito = c_f4.multiselect("Distrito Origem", op_distrito)
            if calc_distrito: df_base_calc = df_base_calc[df_base_calc['Distrito Origem'].isin(calc_distrito)]

            c_f5, c_f6, c_f7, c_f8 = st.columns(4)
            op_status = sorted(df_base_calc['Status da Rota'].dropna().unique())
            calc_status = c_f5.multiselect("Status da Rota", op_status)
            if calc_status: df_base_calc = df_base_calc[df_base_calc['Status da Rota'].isin(calc_status)]
            op_fonte = sorted(df_base_calc['Fonte Geocoding Origem'].dropna().unique())
            calc_fonte = c_f6.multiselect("Fonte Geocoding", op_fonte)
            if calc_fonte: df_base_calc = df_base_calc[df_base_calc['Fonte Geocoding Origem'].isin(calc_fonte)]
            op_fonte_rota = sorted(df_base_calc['Fonte da Rota'].dropna().unique())
            calc_fonte_rota = c_f7.multiselect("Fonte da Rota", op_fonte_rota)
            if calc_fonte_rota: df_base_calc = df_base_calc[df_base_calc['Fonte da Rota'].isin(calc_fonte_rota)]
            op_balsa = sorted(df_base_calc['Balsas'].dropna().astype(str).unique())
            calc_balsa = c_f8.multiselect("Possui Balsa", op_balsa)
            if calc_balsa: df_base_calc = df_base_calc[df_base_calc['Balsas'].astype(str).isin(calc_balsa)]

        st.markdown("#### ⚙️ Configuração dos Cálculos")
        with st.container(border=True):
            cc1, cc2, cc3 = st.columns([1, 1, 2])
            colunas_disponiveis = df_base_calc.columns.tolist()
            calc_campo = cc1.selectbox("Campo de Análise", colunas_disponiveis, index=colunas_disponiveis.index('Distancia') if 'Distancia' in colunas_disponiveis else 0)
            operacoes = ['Contagem (Count)', 'Contagem Distinta (Count Distinct)', 'Soma (Sum)', 'Média (Average)', 'Mínimo (Min)', 'Máximo (Max)', 'Mediana (Median)', 'Desvio Padrão', 'Variância', 'Percentil 25', 'Percentil 50', 'Percentil 75']
            calc_op = cc2.selectbox("Operação Matemática/Estatística", operacoes, index=3)
            calc_agrup = cc3.multiselect("Agrupar por (Pivot)", colunas_disponiveis, default=['Regiao_Sintetica_Origem'])

        st.markdown("#### 📊 Resultados Analíticos Extraídos")
        if df_base_calc.empty:
            st.warning("O conjunto resultante das filtragens locais (Calculadora) ou globais (Analytics) está vazio.")
        else:
            try:
                fig = None
                if not calc_agrup:
                    if 'Contagem' in calc_op and 'Distinta' not in calc_op: resultado_final = df_base_calc[calc_campo].count()
                    elif 'Contagem Distinta' in calc_op: resultado_final = df_base_calc[calc_campo].nunique()
                    else: resultado_final = df_base_calc[calc_campo].agg(get_agg_func(calc_op))
                    st.metric(f"Resultado Consolidado: {calc_op} de {calc_campo}", round(resultado_final, 2) if isinstance(resultado_final, (float, int)) else resultado_final)
                    df_agg = pd.DataFrame([{"Métrica": f"{calc_op} de {calc_campo}", "Valor": resultado_final}])
                else:
                    df_agg = df_base_calc.groupby(calc_agrup).agg(Resultado_Metrica=(calc_campo, get_agg_func(calc_op))).reset_index()
                    df_agg = df_agg.rename(columns={'Resultado_Metrica': f"{calc_op} de {calc_campo}"})
                    if 'Soma' in calc_op or 'Contagem' in calc_op: df_agg = df_agg.sort_values(by=f"{calc_op} de {calc_campo}", ascending=False)
                    
                    col_r1, col_r2 = st.columns([40, 60])
                    with col_r1: st.dataframe(df_agg, use_container_width=True, hide_index=True)
                    with col_r2:
                        if len(calc_agrup) == 1: fig = px.bar(df_agg, x=calc_agrup[0], y=f"{calc_op} de {calc_campo}", color=calc_agrup[0], title=f"Distribuição de {calc_campo}")
                        elif len(calc_agrup) >= 2: fig = px.bar(df_agg, x=calc_agrup[0], y=f"{calc_op} de {calc_campo}", color=calc_agrup[1], barmode='group', title=f"Análise Multidimensional de {calc_campo}")
                        fig.update_layout(showlegend=True, height=400, margin=dict(l=0, r=0, t=40, b=0))
                        st.plotly_chart(fig, use_container_width=True)

                st.markdown("#### 📥 Exportação Avançada Multi-Abas (Calculadora + Gráficos)")
                
                output_calc = io.BytesIO()
                with pd.ExcelWriter(output_calc, engine='xlsxwriter') as writer:
                    df_resumo = pd.DataFrame([{"Métrica Principal": f"{calc_op} de {calc_campo}", "Total de Linhas Analisadas": len(df_base_calc)}])
                    df_resumo.to_excel(writer, sheet_name='Resumo Executivo', index=False)
                    df_agg.to_excel(writer, sheet_name='Dados Consolidados', index=False)
                    
                    if fig:
                        workbook = writer.book
                        worksheet = workbook.add_worksheet('Gráficos Exportados')
                        try:
                            img_bytes = fig.to_image(format="png", width=900, height=500)
                            worksheet.insert_image('B2', 'grafico.png', {'image_data': io.BytesIO(img_bytes)})
                        except Exception as e:
                            worksheet.write('A1', f"Aviso: O motor de renderização de imagens estáticas (Kaleido) não está ativo neste ambiente. O gráfico interativo não pôde ser convertido para PNG. Detalhes: {str(e)}")

                csv_calc = df_agg.to_csv(index=False).encode('utf-8')
                c_exp1, c_exp2, c_exp3 = st.columns(3)
                c_exp1.download_button("📥 Exportar Relatório Excel Completo (.xlsx)", data=output_calc.getvalue(), file_name="relatorio_calculadora_avancado.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
                c_exp2.download_button("Exportar Tabela Bruta (CSV)", data=csv_calc, file_name="dados_calculadora.csv", mime="text/csv", use_container_width=True)

            except Exception as e:
                st.error(f"⚠️ Impossível realizar o cálculo solicitado. A operação estatística '{calc_op}' falhou. Verifique se o campo '{calc_campo}' contém números válidos. Erro: {e}")
    else:
        st.warning("Os dados ainda não foram processados ou o filtro global está muito restrito. Processe um lote na Aba 'Processamento em Lote'.")

with tab_classificacao:
    st.info("💡 **Objetivo desta aba:** Segmentar a volumetria logística por município, criar faixas personalizadas e rotular os polos de distribuição. Utilize o Editor de Faixas abaixo para configurar os limites, divisores operacionais e níveis críticos.")
    
    st.markdown("### 🚨 Classificação Territorial de Ocorrências Municipais")
    
    if 'df_cf_master' in st.session_state and not st.session_state['df_cf_master'].empty:
        df_base_class = st.session_state['df_cf_master'].copy()
        
        st.markdown("#### ⚙️ Parâmetro Base de Classificação")
        metrica_classificacao = st.radio(
            "Selecione a métrica que definirá as faixas territoriais:",
            ["Distância Total (km)", "Distância Média (km)", "Ocorrências (Volume)"],
            index=0,
            horizontal=True,
            help="A métrica selecionada será utilizada para enquadrar os municípios nas faixas configuradas abaixo."
        )
        
        col_metrica = "Distância_Total" if metrica_classificacao == "Distância Total (km)" else "Distância_Media" if metrica_classificacao == "Distância Média (km)" else "Ocorrências"

        st.markdown("#### 1️⃣ Editor Dinâmico de Faixas e Divisores")
        st.caption(f"Configure os limites Mínimos e Máximos considerando a métrica base escolhida: **{metrica_classificacao}**.")
        
        if 'class_bins' not in st.session_state:
            st.session_state['class_bins'] = pd.DataFrame([
                {"Min": 1, "Max": 500, "Divisor": 500, "Rótulo": "🟢 Operação Normal", "Cor": "#2ECC71"},
                {"Min": 501, "Max": 2000, "Divisor": 2000, "Rótulo": "🟠 Alerta Laranja", "Cor": "#F39C12"},
                {"Min": 2001, "Max": 999999, "Divisor": 5000, "Rótulo": "🔴 Volume Crítico", "Cor": "#E74C3C"}
            ])
            
        edited_bins = st.data_editor(st.session_state['class_bins'], num_rows="dynamic", use_container_width=True, hide_index=True)
        
        with st.spinner("Reagrupando e Classificando Malha Territorial..."):
            df_base_class['Lat Origem'] = pd.to_numeric(df_base_class['Lat Origem'], errors='coerce')
            df_base_class['Lon Origem'] = pd.to_numeric(df_base_class['Lon Origem'], errors='coerce')
            df_base_class['Distancia'] = pd.to_numeric(df_base_class['Distancia'], errors='coerce').fillna(0)
            
            df_agg_class = df_base_class.groupby(['Municipio Origem', 'UF_Sintetica_Origem', 'Regiao_Sintetica_Origem']).agg(
                Ocorrências=('Origem', 'count'),
                Distância_Total=('Distancia', 'sum'),
                Distância_Media=('Distancia', 'mean'),
                Lat_Media=('Lat Origem', 'mean'),
                Lon_Media=('Lon Origem', 'mean')
            ).reset_index()
            
            df_agg_class = df_agg_class[df_agg_class['Municipio Origem'] != "Não Identificado"]
            
            def classificar_ocorrencia(valor):
                for _, row in edited_bins.iterrows():
                    try:
                        vmin, vmax = float(row['Min']), float(row['Max'])
                        if vmin <= valor <= vmax:
                            divisor = float(row['Divisor']) if row['Divisor'] > 0 else 1
                            pct = round((valor / divisor) * 100, 2)
                            return row['Rótulo'], pct, row['Cor']
                    except: pass
                return "⚪ Não Classificado", 0.0, "#95A5A6"
                
            resultados_clas = df_agg_class[col_metrica].apply(classificar_ocorrencia)
            df_agg_class['Rótulo'] = [r[0] for r in resultados_clas]
            df_agg_class['Percentual (%)'] = [r[1] for r in resultados_clas]
            df_agg_class['Cor Hex'] = [r[2] for r in resultados_clas]
            
            df_agg_class = df_agg_class.sort_values(by=col_metrica, ascending=False)
            
            st.markdown("#### 2️⃣ Indicadores e Extremos da Malha")
            cc_k1, cc_k2, cc_k3, cc_k4 = st.columns(4)
            cc_k1.metric("Municípios Analisados", df_agg_class.shape[0])
            valor_total_metrica = df_agg_class[col_metrica].sum() if col_metrica != "Distância_Media" else df_agg_class[col_metrica].mean()
            cc_k2.metric(f"Total: {metrica_classificacao}", round(valor_total_metrica, 1))
            cc_k3.metric("Percentual Médio Aplicado", f"{round(df_agg_class['Percentual (%)'].mean(), 1)}%")
            if not df_agg_class.empty:
                m_critico = df_agg_class.iloc[0]['Municipio Origem']
                v_critico = round(df_agg_class.iloc[0][col_metrica], 1)
                cc_k4.metric("Polo Mais Crítico", f"{m_critico} ({v_critico})")
            
            st.markdown("#### 3️⃣ Ecossistema Visual Temático")
            
            map_colors = dict(zip(df_agg_class['Rótulo'], df_agg_class['Cor Hex']))
            
            t_col1, t_col2 = st.columns([60, 40])
            with t_col1:
                fig_bar_clas = px.bar(df_agg_class.head(20), x='Municipio Origem', y=col_metrica, color='Rótulo', color_discrete_map=map_colors, title=f"Top 20 Cidades por {metrica_classificacao}", text='Percentual (%)')
                fig_bar_clas.update_traces(texttemplate='%{text}%', textposition='outside')
                st.plotly_chart(fig_bar_clas, use_container_width=True)
            with t_col2:
                fig_pie_clas = px.pie(df_agg_class, names='Rótulo', values=col_metrica, color='Rótulo', color_discrete_map=map_colors, hole=0.4, title="Distribuição por Nível Crítico")
                st.plotly_chart(fig_pie_clas, use_container_width=True)
                
            fig_tree = px.treemap(df_agg_class, path=[px.Constant("Brasil"), 'Regiao_Sintetica_Origem', 'UF_Sintetica_Origem', 'Municipio Origem'], values=col_metrica, color='Rótulo', color_discrete_map=map_colors, title="Volumetria Hierárquica por Rótulo Territorial")
            st.plotly_chart(fig_tree, use_container_width=True)

            df_mapa_clas = df_agg_class.dropna(subset=['Lat_Media', 'Lon_Media'])
            df_mapa_clas = df_mapa_clas[(df_mapa_clas['Lat_Media'] != 0.0) & (df_mapa_clas['Lon_Media'] != 0.0)]
            if not df_mapa_clas.empty:
                fig_mapa_clas = px.scatter_mapbox(
                    df_mapa_clas, lat='Lat_Media', lon='Lon_Media', size=col_metrica, color='Rótulo', color_discrete_map=map_colors,
                    size_max=35, zoom=3.5, mapbox_style="carto-darkmatter", hover_name='Municipio Origem',
                    hover_data={'Lat_Media': False, 'Lon_Media': False, 'UF_Sintetica_Origem': True, col_metrica: True, 'Percentual (%)': True, 'Rótulo': False},
                    title="Mapeamento Temático Pós-Classificação"
                )
                fig_mapa_clas.update_layout(margin={"r":0,"t":40,"l":0,"b":0}, height=550)
                st.plotly_chart(fig_mapa_clas, use_container_width=True)

            st.markdown("#### 4️⃣ Tabela Mestre e Exportação Direta")
            st.dataframe(df_agg_class.drop(columns=['Lat_Media', 'Lon_Media', 'Cor Hex']), use_container_width=True, hide_index=True)
            
            out_class = io.BytesIO()
            with pd.ExcelWriter(out_class, engine='xlsxwriter') as writer:
                df_agg_class.drop(columns=['Lat_Media', 'Lon_Media', 'Cor Hex']).to_excel(writer, sheet_name='Ocorrencias e Classificacao', index=False)
            st.download_button("📥 Baixar Tabela de Classificação (.xlsx)", data=out_class.getvalue(), file_name="classificacao_territorial_ocorrencias.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            
    else:
        st.warning("O conjunto de dados base global está vazio. Por favor, processe seu Lote para alimentar este módulo espacial.")

with tab_enciclopedia:
    st.info("💡 **Objetivo desta aba:** Servir como o repositório mestre de conhecimento. Esta enciclopédia detalha toda a jornada técnica de um dado dentro do aplicativo, abordando 100% das funcionalidades corporativas, desde a limpeza gramatical até a validação geométrica extrema anti-colisão.")
    st.markdown("# 📚 Enciclopédia Operacional e Base de Conhecimento Core")
    
    with st.expander("1. Visão Geral do Sistema", expanded=True):
        st.markdown("""
        **O que é o sistema?**
        O *Motor Nacional de Roteirização Inteligente* é uma plataforma corporativa B2B projetada para processar, em escala industrial, a conversão de endereços de texto livre em rotas matemáticas roteirizáveis.
        
        **Qual problema ele resolve?**
        Elimina a dependência de APIs logísticas frágeis (como o Google isolado, que pode falhar em áreas rurais), superando problemas de *falsos positivos topológicos*, onde endereços incompletos são jogados aleatoriamente no centro do estado ou do país.
        
        **Público-Alvo e Benefícios:**
        * **Operadores de Logística:** Descobrem o tempo viário oficial, pedágios virtuais e alocação de caminhões.
        * **Auditores de Frete:** Utilizam a plataforma para desmascarar cobranças de distância indevidas, comparando o asfalto com a linha reta geodésica.
        * **Analistas de Dados:** Aproveitam o Enterprise Analytics para mapas de calor, clusters de entrega e estatísticas robustas cruzadas por região.
        """)
        
    with st.expander("2. Arquitetura Geral e Fluxo de Dados"):
        st.markdown("""
        O sistema opera através de um funil hierárquico extremamente estrito e escalável via *ThreadPoolExecutor*:
        
```text
        [ ENTRADA DE DADOS ] → (Usuário insere Lote Excel ou Single-Shot)
                 ↓
        [ PARSER LEXICAL ] → (Normalização semântica, limpeza de acentos e extração de CEP)
                 ↓
        [ CACHE CHECK ] → (Intercepção instantânea de rotas já processadas)
                 ↓
        [ GEOCODIFICAÇÃO MULTIMOTOR ] → (Busca paralela no ArcGIS, Nominatim, TomTom e Photon)
                 ↓
        [ BARREIRA ANTI-COLISÃO ] → (Se Ponto A == Ponto B, força modo estrito nas APIs)
                 ↓
        [ CÁLCULO DA LINHA RETA ] → (Árbitro Supremo: WGS-84 ou Haversine)
                 ↓
        [ ROTEIRIZAÇÃO ASFÁLTICA ] → (Google Maps Scraper Engine ou OSRM Open-Source)
                 ↓
        [ SCORE XAI E AUDITORIA ] → (Cálculo de penalidades e confiança baseada em Bayes)
                 ↓
        [ ANALYTICS & EXPORT ] → (Geração de Heatmaps, Tabelas Dinâmicas e Relatórios O(U))
        ```
        """)

    with st.expander("3. Bases de Dados Utilizadas"):
        st.markdown("""
        * **IBGE (Instituto Brasileiro de Geografia e Estatística):** Atua como malha central offline do motor. O sistema baixa e consome o centróide exato de todas as 5.570 cidades e distritos do Brasil. Permite o modo de sobrevivência offline caso a internet corporativa falhe.
        * **OpenStreetMap (OSM):** O maior banco de dados aberto espacial do planeta. Fundamental para estradas de terra e interior do Brasil, servindo dados para o Nominatim, Photon e OSRM.
        * **CNEFE / Base Local:** Dicionário estrutural acoplável (opcional) mantido no cache, permitindo obediência absoluta a regras locais de filiais.
        """)

    with st.expander("4. APIs Utilizadas"):
        st.markdown("""
        **🌐 Geocodificação (Texto para Lat/Lon)**
        * **ArcGIS (ESRI):** Principal motor B2B predial. Padrão-ouro em conversão de ruas com alta fidelidade na numeração corporativa.
        * **Nominatim (OpenStreetMap):** Busca minuciosa. Confiabilidade máxima para áreas rurais, lotes distantes e referências geográficas indiretas.
        * **Photon (Komoot):** Auxiliar de alta velocidade. Atua sob o OSM para fechar o triângulo do Ensemble.
        * **TomTom Logistics:** Foco na malha viária pesada e rotas de caminhões.
        
        **🗺️ Roteirização (Traçado Viário)**
        * **Google Directions Engine:** Principal provedor de asfalto, tempo e distância.
        * **OSRM (Open Source Routing Machine):** Servidor matemático independente de fallback. Se o Google falhar por limite de requisições, o OSRM garante que a esteira continue processando as rotas matemáticas sem interrupções.
        
        **🔎 Auditoria e Cascatas**
        * **BrasilAPI, ViaCEP e OpenCEP:** Formam a "Cascata Postal-Tripla" para garantir a quebra estrutural e reversa do CEP da operação, mitigando falhas na rede.
        """)

    with st.expander("5. Motor de Geocodificação (Como o endereço é compreendido?)"):
        st.markdown("""
        1. **Classificação Fuzzy:** O texto passa por um classificador com a biblioteca `RapidFuzz`, que entende a tipologia: É CEP? É Condomínio? É Área Rural?
        2. **Disparo Simultâneo:** O motor atira a string normalizada para 5 provedores na nuvem ao mesmo tempo.
        3. **Consenso Espacial (DBSCAN):** Com as 5 respostas de coordenadas, o algoritmo de *Machine Learning* agrupa quem caiu perto de quem. Pontos discrepantes (outliers) são removidos.
        4. **Score de Confiança:** Calcula a penalidade multiplicando fatores. Ex: Falta de número tira 5 pontos. O motor reverso acusou estado errado tira 50 pontos.
        """)

    with st.expander("6. Motor de Roteirização (Traçado Logístico)"):
        st.markdown("""
        O sistema primeiro exige ter a Latitude/Longitude Exata de Origem e Destino.
        A partir delas, consulta o banco viário para conectar as ruas.
        * **Tempo Estimado:** O Google traz em tempo real. No fallback (OSRM), aplica-se a matriz matemática de velocidade comercial da frota (45 km/h urbano, 65 km/h rodoviário).
        * **Falhas Topológicas:** Se o traçado por asfalto é absurdamente longo ou impossível (ilha, área isolada), a plataforma adota o Fallback Geodésico, entregando o valor em Linha Reta x 1.45 (fator de correção).
        """)

    with st.expander("7. Distância em Linha Reta (A Matemática do Árbitro)"):
        st.markdown("""
        A distância em linha reta atua como o juiz do motor. É a menor distância curva possível sobre a superfície terrestre.
        
        **Fórmulas Utilizadas:**
        * **WGS-84 (GeographicLib):** Calcula considerando o achatamento polar da Terra elipsoidal. Erro quase zero.
        * **Haversine (Contingência):** Assume a Terra como uma esfera perfeita (Raio = 6371 km).
        
        As fórmulas internas trigonométricas implementadas para fallback (Haversine):
        $$ a = \sin^2\left(\frac{\Delta\phi}{2}\right) + \cos(\phi_1)\cos(\phi_2)\sin^2\left(\frac{\Delta\lambda}{2}\right) $$
        $$ c = 2 \cdot \text{atan2}\left(\sqrt{a}, \sqrt{1-a}\right) $$
        $$ d = 6371 \cdot c $$
        
        **Como é auditada e para que serve?**
        Se o caminhão rodou 200km e a linha reta é 10km, existe fraude, estrangulamento viário ou erro na API. A linha reta serve como base indestrutível para detectar anomalias do Google Maps.
        """)

    with st.expander("8. Sistema de Auditoria Interna"):
        st.markdown("""
        Todo o processo da nuvem é gravado na "Caixa Preta".
        * **Score Global (0 a 100):** Composto por: `35% Confiança Origem + 35% Confiança Destino + 30% Qualidade de Roteamento (Asfalto x Linha Reta)`.
        * **XAI (Explicabilidade):** A auditoria registra em texto exato *o porquê* de o motor ter feito a escolha. Você lerá algo como: "Correspondência administrativa confirmada via Ensemble ArcGIS + TomTom".
        """)

    with st.expander("9. Sistema de Cache Corporativo"):
        st.markdown("""
        O sistema é dotado de inteligência `diskcache`.
        * Se você subir 5.000 clientes e metade já foi calculada ontem, o sistema bate no banco SQLITE embarcado em milissegundos.
        * **Unpoisoning Automático:** Se por ventura uma Linha Reta falhou no passado armazenando "0", a arquitetura identifica, desfaz o cache e reprocessa na hora.
        """)

    with st.expander("10. Analytics Corporativo"):
        st.markdown("""
        O Enterprise Analytics consolida todos os retornos. 
        Possui filtragem bidirecional estilo Power BI: Clicar num estado de um Gráfico de Rosca reduz todos os mapas de calor, scatter plots e cálculos de tempo apenas para a volumetria daquele estado, cruzando KPIs de Distância e Tempo.
        """)

    with st.expander("11. Segurança e Confiabilidade"):
        st.markdown("""
        * **Failover Multi-Level:** Timeout no ArcGIS? Pula pro OSM. Timeout no OSM? Bate na Base Local IBGE. Timeout no Google Routing? Pula pro OSRM. Timeout no OSRM? Retorna a Projeção Matemática da Linha Reta.
        * O sistema foi arquitetado para nunca travar as execuções em lote, registrando os erros graciosamente nos Logs e marcando a linha do Excel afetada como "Erro Operacional", para prosseguir com os milhares de outros cálculos da fila sem paralisação.
        """)

with tab_manual:
    st.info("💡 **Bem-vindo ao Manual Operacional!** Este espaço é destinado a todos os usuários da plataforma, ensinando de forma prática o 'passo a passo' para executar as operações do dia a dia.")
    
    st.markdown("### 📘 Manual do Usuário e Treinamento")

    with st.expander("1. Primeiro Acesso e Navegação", expanded=True):
        st.markdown("""
        Ao entrar na plataforma, você verá um **Menu Lateral (Sidebar)** e **Abas Superiores**.
        * **Menu Lateral:** Contém informações estáticas e o contato de suporte (Ticket de Manutenção).
        * **Abas Superiores:** São os "módulos" do sistema. É ali que a mágica acontece. Você clica numa aba (Ex: ⚙️ Processamento Lote) e a tela muda apenas para essa função.
        """)

    with st.expander("2. Processamento de Rota Individual (Testes Rápidos)"):
        st.markdown("""
        **Quando usar?** Você quer saber a distância de um galpão específico até um cliente sem subir planilhas.
        **Passo a passo:**
        1. Clique na aba **📍 Geocodificação**.
        2. No campo **Origem**, digite o endereço completo ou coordenada (Ex: *Rua Teste, 100, São Paulo, SP*).
        3. No campo **Destino**, digite o final da viagem.
        4. Clique em **🚀 Calcular Rota Individual**.
        5. **Resultado:** O painel exibirá as caixas (Cards) contendo a Distância de Asfalto, a Distância Aérea, e se usa balsas. Abaixo, clique no card de 'Auditoria Detalhada' para ler o log gerado pelo robô.
        """)

    with st.expander("3. Processamento em Lote (Milhares de Rotas simultâneas)"):
        st.markdown("""
        **Quando usar?** Você tem o faturamento do mês num Excel com milhares de entregas e quer a quilometragem oficial de todas.
        **Passo a passo:**
        1. Crie uma planilha em Excel (formato `.xlsx`). Ela **obrigatoriamente** precisa ter uma coluna chamada `Origem` e uma coluna chamada `Destino`.
        2. Entre na aba **⚙️ Processamento Lote**.
        3. Arraste e solte o arquivo no bloco pontilhado central.
        4. (Opcional) Digite sua matrícula para auditoria no campo de Operador.
        5. Clique em **Iniciar Processamento em Lote**.
        6. **Resultado:** Uma barra de progresso encherá rapidamente. No final balões sobem à tela e um botão azul **📥 Baixar Planilha (.xlsx)** aparecerá. Ao abrir seu novo Excel, as distâncias e as auditorias estarão preenchidas!
        """)

    with st.expander("4. Alocação de Hubs (Descobrir o Centro de Distribuição mais próximo)"):
        st.markdown("""
        **Quando usar?** Você tem 5 Filiais e 10.000 Clientes. Você não sabe de qual filial a mercadoria de cada cliente deve sair para economizar frete.
        **Passo a passo:**
        1. Vá na aba **📦 Alocação de Hubs**.
        2. Suba o arquivo 1 (Seus Clientes / Entregas).
        3. Suba o arquivo 2 (A lista com as suas Filiais / Hubs).
        4. Embaixo, escolha nas caixas de seleção o nome da coluna de origem (no Excel 1) e o nome da coluna das filiais (no Excel 2).
        5. Clique em **🗺️ Processar Cruzamento Espacial**.
        6. O sistema cruzará cada cliente contra todas as filiais na matemática. Depois, fará o duelo viário no asfalto e te devolverá um arquivo em Excel apontando exatamente a qual Centro o Cliente pertence.
        """)

    with st.expander("5. Calculadora Analítica"):
        st.markdown("""
        **Quando usar?** Você processou um Lote gigantesco e quer "tirar relatórios" na própria tela sem precisar abrir o Excel (Ex: Somar distâncias por Estado).
        **Passo a passo:**
        1. Após ter processado um lote, vá na aba **🧮 Calculadora Analítica**.
        2. No painel de configuração, escolha o **Campo** (ex: `Distancia`).
        3. Escolha a **Operação** (Ex: `Soma (Sum)` ou `Média (Average)`).
        4. Escolha **Agrupar por** (Ex: `Regiao_Sintetica_Origem` ou `Status da Rota`).
        5. O gráfico e a tabela serão montados instantaneamente com a soma calculada. Você pode baixar em PDF/Excel a tabela que gerou.
        """)

    with st.expander("6. Classificação Territorial"):
        st.markdown("""
        **Quando usar?** Você quer agrupar municípios em faixas de "Tabela de Frete" (Ex: Cidades Críticas, Cidades Normais).
        **Passo a passo:**
        1. Entre na aba **🚨 Classificação Territorial**.
        2. Escolha se as faixas serão baseadas em "Distância" ou "Volume de Rotas".
        3. Você verá uma tabela editável na tela. Pode apagar, adicionar linhas e mudar as cores/rótulos (Ex: de `1` a `500` km = Verde, de `501` para frente = Vermelho).
        4. O sistema processará imediatamente o mapa de calor com as novas regras e te dará um botão para baixar a tabela mestre de segmentação.
        """)

    with st.expander("7. Enterprise Analytics (Dashboards)"):
        st.markdown("""
        **Quando usar?** Módulo estilo Power BI para analisar a saúde logística geral e apresentar resultados em reuniões.
        **Passo a passo:**
        1. Acesse a aba **📊 Enterprise Analytics**.
        2. Todos os gráficos (Pizza, Barras, Linha, Mapa e Bolhas) são interativos.
        3. **Como Filtrar:** Basta clicar na fatia do estado "SP" no gráfico de Pizza. Todos os outros gráficos (Mapa, Indicadores) vão mudar na hora para mostrar os dados exclusivos de São Paulo.
        4. Para voltar, clique em um espaço branco do gráfico ou no botão "🧹 Limpar Todos os Filtros" no topo da página.
        """)

    with st.expander("8. Filtros Avançados"):
        st.markdown("""
        Além dos cliques nos gráficos, a aba Analytics possui caixas brancas expansíveis chamadas **"🎛️ Painel de Controle de Filtros Avançados"**.
        Nelas você pode selecionar explicitamente Regiões, Cidades, ou arrastar a barra de distância (Slider) para forçar o dashboard a te mostrar apenas viagens entre `1.000` km e `2.000` km. A resposta é instantânea e bidirecional.
        """)

    with st.expander("9. Monitoramento de APIs"):
        st.markdown("""
        **Quando usar?** O sistema está demorando e você quer ver se o Google ou o servidor caíram.
        **Passo a passo:**
        1. Acesse a aba **🔌 Monitor APIs**.
        2. A tabela informará se a Latência e os Erros (Falhas de Rede) estão normais. O indicador 🟢 significa que o fornecedor em nuvem está operando bem. O 🔴 avisa de quedas, indicando que o sistema começou a utilizar os "Fallbacks de Segurança" automaticamente.
        """)

    with st.expander("10. Auditoria"):
        st.markdown("""
        **Quando usar?** Você suspeita que o motor colocou um cliente na cidade errada.
        **Passo a passo:**
        1. Vá até a aba **🕵️ Auditoria**.
        2. A tabela gigante na tela detalha o "Dossiê Investigativo". Pesquise pela sua rua ali. A coluna de "XAI Explicabilidade" mostrará exatamente a dedução lógica e cruzamento de APIs que o servidor usou.
        """)

    with st.expander("11. Exportações (Excel, CSV e Relatórios)"):
        st.markdown("""
        Todo o sistema foi criado para exportar fácil. 
        * Nas abas de Lote/Alocação, procure os botões retangulares azuis ou brancos como `📥 Baixar Planilha (.xlsx)`.
        * Na aba "Calculadora Analítica", existem opções de CSV e a "Exportação Multi-Abas" que embute o gráfico visual dentro da sua planilha de Excel corporativa pronta para a chefia.
        """)

    with st.expander("12. Perguntas Frequentes (FAQ Corporativo)"):
        st.markdown("""
        * **Por que uma rota retornou `0 km` ou `Input Inválido`?**
        Provavelmente a célula original no seu Excel estava vazia, ou você escreveu lixo indecifrável (ex: `%$#¨#`).
        * **O que significa o Score de Confiança?**
        Um número de 0 a 100 indicando a precisão da geocodificação. Acima de 80, a mercadoria chega na porta. Abaixo de 50, o endereço caiu apenas genericamente na cidade.
        * **O que significa a `Distância Linha Reta`?**
        É o voo de um pássaro entre o Ponto A e B ignorando ruas. É essencial para você não cair no golpe do frete "asfáltico" cobrado em rotas com desvios artificiais.
        * **Como identifico uso de balsa?**
        A coluna `Balsas` no Excel exportado sairá marcada como `Sim` se os radares aquáticos do OSRM/Google detectarem travessia obrigatória.
        * **Meus gráficos sumiram na aba Analytics. O que fazer?**
        Provavelmente seus filtros deixaram a base vazia (Ex: Filtrar Nordeste, e depois cruzar pedindo estado SP). Vá no topo da página e clique em **🧹 Limpar Todos os Filtros**.
        """)

with tab_motores:
    st.info("💡 **Objetivo desta aba:** Monitorar a saúde técnica do ecossistema e o Uptime (SLA) de cada parceiro. Visualize quais APIs em nuvem responderam melhor, identifique instabilidades (timeouts), observe os tempos médios de resposta e verifique a integridade algorítmica do último lote.")
    st.markdown("### 🔌 Painel de Monitoramento de Infraestrutura (APIs Health Check)")
    
    if 'df_processado' in st.session_state:
        df_kpi = st.session_state['df_processado'].copy()
        
        with st.container(border=True):
            col_p1, col_p2, col_p3 = st.columns(3)
            col_p1.metric("Tempo Médio Geocoding (Rede Externa)", f"{round(df_kpi['Tempo Geocoding (s)'].mean(), 2)} s")
            col_p2.metric("Tempo Médio Roteamento (Google/OSRM)", f"{round(df_kpi['Tempo Roteamento (s)'].mean(), 2)} s")
            col_p3.metric("Overhead Global Total / Rota", f"{round(df_kpi['Tempo Total (s)'].mean(), 2)} s")
        
        col_m1, col_m2 = st.columns(2)
        
        with col_m1:
            st.caption("**Volume de Requisições de Resolução por Motor (Market Share Base)**")
            grafico_apis = alt.Chart(df_kpi).mark_arc(innerRadius=60).encode(
                theta=alt.Theta(field="Fonte Geocoding Origem", aggregate="count"),
                color=alt.Color(field="Fonte Geocoding Origem", type="nominal", legend=alt.Legend(title="Motores", orient='bottom')),
                tooltip=['Fonte Geocoding Origem', 'count()']
            ).properties(height=350)
            st.altair_chart(grafico_apis, use_container_width=True)
            
        with col_m2:
            st.caption("**Distribuição Qualitativa: Status Bayesiano Pós-Processamento**")
            status_palette_bar = alt.Scale(domain=['Excelente', 'Boa', 'Aceitável', 'Revisar', 'Erro'], range=['#2ECC71', '#3498DB', '#F1C40F', '#E67E22', '#E74C3C'])
            grafico_status = alt.Chart(df_kpi).mark_bar().encode(
                x=alt.X('Status da Rota:N', title='Classificação de Confiança e Exatidão'),
                y=alt.Y('count():Q', title='Volume de Requisições'),
                color=alt.Color('Status da Rota:N', scale=status_palette_bar, legend=None),
                tooltip=['Status da Rota', 'count()']
            ).properties(height=350)
            st.altair_chart(grafico_status, use_container_width=True)
            
    st.markdown("---")
    st.markdown("#### 📡 Tabela Mestre de SLA e Latência em Tempo Real")
    health_data = []
    for api in ["GOOGLE_MAPS", "ARCGIS", "TOMTOM", "NOMINATIM", "PHOTON", "OVERPASS", "OSRM"]:
        dados = cache_api_health.get(api, {"hits": 0, "calls": 0, "falhas": 0, "tempo_total": 0.0})
        t_med = f"{round((dados['tempo_total'] / max(1, dados['calls'])) * 1000)} ms" if dados['calls'] > 0 else "N/A"
        tx_err = f"{round((dados['falhas'] / max(1, dados['calls'] + dados['falhas'])) * 100, 1)}%" if dados['calls'] > 0 else "0.0%"
        health_data.append({"Provedor/Cloud Oficial": api, "Status da Conexão": "🟢 Estável/Online" if dados["falhas"] == 0 else "🔴 Instável/Erros Detectados", "Latência Média Observada": t_med, "Taxa de Falha Sistêmica": tx_err, "Total de Pings Realizados": dados["calls"]})
    
    st.dataframe(pd.DataFrame(health_data), use_container_width=True)

    st.markdown("#### 🌐 Auditoria do Motor Geodésico Contínuo (Métricas de Integridade Matemática)")
    df_metricas_lr = pd.DataFrame([METRICAS_DISTANCIA])
    df_metricas_lr.columns = ["Total de Cálculos de Linha Reta", "Sucesso: GeographicLib (WGS84)", "Sucesso: Geopy", "Fallback: Haversine", "Correções Automáticas (Anti-Zero)", "Falhas Críticas", "Rotas Unpoisoned (Cache Reparado)", "Barreiras Territoriais (Bounding Box)", "Desambiguações Topológicas (Anti-Colisão)"]
    st.dataframe(df_metricas_lr, use_container_width=True)

with tab_auditoria:
    st.info("💡 **Objetivo desta aba:** Transparência Total e Explicabilidade (XAI). Funciona como uma 'Caixa Preta' aberta do sistema. Verifique em detalhes qual algoritmo tomou a decisão para cada coordenada e por que ele escolheu descartar outras opções em caso de empate de proximidade.")
    st.markdown("### 🕵️ Dossiê Investigativo de Auditoria Viária e Espacial")
    
    tab_aud_lote, tab_aud_hub = st.tabs(["⚙️ Logs do Lote de Roteamento Padrão", "📦 Logs do Motor de Alocação (Hubs Competitive)"])
    
    with tab_aud_lote:
        if 'logs_auditoria' in st.session_state and st.session_state['logs_auditoria']:
            st.write("Abaixo consta a árvore de decisões algorítmicas explicáveis tomada pelo motor durante o cálculo do Lote:")
            st.dataframe(pd.DataFrame(st.session_state['logs_auditoria']), use_container_width=True)
        else:
            st.info("Nenhum registro de auditoria em memória cache. Processe uma nova planilha corporativa na aba de Processamento em Lote (⚙️) para gerar o relatório XAI.")
            
    with tab_aud_hub:
        if 'logs_auditoria_alocacao' in st.session_state and st.session_state['logs_auditoria_alocacao']:
            st.write("Abaixo constam as inferências espaciais estritas feitas individualmente para cada Base (Destino) e Endereço (Origem) na leitura e mapeamento da Matriz Geográfica:")
            st.dataframe(pd.DataFrame(st.session_state['logs_auditoria_alocacao']), use_container_width=True)
        else:
            st.info("Nenhuma árvore de decisão persistida. Processe o cálculo de matrizes matemáticas na aba de Alocação de Hubs (📦) para carregar as justificativas competitivas.")
