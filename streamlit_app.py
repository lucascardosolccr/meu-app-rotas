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
import logging
import threading
from unidecode import unidecode
from rapidfuzz import process, fuzz
from diskcache import Cache
from sklearn.cluster import DBSCAN
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# [UI/CONFIG] CONFIGURAÇÃO DE AMBIENTE CORPORATIVO E LOGGING
# ==============================================================================
st.set_page_config(page_title="Gerenciador de Rotas Inteligentes", page_icon="🚗", layout="wide")

logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')

TOMTOM_API_KEY = "" # Insira sua credencial TomTom Logistics aqui

# ==============================================================================
# [CACHE/PERSISTENCE] GERENCIAMENTO RÍGIDO THREAD-SAFE (WAL MODE) E TTL DINÂMICO
# ==============================================================================
cache_kwargs = {'sqlite_journal_mode': 'wal', 'sqlite_timeout': 20}

cache_classificacao = Cache("./cache_classificacao", **cache_kwargs)
cache_fuzzy = Cache("./cache_fuzzy", **cache_kwargs)
cache_geo = Cache("./cache_geo", **cache_kwargs)
cache_rotas = Cache("./cache_rotas", **cache_kwargs)
cache_poi = Cache("./cache_poi", **cache_kwargs)
cache_cep = Cache("./cache_cep", **cache_kwargs)
cache_google = Cache("./cache_google", **cache_kwargs)
cache_reverse = Cache("./cache_reverse", **cache_kwargs)
cache_base_local = Cache("./cache_base_local", **cache_kwargs)
cache_aprendizado = Cache("./cache_aprendizado", **cache_kwargs)
cache_aprendizado_auto = Cache("./cache_aprendizado_auto", **cache_kwargs)
cache_api_health = Cache("./cache_api_health", **cache_kwargs)
cache_historico_lotes = Cache("./cache_historico_lotes", **cache_kwargs)

TTL_CEP = 90 * 86400
TTL_ENDERECO = 30 * 86400
TTL_POI = 7 * 86400
TTL_ROTA = 3 * 86400

for c in [cache_classificacao, cache_fuzzy, cache_geo, cache_rotas, cache_poi, cache_cep, cache_google, cache_reverse, cache_base_local, cache_aprendizado, cache_aprendizado_auto, cache_api_health, cache_historico_lotes]:
    c.cull()

def realizar_manutencao_logs_google():
    diretorio_logs = "logs_google"
    os.makedirs(diretorio_logs, exist_ok=True)
    limite_tempo = time.time() - TTL_ROTA
    try:
        for arquivo in os.listdir(diretorio_logs):
            caminho_completo = os.path.join(diretorio_logs, arquivo)
            if os.path.isfile(caminho_completo) and os.path.getmtime(caminho_completo) < limite_tempo:
                os.remove(caminho_completo)
    except Exception as e: logging.error(f"Erro na manutenção de logs: {e}")

realizar_manutencao_logs_google()

session = requests.Session()
retry_strategy = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)

# ==============================================================================
# [SERVICES/INFRA] INFRAESTRUTURA DE CONCORRÊNCIA E CONTROLE DE RATE LIMITS
# ==============================================================================
WORKERS_DISPONIVEIS = 8

if "executor_global" not in st.session_state:
    st.session_state["executor_global"] = ThreadPoolExecutor(max_workers=WORKERS_DISPONIVEIS)

if "executor_apis" not in st.session_state:
    st.session_state["executor_apis"] = ThreadPoolExecutor(max_workers=16)

# Semáforos para proteção de APIs Comunitárias e Governamentais
SEMAPHORES = {
    "NOMINATIM": threading.Semaphore(1),  # Estrito: 1 por vez
    "PHOTON": threading.Semaphore(2),
    "OVERPASS": threading.Semaphore(2),
    "BRASIL_API": threading.Semaphore(5),
    "DEFAULT": threading.Semaphore(10)
}

# ==============================================================================
# [MODELS/DATA] DADOS GLOBAIS THREAD-SAFE, HUB B2B E EXPANSÃO SEMÂNTICA
# ==============================================================================
CACHE_IBGE_PATH = "municipios_ibge.pkl"

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

@st.custom_data
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
        if time.time() - os.path.getmtime(CACHE_IBGE_PATH) > TTL_ENDERECO:
            os.remove(CACHE_IBGE_PATH)
        else:
            try:
                with open(CACHE_IBGE_PATH, "rb") as f:
                    d = pickle.load(f)
                    return d.get("municipios", {}), d.get("estados", {}), d.get("distritos", {}), list(d.get("municipios", {}).keys()) + list(d.get("distritos", {}).keys())
            except Exception as e: logging.error(f"Erro ao carregar cache IBGE: {e}")

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
    except Exception as e: logging.error(f"Erro no download IBGE: {e}")
    
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
# [GEOCODING/PARSER] ENGINE DE RESOLUÇÃO UNIVERSAL E ENDEREÇAMENTO CANÔNICO
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
        
        chave_aprendizado_coord = t_raw.upper()
        if chave_aprendizado_coord in cache_aprendizado:
            dado_salvo = cache_aprendizado[chave_aprendizado_coord]
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
        cache_classificacao.set(texto_norm, tipo, expire=TTL_ENDERECO)
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
        cache_fuzzy.set(texto_norm, texto_norm, expire=TTL_ENDERECO)
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
# [GEOCODING/VALIDATORS] VALIDADOR PRÉ-GEOCODING E LÓGICA GEODÉSICA
# ==============================================================================
def auditoria_pre_geocoding(texto_cru, contexto, tipo_entrada):
    if len(texto_cru) < 4: return "INSUFICIENTE"
    if tipo_entrada in ["BAIRRO", "RURAL"] and not contexto.get("municipio"): return "INSUFICIENTE"
    if tipo_entrada in ["ENDERECO_COMPLETO", "LOGRADOURO", "CONDOMINIO"] and not contexto.get("municipio") and not contexto.get("uf"): return "PARCIAL"
    return "COMPLETO"

def obedience_base_local(contexto_estruturado):
    if contexto_estruturado["logradouro"] and contexto_estruturado["municipio"] and contexto_estruturado["uf"]:
        chave_cnefe = f"{contexto_estruturado['logradouro']}_{contexto_estruturado['municipio']}_{contexto_estruturado['uf']}"
        if chave_cnefe in cache_base_local:
            return cache_base_local[chave_cnefe]
    return None

def validar_coordenada_brasil(lat, lon):
    try:
        lat_f, lon_f = float(lat), float(lon)
        if (-35.0 <= lat_f <= 6.0) and (-75.0 <= lon_f <= -28.0): return True, lat_f, lon_f
        if (-35.0 <= lon_f <= 6.0) and (-75.0 <= lat_f <= -28.0): return True, lon_f, lat_f 
        return False, lat_f, lon_f
    except (ValueError, TypeError):
        return False, 0.0, 0.0

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
    except Exception:
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
    if cep_limpo in cache_cep:
        d = cache_cep[cep_limpo]
        if len(d) == 4: return d[0], d[1], d[2], d[3], 0.0, 0.0
        return d
    lat, lon = 0.0, 0.0
    with SEMAPHORES.get("BRASIL_API", SEMAPHORES["DEFAULT"]):
        try:
            r = session.get(f"https://brasilapi.com.br/api/cep/v2/{cep_limpo}", timeout=4).json()
            if "city" in r:
                loc = r.get("location", {}).get("coordinates", {})
                if loc and "latitude" in loc and "longitude" in loc:
                    try: lat, lon = float(loc["latitude"]), float(loc["longitude"])
                    except (ValueError, TypeError): pass
                d = (r.get('street', ''), r.get('neighborhood', ''), r.get('city', ''), r.get('state', ''), lat, lon)
                cache_cep.set(cep_limpo, d, expire=TTL_CEP); return d
        except Exception as e: logging.error(f"Erro BrasilAPI CEP: {e}")
        try:
            r = session.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=4).json()
            if "erro" not in r:
                d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon)
                cache_cep.set(cep_limpo, d, expire=TTL_CEP); return d
        except Exception as e: logging.error(f"Erro ViaCEP: {e}")
        try:
            r = session.get(f"https://opencep.com/v1/{cep_limpo}", timeout=4).json()
            if "error" not in r:
                d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon)
                cache_cep.set(cep_limpo, d, expire=TTL_CEP); return d
        except Exception as e: logging.error(f"Erro OpenCEP: {e}")
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
# [GEOCODING/ONLINE] MÓDULOS DE GEOCODIFICAÇÃO COM TELEMETRIA
# ==============================================================================
def API_Google_Geocoding_Scraper(query):
    start_t = time.time()
    try:
        url = f"https://www.google.com/maps/search/{requests.utils.quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = session.get(url, headers=headers, timeout=5, allow_redirects=True)
        match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.url)
        if not match: match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.text)
        if match: 
            registrar_telemetria("GOOGLE_MAPS", True, time.time() - start_t)
            return [{"lat": float(match.group(1)), "lon": float(match.group(2)), "fonte": "GOOGLE_MAPS", "score_base": 40, "cidade": "", "estado": "", "bairro": "", "logradouro": "", "numero": "", "cep": ""}]
    except Exception as e: logging.error(f"Erro Google Scraper: {e}")
    registrar_telemetria("GOOGLE_MAPS", False, time.time() - start_t)
    return None

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
    except Exception as e: logging.error(f"Erro TomTom: {e}")
    registrar_telemetria("TOMTOM", False, time.time() - start_t)
    return None

def API_ArcGIS(query, ctx=None):
    start_t = time.time()
    try:
        if ctx and (ctx.get("logradouro") or ctx.get("municipio")):
            end, cid, uf, bair, cep = requests.utils.quote(ctx.get("logradouro", "")), requests.utils.quote(ctx.get("municipio", "")), requests.utils.quote(ctx.get("uf", "")), requests.utils.quote(ctx.get("bairro", "")), requests.utils.quote(ctx.get("cep", ""))
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
    except Exception as e: logging.error(f"Erro ArcGIS: {e}")
    registrar_telemetria("ARCGIS", False, time.time() - start_t)
    return None

def API_Nominatim(query, ctx=None):
    start_t = time.time()
    with SEMAPHORES.get("NOMINATIM", SEMAPHORES["DEFAULT"]):
        try:
            def _call_nom():
                time.sleep(1.1)
                if ctx and ctx.get("logradouro") and ctx.get("municipio"):
                    rua, cid, est = requests.utils.quote(ctx["logradouro"]), requests.utils.quote(ctx["municipio"]), requests.utils.quote(ctx.get("uf", ""))
                    url = f"https://nominatim.openstreetmap.org/search?format=json&street={rua}&city={cid}&state={est}&limit=5&addressdetails=1&countrycodes=br"
                else:
                    url = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query)}&limit=5&addressdetails=1&countrycodes=br"
                return session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
                
            r = st.session_state["fila_nominatim"].submit(_call_nom).result()
            resultados = []
            if r:
                for a in r[:5]:
                    addr = a.get("address", {})
                    resultados.append({"lat": float(a['lat']), "lon": float(a['lon']), "fonte": "NOMINATIM", "score_base": 25, "cidade": addr.get('city', addr.get('town', '')).upper(), "estado": addr.get('state', '').upper(), "bairro": addr.get('neighbourhood', addr.get('suburb', '')).upper(), "logradouro": addr.get('road', '').upper(), "numero": str(addr.get('house_number', '')).upper(), "cep": addr.get('postcode', '').replace("-", "")})
                registrar_telemetria("NOMINATIM", True, time.time() - start_t)
            return resultados if resultados else None
        except Exception as e: logging.error(f"Erro Nominatim: {e}")
    registrar_telemetria("NOMINATIM", False, time.time() - start_t)
    return None

def API_Photon(query):
    start_t = time.time()
    with SEMAPHORES.get("PHOTON", SEMAPHORES["DEFAULT"]):
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
        except Exception as e: logging.error(f"Erro Photon: {e}")
    registrar_telemetria("PHOTON", False, time.time() - start_t)
    return None

def API_Overpass_POIs(texto_norm):
    if len(texto_norm) < 10: return None
    if texto_norm in cache_poi: return cache_poi[texto_norm]
    start_t = time.time()
    endpoints = ["https://overpass-api.de/api/interpreter", "https://lz4.overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
    texto_seguro = re.escape(texto_norm)
    query_osm = f'[out:json][timeout:3];(node["name"~"{texto_seguro}",i]["amenity"];way["name"~"{texto_seguro}",i]["amenity"];node["name"~"{texto_seguro}",i]["building"];way["name"~"{texto_seguro}",i]["building"];node["name"~"{texto_seguro}",i]["healthcare"];way["name"~"{texto_seguro}",i]["healthcare"];node["name"~"{texto_seguro}",i]["education"];way["name"~"{texto_seguro}",i]["education"];);out center;'
    
    with SEMAPHORES.get("OVERPASS", SEMAPHORES["DEFAULT"]):
        for url in endpoints:
            try:
                r = session.post(url, data={"data": query_osm}, timeout=4)
                if r.status_code == 200:
                    elems = r.json().get("elements", [])
                    if elems:
                        e = elems[0]
                        lat, lon = e.get("lat", e.get("center", {}).get("lat", 0.0)), e.get("lon", e.get("center", {}).get("lon", 0.0))
                        tags = e.get("tags", {})
                        res_poi = {"lat": lat, "lon": lon, "fonte": "OVERPASS", "score_base": 40, "cidade": tags.get("addr:city", "").upper(), "estado": tags.get("addr:state", "").upper(), "bairro": tags.get("addr:suburb", "").upper(), "logradouro": tags.get("addr:street", "").upper(), "numero": str(tags.get("addr:housenumber", "")).upper(), "cep": tags.get("addr:postcode", "").replace("-", "")}
                        cache_poi.set(texto_norm, [res_poi], expire=TTL_POI)
                        registrar_telemetria("OVERPASS", True, time.time() - start_t)
                        return [res_poi]
            except Exception as e: logging.error(f"Erro Overpass ({url}): {e}"); continue
    registrar_telemetria("OVERPASS", False, time.time() - start_t)
    return None

def executar_reverse_geocoding_multimotor(lat, lon):
    rev_key = f"{round(lat,5)}|{round(lon,5)}"
    if rev_key in cache_reverse: return cache_reverse[rev_key]
    res = {"logradouro": "", "bairro": "", "cidade": "", "municipio": "", "distrito": "", "estado": "", "cep": ""}
    
    with SEMAPHORES.get("NOMINATIM", SEMAPHORES["DEFAULT"]):
        try:
            def _nom_rev():
                time.sleep(1.1)
                url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&addressdetails=1"
                return session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
            a = st.session_state["fila_nominatim"].submit(_nom_rev).result().get("address", {})
            res.update({"logradouro": a.get("road", a.get("pedestrian", "")), "bairro": a.get("neighbourhood", a.get("suburb", a.get("city_district", ""))), "cidade": a.get("city", a.get("town", a.get("municipality", ""))), "estado": a.get("state", "").upper(), "cep": a.get("postcode", "")})
            cache_reverse.set(rev_key, res, expire=TTL_ENDERECO); return res
        except Exception as e: logging.error(f"Erro Reverse Nominatim: {e}")
        
    try:
        url_arc = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/reverseGeocode?location={lon},{lat}&f=json"
        r_arc = session.get(url_arc, timeout=4).json()
        if 'address' in r_arc:
            addr = r_arc['address']
            res.update({"logradouro": addr.get('Address', ''), "bairro": addr.get('Neighborhood', ''), "cidade": addr.get('City', ''), "estado": addr.get('RegionAbbr', '').upper(), "cep": addr.get('Postal', '')})
            cache_reverse.set(rev_key, res, expire=TTL_ENDERECO)
    except Exception as e: logging.error(f"Erro Reverse ArcGIS: {e}")
    return res

# ==============================================================================
# [GEOCODING/CONSENSUS] ENSEMBLE BAYESIANO, DBSCAN E DOSSIÊ XAI
# ==============================================================================
def _gerar_dossie_xai(texto_cru, end_canonico, tipo_entrada, candidatos, valid_labels, feat_cep, feat_uf, feat_mun, confianca, prob_final, vencedor, end_reverse):
    return {
        "1_entrada_original": texto_cru,
        "2_endereco_canonico": end_canonico,
        "3_tipo_detectado": tipo_entrada,
        "4_apis_consultadas": list(set(c.get("fonte", "N/A") for c in candidatos)),
        "5_respostas_individuais": [{"fonte": c.get("fonte"), "lat": c.get("lat"), "lon": c.get("lon"), "score": round(c.get("score_final", 0), 2)} for c in candidatos[:5]],
        "6_distancia_candidatos": "Calculada dinamicamente via Haversine (Radianos) no Algoritmo DBSCAN.",
        "7_resultado_dbscan": f"Cluster espacial majoritário detectado com {len(valid_labels)} fontes concordantes (Densidade).",
        "8_criterio_escolha": "Fusão Probabilística Bayesiana de Vizinhança (Ensemble Independent).",
        "9_evidencias_usadas": {"cep_cruzado": feat_cep, "uf_cruzada": feat_uf, "municipio_cruzado": feat_mun},
        "10_score_final": f"{round(prob_final, 2)} / 100",
        "11_motivos_descarte": "Candidatos descartados por violação de fronteira estadual (Bounding Box), rejeição municipal ou classificação como ruído espacial no DBSCAN (-1).",
        "12_reverse_geocoding": end_reverse,
        "13_fonte_escolhida": vencedor.get('fonte', 'N/A'),
        "14_grau_confianca": confianca,
        "explicacoes_didaticas": {
            "Fuzzy Matching": "Algoritmo que mede o quanto dois textos se parecem, lidando com abreviações. Ex: 'Rua Flores' e 'R. das Flores'.",
            "DBSCAN": "Algoritmo de Machine Learning que varre o mapa e agrupa coordenadas que estão muito próximas umas das outras, isolando erros graves.",
            "Vincenty": "Fórmula matemática extremamente precisa que calcula a distância entre dois pontos considerando o formato oval (elipsoide) da Terra.",
            "Ensemble Bayesiano": "Cálculo estatístico que aumenta exponencialmente a confiança final quando múltiplas fontes diferentes (Google, TomTom) concordam no mesmo local exato."
        }
    }

def processar_consenso_dinamico(candidatos, tipo_entrada, texto_cru, end_canonico):
    candidatos_validos = []
    candidatos_para_avaliacao = candidatos.copy()
    
    ctx_inf = semantica.resolver_contexto_administrativo(texto_cru.upper())
    uf_inf, mun_inf, dist_inf = ctx_inf.get("uf", ""), ctx_inf.get("municipio", ""), ctx_inf.get("distrito", "")
    box = BOUNDING_BOXES_UF.get(uf_inf) if uf_inf else None
    
    for c in candidatos:
        valido, lat_c, lon_c = validar_coordenada_brasil(c["lat"], c["lon"])
        if valido:
            if box and not (box["lat_min"] <= lat_c <= box["lat_max"] and box["lon_min"] <= lon_c <= box["lon_max"]): continue
            c["lat"], c["lon"] = lat_c, lon_c 
            candidatos_validos.append(c)
            
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
    valid_labels_dbscan = []
    if len(coords_matriz) >= 2:
        coords_rad = np.radians(coords_matriz)
        eps_angular = raio_cluster_km / 6371.0
        db_model = DBSCAN(eps=eps_angular, min_samples=2, metric='haversine').fit(coords_rad)
        labels = db_model.labels_
        valid_labels_dbscan = [l for l in labels if l != -1]
        if valid_labels_dbscan:
            contagem_clusters = collections.Counter(valid_labels_dbscan).most_common(2)
            if len(contagem_clusters) > 1 and contagem_clusters[0][1] == contagem_clusters[1][1]:
                c1_amb = candidatos_validos[labels.tolist().index(contagem_clusters[0][0])]
                c2_amb = candidatos_validos[labels.tolist().index(contagem_clusters[1][0])]
                motivo_amb = f"AMBÍGUO: Empate de consenso entre {c1_amb.get('cidade','')}/{c1_amb.get('estado','')} e {c2_amb.get('cidade','')}/{c2_amb.get('estado','')}"
                return {"lat": 0.0, "lon": 0.0, "endereco": texto_cru, "confianca": "AMBIGUA", "score": 0, "distrito": "", "municipio": "", "fonte": "N/A", "xai": {"erro": motivo_amb}}
                
            maior_cluster_label = contagem_clusters[0][0]
            candidatos_validos = [candidatos_validos[idx] for idx, label in enumerate(labels) if label == maior_cluster_label]
    if not candidatos_validos: return None

    input_usuario = ParserGeograficoBR.extrair_componentes(texto_cru.upper())
    candidatos_consistentes_uf = [c for c in candidatos_validos if validar_consistencia_administrativa(c, uf_inf)]
    if candidatos_consistentes_uf: candidatos_validos = candidatos_consistentes_uf

    candidatos_consistentes_mun = [c for c in candidatos_validos if validar_consistencia_municipal(c, mun_inf)]
    if candidatos_consistentes_mun: candidatos_validos = candidatos_consistentes_mun
        
    PESO_FONTES = {}
    DEFAULT_WEIGHTS = {"GOOGLE_MAPS": 1.00, "ARCGIS": 0.95, "TOMTOM": 0.90, "OVERPASS": 0.85, "NOMINATIM": 0.80, "PHOTON": 0.75}
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
        p_prior = min(c1.get("score_base", 30) / 100.0, 0.50)
        
        feat_mun = mun_inf and c1.get("cidade") and (mun_inf in c1["cidade"] or fuzz.token_set_ratio(mun_inf, c1["cidade"]) >= 95)
        feat_uf = uf_inf and c1.get("estado") and uf_inf in c1["estado"]
        feat_cep = input_usuario.get("cep") and c1.get("cep") and input_usuario["cep"] in c1.get("cep", "").replace("-", "")
        feat_bairro = dist_inf and c1.get("bairro") and dist_inf in c1["bairro"]
        feat_numero = input_usuario.get("numero") and c1.get("numero") and input_usuario["numero"] in c1.get("numero", "")
        fuzz_rua = fuzz.token_set_ratio(texto_cru.upper(), c1.get("logradouro", "")) / 100.0 if c1.get("logradouro") else 0.1
        
        PADROES_RODOVIA = [r'\bBR[- ]?\d+\b', r'\bSP[- ]?\d+\b', r'\bMG[- ]?\d+\b', r'\bGO[- ]?\d+\b', r'\bDF[- ]?\d+\b', r'\bRJ[- ]?\d+\b', r'\bPR[- ]?\d+\b', r'\bSC[- ]?\d+\b', r'\bRS[- ]?\d+\b']
        input_tem_rodovia = any(re.search(p, texto_cru.upper()) for p in PADROES_RODOVIA)
        api_tem_rodovia = any(re.search(p, c1.get("logradouro", "").upper()) for p in PADROES_RODOVIA) or bool(re.search(r'\b(RODOVIA|KM|ESTRADA)\b', c1.get("logradouro", "").upper()))
        feat_punicao_rodovia = not input_tem_rodovia and api_tem_rodovia
        
        api_end_str = f"{c1.get('logradouro','')} {c1.get('bairro','')} {c1.get('cidade','')} {c1.get('estado','')}".upper()
        l_conf_rural = 0.2 if (tipo_entrada == "RURAL" and any(urb in api_end_str for urb in ["QUADRA ", "SQN ", "SQS ", "APARTAMENTO ", "EDIFICIO ", "BLOCO "])) else 1.0
        l_conf_urbano = 0.4 if (tipo_entrada in ["ENDERECO_COMPLETO", "BAIRRO"] and any(rur in api_end_str for rur in ["CHACARA ", "FAZENDA ", "GLEBA "])) else 1.0

        probabilidades_cluster = [p_prior]
        apis_concordantes = set([c1.get("fonte", "")])
        
        for c2 in candidatos_validos:
            if c1.get("fonte") != c2.get("fonte"):
                # Fast-Path Pruning Bounding Box (~15km)
                if abs(c1["lat"] - c2["lat"]) > 0.15 or abs(c1["lon"] - c2["lon"]) > 0.15: continue
                dist = calcular_distancia_vincenty(c1["lat"], c1["lon"], c2["lat"], c2["lon"])
                if dist <= raio_cluster_km: 
                    apis_concordantes.add(c2.get("fonte", ""))
                    probabilidades_cluster.append(PESO_FONTES.get(c2.get("fonte", ""), 0.5))
        
        falha_combinada = 1.0
        for prob in probabilidades_cluster: falha_combinada *= (1.0 - prob)
        prob_ensemble = 1.0 - falha_combinada
        
        odds = (prob_ensemble / (1 - prob_ensemble)) * (bm["mun"] if feat_mun else 0.4) * (bm["uf"] if feat_uf else 0.7) * (bm["cep"] if feat_cep else 0.9) * (bm["bairro"] if feat_bairro else 0.9) * (bm["numero"] if feat_numero else 0.8) * (0.5 + (fuzz_rua * bm["rua_peso"])) * (0.1 if feat_punicao_rodovia else 1.0) * l_conf_rural * l_conf_urbano
        
        c1["score_final"] = min((odds / (1 + odds)) * 100, 99.9)
        c1["xai_data"] = {"mun": bool(feat_mun), "uf": bool(feat_uf), "cep": bool(feat_cep), "num": bool(feat_numero), "fuzz": round(fuzz_rua * 100, 1), "apis": list(apis_concordantes)}
        
    candidatos_validos.sort(key=lambda x: x["score_final"], reverse=True)
    
    vencedor = None
    for cand in candidatos_validos[:3]:
        m = executar_reverse_geocoding_multimotor(cand["lat"], cand["lon"])
        if uf_inf and m.get("estado") and uf_inf != m.get("estado", "").upper().strip(): continue 
        cidade_rev = m.get("cidade", "").upper().strip()
        if mun_inf and cidade_rev and not ((mun_inf in cidade_rev) or (cidade_rev in mun_inf) or (fuzz.token_set_ratio(mun_inf, cidade_rev) >= 85)): continue
        
        end_reverse = ", ".join([c for c in [m.get("logradouro", ""), m.get("bairro", ""), m.get("cidade", ""), m.get("estado", "").upper()] if c.strip()])
        if fuzz.token_set_ratio(texto_cru.upper(), end_reverse.upper()) >= 70:
            vencedor = cand; break
            
    if not vencedor: return None
    
    for cand in candidatos_para_avaliacao:
        if cand.get("lat", 0.0) == 0.0 or cand.get("lon", 0.0) == 0.0: continue
        f_n = cand.get("fonte", "")
        if abs(cand["lat"] - vencedor["lat"]) <= 0.15 and abs(cand["lon"] - vencedor["lon"]) <= 0.15:
            if calcular_distancia_vincenty(cand["lat"], cand["lon"], vencedor["lat"], vencedor["lon"]) <= 0.05:
                metr = cache_api_health.get(f_n, {"hits": 0, "calls": 0, "falhas": 0, "tempo_total": 0.0})
                metr["hits"] += 1
                cache_api_health.set(f_n, metr, expire=None)

    m = executar_reverse_geocoding_multimotor(vencedor["lat"], vencedor["lon"])
    score_lim = min(int(vencedor["score_final"]), 95 if tipo_entrada == "ENDERECO_COMPLETO" else 100 if tipo_entrada == "CEP" else 85)
    if m.get("cep"): score_lim = min(score_lim + 10, 100 if tipo_entrada == "CEP" else 95)

    match_logr = fuzz.token_set_ratio(texto_cru.upper(), m.get("logradouro", "").upper())
    match_bairro = fuzz.token_set_ratio(dist_inf, m.get("bairro", "").upper()) if dist_inf else 100
    match_cep = 100 if input_usuario.get("cep") and m.get("cep") and input_usuario["cep"] in m.get("cep", "").replace("-", "") else 0 if input_usuario.get("cep") else 100
    
    confianca = "MUNICIPAL" if tipo_entrada in ["ENDERECO_COMPLETO", "CEP"] and not vencedor.get("logradouro") else "ALTISSIMA" if score_lim >= 85 else "ALTA" if score_lim >= 75 else "MEDIA" if score_lim >= 60 else "BAIXA"
    
    if (match_logr * 0.5) + (match_bairro * 0.3) + (match_cep * 0.2) < 65.0:
        confianca = "REVISAO_MANUAL"
        score_lim = min(score_lim, 49)

    rua_f = m.get("logradouro") if m.get("logradouro") else texto_cru.upper()
    endereco_f = ", ".join([c for c in [rua_f, m.get("bairro", ""), m.get("cidade", ""), m.get("estado", "").upper()] if c.strip()]) + ", BRASIL"
    
    xai_report = _gerar_dossie_xai(texto_cru, end_canonico, tipo_entrada, candidatos_para_avaliacao, valid_labels_dbscan, vencedor["xai_data"]["cep"], vencedor["xai_data"]["uf"], vencedor["xai_data"]["mun"], confianca, vencedor["score_final"], vencedor, endereco_f)

    return {"lat": vencedor["lat"], "lon": vencedor["lon"], "endereco": endereco_f, "confianca": confianca, "score": score_lim, "distrito": m.get("distrito", ""), "municipio": m.get("cidade", ""), "fonte": vencedor.get("fonte", ""), "xai": xai_report}

# ==============================================================================
# [SERVICES/ORQUESTRADOR] CASCATA HIERÁRQUICA E OFFLINE-FIRST
# ==============================================================================
def _resolver_bypass_coordenadas(texto_cru):
    if match_coords := re.match(r'^\s*(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*$', texto_cru):
        lat_in, lon_in = float(match_coords.group(1)), float(match_coords.group(2))
        if validar_coordenada_brasil(lat_in, lon_in)[0]:
            m = executar_reverse_geocoding_multimotor(lat_in, lon_in)
            end_f = ", ".join([c for c in [m.get("logradouro", ""), m.get("bairro", ""), m.get("cidade", ""), m.get("estado", "")] if c.strip()]) + ", BRASIL"
            return {"lat": lat_in, "lon": lon_in, "endereco": end_f, "confianca": "ABSOLUTA", "score": 100, "distrito": m.get("bairro", ""), "municipio": m.get("cidade", ""), "fonte": "COORDENADA_EXATA", "xai": {"motivo": "Entrada direta numérica"}}
    return None

def _resolver_bypass_pois_b2b(texto_cru):
    for poi_key, poi_data in BASE_POIS_LOGISTICOS.items():
        if poi_key in texto_cru.upper():
            return {"lat": poi_data["lat"], "lon": poi_data["lon"], "endereco": poi_data["endereco"], "confianca": "ABSOLUTA", "score": 100, "distrito": "", "municipio": poi_data["municipio"], "fonte": "BASE_POIS_NACIONAIS", "xai": {"motivo": "Base Logística Local"}}
    return None

def _orquestrar_apis_online(endereco_canonico, contexto_estruturado, tipo_entrada, texto_cru):
    candidatos_validos = []
    def disparar_apis_paralelas(tarefas):
        resultados = []
        for f in as_completed([st.session_state["executor_apis"].submit(func, *args, **kwargs) for func, args, kwargs in tarefas]):
            if res := f.result(): resultados.extend(res)
        return resultados

    if tipo_entrada in ["POI", "CONDOMINIO"]:
        candidatos_validos.extend(disparar_apis_paralelas([(API_Google_Geocoding_Scraper, (endereco_canonico,), {}), (API_Overpass_POIs, (semantica.normalizar(texto_cru),), {}), (API_TomTom, (endereco_canonico,), {})]))
    elif tipo_entrada in ["ENDERECO_COMPLETO", "LOGRADOURO"]:
        candidatos_validos.extend(disparar_apis_paralelas([(API_ArcGIS, (endereco_canonico,), {"ctx": contexto_estruturado}), (API_Google_Geocoding_Scraper, (endereco_canonico,), {}), (API_TomTom, (endereco_canonico,), {})]))
        if res_nom := API_Nominatim(endereco_canonico, ctx=contexto_estruturado): candidatos_validos.extend(res_nom)
    elif tipo_entrada in ["BAIRRO", "MUNICIPIO", "DISTRITO"]:
        candidatos_validos.extend(disparar_apis_paralelas([(API_Photon, (endereco_canonico,), {})]))
        if res_nom := API_Nominatim(endereco_canonico, ctx=contexto_estruturado): candidatos_validos.extend(res_nom)
    else:
        candidatos_validos.extend(disparar_apis_paralelas([(API_Google_Geocoding_Scraper, (endereco_canonico,), {}), (API_Photon, (endereco_canonico,), {}), (API_ArcGIS, (endereco_canonico,), {"ctx": contexto_estruturado}), (API_TomTom, (endereco_canonico,), {})]))
            
    res_final = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru, endereco_canonico)
    if not res_final and tipo_entrada not in ["BAIRRO", "MUNICIPIO"]:
        if res_nom := API_Nominatim(endereco_canonico, ctx=contexto_estruturado):
            candidatos_validos.extend(res_nom)
            res_final = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru, endereco_canonico)
    return res_final

def obter_coordenadas_e_endereco_oficial(localidade):
    texto_cru = str(localidade).strip()
    if not texto_cru or texto_cru.lower() == 'nan': return {"lat": 0.0, "lon": 0.0, "endereco": "", "confianca": "BAIXA", "score": 0, "distrito": "", "municipio": "", "fonte": "N/A", "xai": {}}
    
    if bypass := _resolver_bypass_coordenadas(texto_cru): return bypass
    if bypass_poi := _resolver_bypass_pois_b2b(texto_cru): return bypass_poi

    chave_aprendizado_coord = texto_cru.upper()
    if chave_aprendizado_coord in cache_aprendizado_auto:
        d = cache_aprendizado_auto[chave_aprendizado_coord]
        if isinstance(d, dict) and "lat" in d:
            return {"lat": d["lat"], "lon": d["lon"], "endereco": d.get("endereco", texto_cru.upper()), "confianca": "ALTISSIMA", "score": 100, "distrito": d.get("distrito", ""), "municipio": d.get("municipio", ""), "fonte": "APRENDIZADO_LOCAL", "xai": d.get("metadata", {}).get("evidencias_xai", {})}

    endereco_canonico, tipo_entrada, _, _, _ = semantica.construir_endereco_canonico(texto_cru)
    ctx = semantica.resolver_contexto_administrativo(texto_cru.upper())
    parsed_comp = ParserGeograficoBR.extrair_componentes(texto_cru.upper())
    
    cache_key = hashlib.md5(f"{tipo_entrada}_{endereco_canonico}".encode('utf-8')).hexdigest()
    if cache_key in cache_geo:
        c = cache_geo[cache_key]
        return {"lat": c["lat"], "lon": c["lon"], "endereco": c["endereco"], "confianca": c["confianca"], "score": c["score_num"], "distrito": c.get("distrito", ""), "municipio": c.get("municipio", ""), "fonte": c.get("fonte", ""), "xai": c.get("xai", {})}

    rua_suja = parsed_comp["resto"]
    for loc in [ctx.get("municipio", ""), ctx.get("distrito", ""), ctx.get("uf", ""), "BRASIL", "DF"]:
        if loc: rua_suja = re.sub(rf'\b{loc}\b', '', rua_suja).strip(" ,-")
        
    rua_limpa = re.sub(r'\s+', ' ', rua_suja).strip()
    if parsed_comp["numero"]: rua_limpa = f"{rua_limpa} {parsed_comp['numero']}".strip()
    
    contexto_estruturado = {"logradouro": rua_limpa if rua_limpa else texto_cru.upper(), "bairro": ctx.get("distrito", ""), "municipio": ctx.get("municipio", ""), "uf": ctx.get("uf", ""), "cep": parsed_comp.get("cep", "")}

    if auditoria_pre_geocoding(texto_cru, contexto_estruturado, tipo_entrada) == "INSUFICIENTE":
        return {"lat": 0.0, "lon": 0.0, "endereco": texto_cru, "confianca": "INSUFICIENTE", "score": 0, "distrito": "", "municipio": "", "fonte": "PRE_FLIGHT", "xai": {}}

    if match_offline := obedience_base_local(contexto_estruturado):
        return {"lat": match_offline["lat"], "lon": match_offline["lon"], "endereco": match_offline["endereco"], "confianca": "ALTISSIMA", "score": 100, "distrito": match_offline.get("distrito", ""), "municipio": match_offline.get("municipio", ""), "fonte": "BASE_NACIONAL_OFFLINE", "xai": {}}

    if not ctx.get("municipio") and tipo_entrada not in ["POI", "CEP"]:
        return {"lat": 0.0, "lon": 0.0, "endereco": endereco_canonico, "confianca": "BAIXA", "score": 0, "distrito": "", "municipio": "", "fonte": "N/A", "xai": {}}

    if tipo_entrada == "CEP":
        cep_estrito = re.search(r'\b\d{5}-?\d{3}\b', texto_cru)
        if cep_estrito:
            cep_limpo = cep_estrito.group(0).replace("-", "")
            logr, bair, loca, uf, lat_c, lon_c = cascata_postal_tripla(cep_limpo)
            if loca:
                nome_est_cep = IBGE_ESTADOS.get(uf, uf) if uf else ""
                addr_c = re.sub(r',\s*,', ',', f"{logr}, {bair}, {loca}, {nome_est_cep}, CEP {cep_estrito.group(0)}, BRASIL").strip(' ,')
                
                if validar_coordenada_brasil(lat_c, lon_c)[0] and lat_c != 0.0:
                    res_f = {"lat": lat_c, "lon": lon_c, "endereco": addr_c, "confianca": "ALTISSIMA", "score": 100, "distrito": bair, "municipio": loca, "fonte": "BrasilAPI/OSM Postal", "xai": {"motivo": "Cascata Direta"}}
                    cache_geo.set(cache_key, {"lat": lat_c, "lon": lon_c, "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca, "fonte": "BrasilAPI/OSM Postal"}, expire=TTL_CEP)
                    return res_f
                
                res_arc = API_ArcGIS(addr_c)
                if res_arc:
                    if isinstance(res_arc, list): res_arc = res_arc[0]
                    val_arc, lat_arc, lon_arc = validar_coordenada_brasil(res_arc["lat"], res_arc["lon"])
                    if val_arc:
                        res_f = {"lat": lat_arc, "lon": lon_arc, "endereco": addr_c, "confianca": "ALTISSIMA", "score": 100, "distrito": bair, "municipio": loca, "fonte": "ViaCEP/ArcGIS", "xai": {"motivo": "Cascata + ArcGIS"}}
                        cache_geo.set(cache_key, {"lat": lat_arc, "lon": lon_arc, "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca, "fonte": "ViaCEP/ArcGIS"}, expire=TTL_CEP)
                        return res_f

    if tipo_entrada == "MUNICIPIO" and ctx.get("municipio") and ctx.get("uf"):
        mun_nome, uf_nome = ctx["municipio"], ctx["uf"]
        if mun_nome in IBGE_MUNICIPIOS:
            for item in IBGE_MUNICIPIOS[mun_nome]:
                if item["uf"] == uf_nome and item.get("lat", 0.0) != 0.0 and item.get("lon", 0.0) != 0.0:
                    endereco_ibge = f"{mun_nome}, {IBGE_ESTADOS.get(uf_nome, uf_nome)}, BRASIL"
                    res_ibge = {"lat": item["lat"], "lon": item["lon"], "endereco": endereco_ibge, "confianca": "ALTISSIMA", "score": 100, "distrito": "", "municipio": mun_nome, "fonte": "BASE_IBGE_LOCAL", "xai": {}}
                    cache_geo.set(cache_key, {"lat": res_ibge["lat"], "lon": res_ibge["lon"], "endereco": res_ibge["endereco"], "confianca": res_ibge["confianca"], "score_num": res_ibge["score"], "distrito": res_ibge["distrito"], "municipio": res_ibge["municipio"], "fonte": res_ibge["fonte"]}, expire=TTL_ENDERECO)
                    return res_ibge

    res_final = _orquestrar_apis_online(endereco_canonico, contexto_estruturado, tipo_entrada, texto_cru)

    if res_final:
        cache_geo.set(cache_key, {"lat": res_final["lat"], "lon": res_final["lon"], "endereco": res_final["endereco"], "confianca": res_final["confianca"], "score_num": res_final["score"], "distrito": res_final["distrito"], "municipio": res_final["municipio"], "fonte": res_final["fonte"], "xai": res_final.get("xai", {})}, expire=TTL_ENDERECO)
        if res_final["score"] >= 95 and res_final["confianca"] == "ALTISSIMA":
            cache_aprendizado_auto.set(chave_aprendizado_coord, {"lat": res_final["lat"], "lon": res_final["lon"], "endereco": res_final["endereco"], "distrito": res_final["distrito"], "municipio": res_final["municipio"], "metadata": {"evidencias_xai": res_final.get("xai", {})}}, expire=TTL_ENDERECO)
        return res_final
        
    return {"lat": 0.0, "lon": 0.0, "endereco": endereco_canonico, "confianca": "BAIXA", "score": 0, "distrito": "", "municipio": "", "fonte": "N/A", "xai": {}}

# ==============================================================================
# [ROUTING] MOTOR DE ARBITRAGEM DE PROVEDORES DE DISTÂNCIA
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
        texto_resposta = resposta.text
        if len(texto_resposta) < 500 or "directions" not in texto_resposta.lower(): return None
        with open(f"logs_google/{hash(cache_key)}.txt", "w", encoding="utf-8") as f: f.write(texto_resposta)
            
        match_km = re.findall(r'\"(\d+[\.,]?\d*)\s*km\"', texto_resposta)
        match_tempo = re.findall(r'\"(\d+\s*h\s*\d+\s*min|\d+\s*h|\d+\s*min)\"', texto_resposta)
        if match_km and match_tempo:
            km_puro = float(match_km[0].replace('.', '').replace(',', '.'))
            if dist_linha_reta > 0:
                limite_curto = max(dist_linha_reta * 2.0, dist_linha_reta + 15.0)
                if dist_linha_reta <= 50.0 and km_puro > limite_curto: return None  
                elif km_puro < dist_linha_reta * 0.8 or km_puro > dist_linha_reta * 4.0: return None  

            envolve_balsa = "Sim" if any(re.search(p, texto_resposta.lower()) for p in [r'\"utilizar\s+balsa\b', r'\"ferry\b']) else "Não"
            res = (km_puro, match_tempo[0], link_maps, envolve_balsa, 70 + (10 if km_puro > 0 else 0) + (10 if match_tempo[0] else 0) + (10 if km_puro >= dist_linha_reta else 0))
            cache_google.set(cache_key, res, expire=TTL_ROTA); return res
    except Exception as e: logging.error(f"Erro Google Rota: {e}")
    return None

def rota_osrm(lat_o, lon_o, lat_d, lon_d):
    try:
        url = f"https://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=false"
        r = session.get(url, timeout=5).json()
        if r.get("routes"):
            km = round(r["routes"][0]["distance"] / 1000, 2)
            minutos = round(r["routes"][0]["duration"] / 60)
            return km, f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min", "OSRM", 95
    except Exception as e: logging.error(f"Erro OSRM: {e}")
    return None

def obter_fator_desvio_rodoviario(linha_reta):
    return 1.45 if linha_reta < 5.0 else 1.35 if linha_reta < 20.0 else 1.25 if linha_reta < 100.0 else 1.18

def calcular_pipeline_logistico(origem, destino, perfil_rota="shortest"):
    start_total = time.time()
    origem_clean, destino_clean = str(origem).strip(), str(destino).strip()
    
    chave_rota_cache = f"ROTA_{semantica.normalizar(origem_clean)}->{semantica.normalizar(destino_clean)}"
    if chave_rota_cache in cache_rotas: return cache_rotas[chave_rota_cache]
    
    start_geo = time.time()
    res_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    res_d = obter_coordenadas_e_endereco_oficial(destino_clean)
    tempo_geocoding = round(time.time() - start_geo, 2)
    start_rot = time.time()

    lat_o, lon_o = res_o["lat"], res_o["lon"]
    lat_d, lon_d = res_d["lat"], res_d["lon"]
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d) if lat_o and lat_d else 0.0

    link_fallback = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(res_o['endereco'])}&destination={requests.utils.quote(res_d['endereco'])}&travelmode=driving"

    usar_coords = True if (lat_o != 0.0 and lat_d != 0.0) else False
    if usar_coords and dist_linha_reta > 150.0:
        siglas_originais = re.findall(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM|PA|MT|MS)\b', origem_clean.upper() + " " + destino_clean.upper())
        if len(set(siglas_originais)) <= 1: usar_coords = False

    res_osrm = rota_osrm(lat_o, lon_o, lat_d, lon_d) if usar_coords else None
    res_google = extrair_dados_reais_google(res_o['endereco'], res_d['endereco'], lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=usar_coords)

    def empacotar_retorno(dist, tempo, balsa, fonte_r, score_r):
        return {
            "distancia_km": dist, "tempo_str": tempo, "link_rota": link_fallback if not res_google else res_google[2], "balsas": balsa,
            "linha_reta": dist_linha_reta, "fonte_rota": fonte_r, "score_rota": score_r,
            "conf_origem": res_o["confianca"], "score_origem": res_o["score"], "distrito_origem": res_o["distrito"], "mun_origem": res_o["municipio"], "fonte_origem": res_o["fonte"], "endereco_origem": res_o["endereco"], "lat_origem": lat_o, "lon_origem": lon_o,
            "conf_destino": res_d["confianca"], "score_destino": res_d["score"], "distrito_destino": res_d["distrito"], "mun_destino": res_d["municipio"], "fonte_destino": res_d["fonte"], "endereco_destino": res_d["endereco"], "lat_destino": lat_d, "lon_destino": lon_d,
            "tempo_geo": tempo_geocoding, "tempo_rot": round(time.time() - start_rot, 2), "tempo_total": round(time.time() - start_total, 2),
            "xai_o": res_o.get("xai", {}), "xai_d": res_d.get("xai", {})
        }

    if res_osrm and perfil_rota == "fastest":
        ret = empacotar_retorno(res_osrm[0], res_osrm[1], "Não", res_osrm[2], res_osrm[3])
        cache_rotas.set(chave_rota_cache, ret, expire=TTL_ROTA); return ret

    if perfil_rota == "shortest":
        opcoes = []
        if res_osrm: opcoes.append((res_osrm[0], res_osrm[1], "Não", res_osrm[2], res_osrm[3]))
        if res_google: opcoes.append((res_google[0], res_google[1], res_google[3], "Google Preview", res_google[4]))
        if opcoes:
            m_opt = min(opcoes, key=lambda x: x[0]) 
            ret = empacotar_retorno(*m_opt)
            cache_rotas.set(chave_rota_cache, ret, expire=TTL_ROTA); return ret

    if res_google:
        ret = empacotar_retorno(res_google[0], res_google[1], res_google[3], "Google Preview", res_google[4])
        cache_rotas.set(chave_rota_cache, ret, expire=TTL_ROTA); return ret

    km_terrestre = round(dist_linha_reta * obter_fator_desvio_rodoviario(dist_linha_reta), 2)
    minutos_est = round((km_terrestre / (45.0 if km_terrestre < 50.0 else 65.0)) * 60) if km_terrestre > 0 else 0
    tempo_geo_str = f"{minutos_est} min" if minutos_est < 60 else f"{minutos_est // 60} h {minutos_est % 60} min"
    
    ret = empacotar_retorno(km_terrestre, tempo_geo_str, "Não", "Geodésico Adaptativo", 70)
    cache_rotas.set(chave_rota_cache, ret, expire=TTL_ROTA); return ret

def embrulhar_task_paralela(item):
    par_id, orig, dest = item
    try: return par_id, calcular_pipeline_logistico(orig, dest, perfil_rota="shortest")
    except Exception as e: logging.error(f"Erro Task Paralela: {e}"); return par_id, None

# ==============================================================================
# [UI/FRONTEND] INTERFACE STREAMLIT COM ENGINE DE SIDEBAR E ABAS
# ==============================================================================
st.markdown("""
<div style="background-color:#1E1E1E; padding:20px; border-radius:10px; margin-bottom: 25px; border-left: 5px solid #00FF7F;">
    <h1 style="color:white; margin:0;">🗺️ Motor Nacional de Roteirização Inteligente</h1>
    <p style="color:#A0A0A0; margin:0; font-size: 16px;">Plataforma Corporativa B2B de Geocodificação, Inferência Bayesiana e Auditoria Logística.</p>
</div>
""", unsafe_allow_html=True)

with st.sidebar:
    st.header("📖 Manual do Sistema")
    with st.expander("🎯 Visão Geral"): st.markdown("1. Validador Offline\n2. Ensemble Bayesiano (DBSCAN)\n3. Roteamento Multimotor.")
    with st.expander("📍 Geocodificação"): st.markdown("APIs Independentes em Paralelo: ArcGIS, Google, TomTom, Nominatim, Photon, Overpass.")
    with st.expander("📊 Score e XAI"): st.markdown("Pesos Dinâmicos Bayesianos para Origem e Destino com Dossiê Ativo.")

tab_individual, tab_processamento, tab_analytics, tab_auditoria = st.tabs([
    "📍 Geocodificação Rápida", "⚙️ Processamento em Lote", "📊 Analytics & Saúde", "🕵️ Aba de Auditoria (XAI)"
])

with tab_individual:
    st.markdown("### 🔍 Validador Rápido de Rota (Single-Shot)")
    col_ind1, col_ind2 = st.columns(2)
    with col_ind1: orig_ind = st.text_input("Origem (Endereço, POI ou Coordenadas)", "CD MERCADO LIVRE CAJAMAR")
    with col_ind2: dest_ind = st.text_input("Destino (Endereço, POI ou Coordenadas)", "-15.793889, -47.882778")
    
    if st.button("🚀 Calcular Rota Individual", type="primary"):
        if orig_ind and dest_ind:
            with st.spinner("Acionando motores de geocodificação e consenso..."):
                res_ind = calcular_pipeline_logistico(orig_ind, dest_ind, perfil_rota="shortest")
                
            if res_ind and res_ind.get("conf_origem") != "INSUFICIENTE":
                st.success("✅ Rota estabelecida com sucesso!")
                m_dist, m_time, m_score = st.columns(3)
                m_dist.metric("Distância Viária", f"{res_ind['distancia_km']} km")
                m_time.metric("Tempo Estimado", res_ind['tempo_str'])
                score_g = round((0.35 * res_ind['score_origem']) + (0.35 * res_ind['score_destino']) + (0.30 * res_ind['score_rota']), 2)
                m_score.metric("Score Global de Qualidade", f"{score_g} / 100")
                
                lat_orig, lon_orig, lat_dest, lon_dest = res_ind['lat_origem'], res_ind['lon_origem'], res_ind['lat_destino'], res_ind['lon_destino']
                
                # Fallback Geográfico de Renderização
                if lat_orig == 0.0 or lat_dest == 0.0:
                    st.warning("⚠️ O mapa não pôde ser renderizado com precisão pois uma das coordenadas retornou inválida (0.0). Exibindo centro do país.")
                    lat_c, lon_c, zoom_lvl = -15.793889, -47.882778, 3
                    arc_layer, scatter_layer, coverage_layer = None, None, None
                else:
                    lat_c, lon_c, zoom_lvl = (lat_orig + lat_dest) / 2, (lon_orig + lon_dest) / 2, 4
                    arc_layer = pdk.Layer("ArcLayer", data=[{"o": [lon_orig, lat_orig], "d": [lon_dest, lat_dest]}], get_source_position="o", get_target_position="d", get_source_color=[0, 255, 128, 160], get_target_color=[255, 0, 0, 160], width_scale=0.04, width_min_pixels=3, width_max_pixels=15)
                    scatter_layer = pdk.Layer("ScatterplotLayer", data=[{"pos": [lon_orig, lat_orig], "color": [0, 255, 128]}, {"pos": [lon_dest, lat_dest], "color": [255, 0, 0]}], get_position="pos", get_fill_color="color", get_radius=800)
                    coverage_layer = pdk.Layer("ScatterplotLayer", data=[{"pos": [lon_dest, lat_dest], "r": 50000, "c": [255, 165, 0, 80]}, {"pos": [lon_dest, lat_dest], "r": 100000, "c": [0, 191, 255, 60]}, {"pos": [lon_dest, lat_dest], "r": 200000, "c": [138, 43, 226, 40]}], get_position="pos", get_radius="r", stroked=True, filled=True, get_fill_color="c", get_line_color=[255, 255, 255, 150], line_width_min_pixels=1)
                
                layers_to_render = [l for l in [coverage_layer, arc_layer, scatter_layer] if l is not None]
                st.pydeck_chart(pdk.Deck(layers=layers_to_render, initial_view_state=pdk.ViewState(latitude=lat_c, longitude=lon_c, zoom=zoom_lvl, pitch=45), map_style="mapbox://styles/mapbox/dark-v10"))
                st.info(f"**Origem fixada por:** {res_ind['fonte_origem']} | **Destino fixada por:** {res_ind['fonte_destino']} | **Motor da Rota:** {res_ind['fonte_rota']}")
                st.markdown(f"[🔗 Abrir Rota no Google Maps]({res_ind['link_rota']})")
                
                st.session_state['logs_auditoria'] = [res_ind.get('xai_o', {}), res_ind.get('xai_d', {})]
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
            if len(df) > MAX_LINHAS: st.error(f"⚠️ Limite de {MAX_LINHAS} linhas excedido."); st.stop()
            st.success(f"Tabela com {len(df)} registros mapeada! Pronto para processar.")
            
            nome_operador = st.text_input("Matrícula / Nome do Operador (Opcional)", max_chars=50)
            
            if st.button("Iniciar Processamento em Lote"):
                start_lote_clock = time.time()
                novas_colunas = ['Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta', 'Fonte da Rota', 'Score da Rota', 'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem', 'Fonte Geocoding Origem', 'Endereco Oficial Origem', 'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino', 'Fonte Geocoding Destino', 'Endereco Oficial Destino', 'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)', 'Score Final Global', 'Status da Rota']
                for col in novas_colunas: df[col] = None
                    
                pares_unicos = set()
                mapeamento_linhas = []
                for index, linha in df.iterrows():
                    origem = str(getattr(linha, 'Origem', '')).strip() if pd.notna(getattr(linha, 'Origem', '')) else ""
                    destino = str(getattr(linha, 'Destino', '')).strip() if pd.notna(getattr(linha, 'Destino', '')) else ""
                    if origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan':
                        pares_unicos.add((origem, destino))
                        mapeamento_linhas.append((index, origem, destino))
                
                if not pares_unicos: st.warning("Nenhuma linha contendo endereços válidos."); st.stop()
                    
                MAPA_PRIORIDADE = {"CEP": 1, "ENDERECO_COMPLETO": 2, "POI": 3, "CONDOMINIO": 3, "MUNICIPIO": 4, "BAIRRO": 5, "RURAL": 6, "LOGRADOURO": 7}
                tarefas_priorizadas = [(MAPA_PRIORIDADE.get(semantica.classificar_entrada(semantica.normalizar(p[0])), 99), p) for p in pares_unicos]
                tarefas_priorizadas.sort(key=lambda x: x[0])
                
                st.info(f"Otimização O(U) com Fila Inteligente Ativa: {len(pares_unicos)} rotas exclusivas.")
                
                executor_lote = st.session_state["executor_global"]
                futuros = {executor_lote.submit(embrulhar_task_paralela, (t[1], t[1][0], t[1][1])): t for t in tarefas_priorizadas}
                
                resultados_unicos = {}
                concluidos, barra_progresso, container_status = 0, st.progress(0), st.empty()
                st.session_state['logs_auditoria'] = []
                
                for f in as_completed(futuros):
                    par_id, res = f.result()
                    resultados_unicos[par_id] = res
                    concluidos += 1
                    container_status.text(f"🚀 Fila de Prioridade Assíncrona: {concluidos} / {len(pares_unicos)}")
                    barra_progresso.progress(concluidos / len(pares_unicos))
                    
                for idx, origem, destino in mapeamento_linhas:
                    res = resultados_unicos.get((origem, destino))
                    if res:
                        df.loc[idx, ['Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta', 'Fonte da Rota', 'Score da Rota', 'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem', 'Fonte Geocoding Origem', 'Endereco Oficial Origem', 'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino', 'Fonte Geocoding Destino', 'Endereco Oficial Destino', 'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)']] = [res['distancia_km'], res['tempo_str'], res['link_rota'], res['balsas'], res['linha_reta'], res['fonte_rota'], res['score_rota'], res['conf_origem'], res['score_origem'], res['distrito_origem'], res['mun_origem'], res['fonte_origem'], res['endereco_origem'], res['conf_destino'], res['score_destino'], res['distrito_destino'], res['mun_destino'], res['fonte_destino'], res['endereco_destino'], res['lat_origem'], res['lon_origem'], res['lat_destino'], res['lon_destino'], res['tempo_geo'], res['tempo_rot'], res['tempo_total']]
                        score_global = round((0.35 * res['score_origem']) + (0.35 * res['score_destino']) + (0.30 * res['score_rota']), 2)
                        df.at[idx, 'Score Final Global'] = score_global
                        df.at[idx, 'Status da Rota'] = "Excelente" if score_global >= 90 else "Boa" if score_global >= 80 else "Aceitável" if score_global >= 70 else "Revisar"
                        st.session_state['logs_auditoria'].append(res.get('xai_o', {}))
                    else: df.at[idx, 'Status da Rota'] = "Erro de Processamento"

                tempo_lote = round(time.time() - start_lote_clock, 2)
                cache_historico_lotes.set(f"lote_{start_lote_clock}", {"Data/Hora": time.strftime("%Y-%m-%d %H:%M:%S"), "Operador": nome_operador.strip() or "Operador Automático", "Linhas": len(pares_unicos), "Tempo (s)": tempo_lote, "Méd/Rota": round(tempo_lote / max(1, len(pares_unicos)), 2)}, expire=None)

                st.session_state['df_processado'] = df
                container_status.empty(); barra_progresso.empty()
                st.success("✨ Processamento em lote corporativo concluído!")
                
                df = df.reindex(columns=['Origem', 'Destino'] + novas_colunas)
                output_buffer = io.BytesIO()
                with pd.ExcelWriter(output_buffer, engine='openpyxl') as w: df.to_excel(w, index=False)
                st.session_state['planilha_pronta'] = output_buffer.getvalue()

        if 'planilha_pronta' in st.session_state:
            st.write("---"); st.balloons()
            st.download_button("📥 Baixar Planilha", data=st.session_state['planilha_pronta'], file_name="planilha_rotas_calculada.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

with tab_analytics:
    st.markdown("### 📊 Painel de KPIs e Saúde do Sistema")
    if 'df_processado' in st.session_state:
        df_kpi = st.session_state['df_processado']
        df_suc = df_kpi[~df_kpi["Status da Rota"].str.contains("Erro", na=False)]
        
        c1, c2, c3 = st.columns(3); c4, c5, c6 = st.columns(3)
        c1.metric("Total de Rotas em Lote", len(df_kpi))
        c2.metric("Rotas com Sucesso", f"{len(df_suc)} ({round((len(df_suc)/max(1, len(df_kpi)))*100, 1)}%)")
        c3.metric("Distância Média Viária", f"{round(df_suc['Distancia'].mean(), 1) if not df_suc.empty else 0} km")
        c4.metric("Tempo Médio de Geocoding", f"{round(df_kpi['Tempo Geocoding (s)'].mean(), 2)} s")
        c5.metric("Tempo Médio de Roteamento", f"{round(df_kpi['Tempo Roteamento (s)'].mean(), 2)} s")
        c6.metric("Score de Qualidade Médio", f"{round(df_suc['Score Final Global'].mean(), 1) if not df_suc.empty else 0} / 100")
        
        st.markdown("---"); st.markdown("#### 🚨 Mapa de Calor de Inconsistências (Score < 70)")
        df_err = df_kpi[df_kpi['Score Final Global'] < 70].dropna(subset=['Lat Destino', 'Lon Destino'])
        if not df_err.empty:
            hl = pdk.Layer("HeatmapLayer", data=df_err, get_position=['Lon Destino', 'Lat Destino'], aggregation='"SUM"', get_weight="100 - `Score Final Global`", radiusPixels=50)
            st.pydeck_chart(pdk.Deck(layers=[hl], initial_view_state=pdk.ViewState(latitude=-15.78, longitude=-47.92, zoom=3), map_style="mapbox://styles/mapbox/dark-v10"))
        else: st.success("Nenhuma inconsistência crítica detectada.")
            
        st.markdown("---"); st.markdown("#### ⚙️ Monitor de Saúde das APIs")
        health_data = [{"Provedor": api, "Status": "Online" if (d:=cache_api_health.get(api, {"hits": 0, "calls": 0, "falhas": 0, "tempo_total": 0.0}))["falhas"] == 0 else "Instável", "Latência": f"{round((d['tempo_total'] / max(1, d['calls'])) * 1000)} ms", "Erros": f"{round((d['falhas'] / max(1, d['calls'])) * 100, 1)}%", "Chamadas": d["calls"]} for api in ["GOOGLE_MAPS", "ARCGIS", "TOMTOM", "NOMINATIM", "PHOTON", "OVERPASS"]]
        st.dataframe(pd.DataFrame(health_data), use_container_width=True)
    else: st.info("Aguardando processamento de lote para métricas.")
        
    st.markdown("---"); st.markdown("#### 📜 Trilha de Auditoria Corporativa")
    historico = [cache_historico_lotes[k] for k in cache_historico_lotes]
    st.dataframe(pd.DataFrame(historico).sort_values(by="Data/Hora", ascending=False).reset_index(drop=True) if historico else pd.DataFrame(), use_container_width=True)

with tab_auditoria:
    st.markdown("### 🕵️ Dossiê de Auditoria (XAI - Explainable AI)")
    st.write("Explore o laudo de decisão do Ensemble Bayesiano para os últimos processamentos.")
    if 'logs_auditoria' in st.session_state and st.session_state['logs_auditoria']:
        for idx, report in enumerate(st.session_state['logs_auditoria']):
            if report:
                with st.expander(f"🔎 Relatório de Resolução: {report.get('1_entrada_original', 'N/A')}"):
                    st.json(report)
    else: st.info("Nenhum registro gerado no momento.")
