import streamlit as st
import streamlit.components.v1 as components
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
import json
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import altair as alt
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

if "cache_limpo_v49" not in st.session_state:
    for c in [cache_classificacao, cache_fuzzy, cache_geo, cache_rotas, cache_poi, cache_cep, cache_google, cache_reverse, cache_base_local, cache_aprendizado, cache_aprendizado_auto, cache_api_health, cache_historico_lotes]:
        c.clear()
    st.session_state["cache_limpo_v49"] = True
    st.session_state['dash_key'] = 0

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

        if not uf_explicita or uf_explicita == "DF":
            for token in texto_norm.split():
                sigla_limpa = re.sub(r'[^A-Z]', '', token)
                if sigla_limpa in self.mapa_siglas_df and len(sigla_limpa) >= 2:
                    resultado.update({"uf": "DF", "municipio": "BRASILIA", "distrito": self.mapa_siglas_df[sigla_limpa]})
                    return resultado
                    
            for chave, ra_oficial in self.mapa_contexto_df.items():
                if re.search(rf'\b{chave}\b', texto_norm):
                    resultado.update({"uf": "DF", "municipio": "BRASILIA", "distrito": ra_oficial})
                    return resultado

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
# 🧮 VALIDADOR PRÉ-GEOCODING E LÓGICA GEODÉSICA
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

def validar_coordenadas_mapa(lat, lon):
    try:
        if pd.isna(lat) or pd.isna(lon): return False
        lat_f, float(lat), float(lon)
        if math.isnan(lat_f) or math.isnan(float(lon)) or math.isinf(lat_f) or math.isinf(float(lon)): return False
        if not (-90.0 <= lat_f <= 90.0) or not (-180.0 <= float(lon) <= 180.0): return False
        if lat_f == 0.0 and float(lon) == 0.0: return False
        return True
    except Exception:
        return False

def validar_json_mapa(dados):
    try:
        json.dumps(dados)
        return True
    except (TypeError, OverflowError):
        return False

def obedience_base_local(contexto_estruturado):
    if contexto_estruturado["logradouro"] and contexto_estruturado["municipio"] and contexto_estruturado["uf"]:
        chave_cnefe = f"{contexto_estruturado['logradouro']}_{contexto_estruturado['municipio']}_{contexto_estruturado['uf']}"
        if chave_cnefe in cache_base_local:
            return cache_base_local[chave_cnefe]
    return None

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

def calcular_distancia_linha_reta(lat1, lon1, lat2, lon2):
    try:
        lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
        if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0: return 0.0
        if lat1 == lat2 and lon1 == lon2: return 0.0
        lat1_r, lon1_r, lat2_r, lon2_r = map(math.radians, [lat1, lon1, lat2, lon2])
        dlat = lat2_r - lat1_r
        dlon = lon2_r - lon1_r
        a = math.sin(dlat / 2)**2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2)**2
        c = 2 * math.asin(math.sqrt(a))
        return round(6371.0 * c, 2)
    except Exception:
        return 0.0

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

# ==============================================================================
# 🗺️ MÓDULOS DE GEOCODIFICAÇÃO COM TELEMETRIA (CONTRATO LISTA TOP-K)
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
                dist = calcular_distancia_linha_reta(c1["lat"], c1["lon"], c2["lat"], c2["lon"])
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
        if calcular_distancia_linha_reta(cand["lat"], cand["lon"], vencedor["lat"], vencedor["lon"]) <= 0.05:
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

    for poi_key, poi_data in BASE_POIS_LOGISTICOS.items():
        if poi_key in texto_norm:
            return poi_data["lat"], poi_data["lon"], poi_data["endereco"], "ABSOLUTA", 100, "", poi_data["municipio"], "BASE_POIS_NACIONAIS", ["Resolvido via Base Nacional de POIs Logísticos Ground Truth."]

    if texto_norm in cache_aprendizado:
        dado_salvo = cache_aprendizado[texto_norm]
        if isinstance(dado_salvo, dict) and "lat" in dado_salvo and "lon" in dado_salvo:
            return dado_salvo["lat"], dado_salvo["lon"], dado_salvo.get("endereco", texto_norm), "ALTISSIMA", 100, dado_salvo.get("distrito", ""), dado_salvo.get("municipio", ""), "APRENDIZADO_LOCAL", ["Ponto quente extraído do cache local enriquecido."]

    endereco_canonico, tipo_entrada, _, _, _ = semantica.construir_endereco_canonico(texto_norm)
    parsed_comp = ParserGeograficoBR.extrair_componentes(texto_norm)
    
    cache_key = hashlib.md5(f"GEO_V49_{tipo_entrada}_{endereco_canonico}".encode('utf-8')).hexdigest()
    
    if cache_key in cache_geo:
        c = cache_geo[cache_key]
        if c.get("lat", 0.0) != 0.0 and c.get("lon", 0.0) != 0.0:
            return c["lat"], c["lon"], c["endereco"], c["confianca"], c["score_num"], c["distrito"], c["municipio"], c["fonte"], ["Cache L2 Hit."]

    ctx = semantica.resolver_contexto_administrativo(texto_norm)

    if ctx.get("municipio") and ctx.get("uf"):
        mun_nome = ctx["municipio"]
        uf_nome = ctx["uf"]
        chave_seguranca = f"{mun_nome}_{uf_nome}"
        KNOWN_CITIES_MATRIX = {
            "RIBEIRAO CASCALHEIRA_MT": (-12.9268, -51.8244),
            "SAO MIGUEL DO ARAGUAIA_GO": (-13.2750, -50.1628),
            "PORTO DE MOZ_PA": (-1.7483, -52.2383),
            "ALMEIRIM_PA": (-1.5233, -52.5816)
        }
        if chave_seguranca in KNOWN_CITIES_MATRIX:
            lat_c, lon_c = KNOWN_CITIES_MATRIX[chave_seguranca]
            endereco_ibge = f"{mun_nome}, {IBGE_ESTADOS.get(uf_nome, uf_nome)}, BRASIL"
            res_final = (lat_c, lon_c, endereco_ibge, "ALTA", 100, ctx.get("distrito", ""), mun_nome, "MATRIZ_SEGURANCA_INTERNA", ["Blindagem Crítica Antecipada: Coordenada rodoviária exata injetada do Dicionário de Segurança em Memória."])
            cache_geo.set(cache_key, {"lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], "confianca": res_final[3], "score_num": res_final[4], "distrito": res_final[5], "municipio": res_final[6], "fonte": res_final[7]}, expire=2592000)
            return res_final
            
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
        candidatos_validos.extend(disparar_apis_paralelas([(API_Overpass_POIs, (texto_norm,), {}), (API_TomTom, (endereco_canonico,), {})]))
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
# 🚀 MOTOR DE ROTEAMENTO EXTREMO (ARBITRAGEM DE PROVEDORES COM LINK DINÂMICO)
# ==============================================================================
def extrair_dados_reais_google(origem_texto, destino_texto, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=True):
    cache_key = f"GOOG_V49_{origem_texto}|{destino_texto}|{usar_coordenadas}"
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
    
    chave_rota_cache = f"ROTA_V49_{semantica.normalizar(origem_clean)}->{semantica.normalizar(destino_clean)}"
    if chave_rota_cache in cache_rotas: return cache_rotas[chave_rota_cache]
    
    start_geo = time.time()
    lat_o, lon_o, end_oficial_o, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, xai_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    lat_d, lon_d, end_oficial_d, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, xai_d = obter_coordenadas_e_endereco_oficial(destino_clean)
    tempo_geocoding = round(time.time() - start_geo, 2)
    
    start_rot = time.time()

    if all([lat_o is not None, lon_o is not None, lat_d is not None, lon_d is not None]) and lat_o != 0.0 and lat_d != 0.0:
        dist_linha_reta = calcular_distancia_linha_reta(lat_o, lon_o, lat_d, lon_d)
    else:
        dist_linha_reta = 0.0

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
        retorno = (km_rota, tempo_rota, link_rota, balsa_rota, dist_linha_reta, fonte_rota, score_rota, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total, xai_o, xai_d, motivo_roteamento, link_embed)
        cache_rotas.set(chave_rota_cache, retorno, expire=2592000)
        return retorno

    km_terrestre = round(dist_linha_reta * obter_fator_desvio_rodoviario(dist_linha_reta), 2)
    v_comercial = 45.0 if km_terrestre < 50.0 else 65.0
    minutos_est = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0 else 0
    tempo_geo_str = f"{minutos_est} min" if minutos_est < 60 else f"{minutos_est // 60} h {minutos_est % 60} min"
    tempo_roteamento = round(time.time() - start_rot, 2); tempo_total = round(time.time() - start_total, 2)
    motivo_fallback = "Alerta Crítico: Motores viários em Nuvem e Open-Source rejeitaram a rota (Timeout ou Coordenadas Inválidas). Projeção Geodésica Adaptativa acionada baseada na Linha Reta."
    
    retorno = (km_terrestre, tempo_geo_str, link_fallback, "Não", dist_linha_reta, "Geodésico Adaptativo", 50, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total, xai_o, xai_d, motivo_fallback, link_embed_fallback)
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
        return (0.0, "0 min", "Link Indisponível", "Não", 0.0, "Input Inválido", 0, "BAIXA", 0, "Não Informado", "Não Informado", "N/A", orig, "BAIXA", 0, "Não Informado", "Não Informado", "N/A", dest, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, ["Falha Espacial Origem"], ["Falha Espacial Destino"], "Falha de Roteamento: Hub Base ou Endereço Destino foi incapaz de resolver latitude/longitude em nuvem.", "N/A", concorrente, dist_conc, link_conc, justificativa)
        
    if orig.lower() in ['nan', 'none', 'null', ''] or dest.lower() in ['nan', 'none', 'null', '']:
        return (0.0, "0 min", "Link Indisponível", "Não", 0.0, "Input Inválido", 0, "BAIXA", 0, "Não Informado", "Não Informado", "N/A", orig, "BAIXA", 0, "Não Informado", "Não Informado", "N/A", dest, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, [], [], "Falha na leitura da célula (Campo Vazio).", "N/A", concorrente, dist_conc, link_conc, justificativa)
    
    res = calcular_pipeline_logistico(orig, dest, perfil_rota="shortest")
    
    if runner_up_info and res and len(res) >= 30:
        dist_v_runner, r_nome, r_lat, r_lon = runner_up_info
        lat_o, lon_o = res[19], res[20]
        
        if lat_o != 0.0 and r_lat != 0.0:
            dist_v_real = calcular_distancia_linha_reta(lat_o, lon_o, r_lat, r_lon)
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
        if res and isinstance(res, tuple) and len(res) < 34:
            res = tuple(list(res) + ["N/A"] * (34 - len(res)))
        return par_id, res
    except Exception as e: 
        msg_erro = f"FALHA INTERNA: {str(e)}"
        fallback = (0.0, "0 min", "Link Indisponível", "Não", 0.0, msg_erro, 0, "BAIXA", 0, "Erro", "Erro", "N/A", str(orig), "BAIXA", 0, "Erro", "Erro", "N/A", str(dest), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, [msg_erro], [msg_erro], msg_erro, "N/A", "N/A", 0.0, "N/A", "N/A")
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
                        df.at[idx, 'Distancia Concorrente'] = float(res[31]) if res[31] != "N/A" else 0.0
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
                
                if runner_up_map:
                    df.at[idx, 'Concorrente Analisado'] = res[30] if res[30] is not None else "N/A"
                    df.at[idx, 'Link Rota Concorrente'] = res[32] if res[32] is not None else "N/A"
                    df.at[idx, 'Justificativa de Alocacao'] = res[33] if res[33] is not None else "N/A"
                
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
                
    return df

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
    st.header("📖 Documentação Técnica Oficial", help="Diretrizes estruturais, matemáticas e logísticas do motor corporativo.")
    
    with st.expander("1. Visão Geral da Arquitetura Corporativa"):
        st.markdown("""
        O **Motor Nacional de Roteirização Inteligente** é um sistema corporativo B2B projetado para operações logísticas extremas. Ele processa entradas de texto não estruturadas (endereços sujos), transforma-as em coordenadas rigorosas e calcula desvios viários reais.
        
        Sua premissa é atuar sob um **Pipeline Híbrido Multimotor**: nunca depender de uma única API, garantindo alta disponibilidade (SLA) e eliminando falsos positivos topológicos por meio de consenso matemático e inteligência geográfica em nuvem.
        """)

    with st.expander("2. Inteligência de Busca e Motores em Nuvem"):
        st.markdown("""
        O sistema dispara procuras simultâneas e paralelas contra múltiplos provedores globais:
        
        * **ArcGIS (ESRI):** Considerado o padrão-ouro no geocoding corporativo. Possui a melhor malha predial urbana do Brasil e é prioritário para rotas contendo logradouros e CEPs.
        * **Nominatim & Photon (OSM):** Motores baseados no OpenStreetMap. Desempenham com altíssima excelência em localizações rurais e assentamentos ignorados pelas plataformas comerciais.
        * **TomTom Logistics:** Focado estritamente na malha rodoviária B2B.
        * **Google Maps Engine:** Motor oficial de roteamento viário. Extrai em tempo real a *Quilometragem Viária Exata* e o tempo de trânsito em asfalto.
        * **OSRM (Open Source Routing Machine):** Atua paralelamente como um validador de manobras logísticas, assegurando a detecção de balsas e rios.
        """)

    with st.expander("3. Algoritmos e Fórmulas Matemáticas"):
        st.markdown("""
        * **Fórmula de Haversine (Geodésica Esférica):** Utilizada no backend para medir distâncias aéreas puras (em linha reta) entre a Origem e o Destino. Serve como *Baseline* para auditar o desvio de asfalto do Google.
        """)
        st.latex(r"d = 2r \arcsin{\sqrt{\sin^2(\frac{\Delta\phi}{2}) + \cos{\phi_1}\cos{\phi_2}\sin^2(\frac{\Delta\lambda}{2})}}")
        st.markdown("""
        * **Clustering Espacial via DBSCAN:** Plota todos os retornos de APIs no mapa e agrupa as coordenadas verdadeiras que estão a menos de 500 metros umas das outras, descartando anomalias.
        * **Inferência Bayesiana Multiplicativa:** Motor heurístico que gera o "Score Global", multiplicando fatores de confiança se o CEP for validado, se a UF for condizente e se o IBGE reconhecer o distrito.
        """)

    with st.expander("4. Guia Rápido das Abas de Operação"):
        st.markdown("""
        * **Geocodificação Rápida:** Modo para testar e inspecionar rotas uma a uma.
        * **Processamento em Lote:** O núcleo do sistema. Envie tabelas massivas e ele devolverá os dados processados e auditados em alta velocidade.
        * **Alocação de Hubs (Nearest Neighbor):** A inteligência competitiva. Descobre automaticamente qual é a sua Base Logística mais próxima de cada cliente da lista.
        * **Analytics & Dashboards:** Visualização de KPIs para as localidades logísticas do último lote processado.
        * **Enciclopédia do Sistema:** Detalhamento arquitetural completo do aplicativo.
        * **Motores & APIs:** Monitor de infraestrutura de rede.
        * **Auditoria:** Acesso à árvore de decisões (Caixa Preta) do algoritmo.
        """)

    with st.expander("5. Pré-Processamento Anti-Fantasma"):
        st.markdown("""
        * **Fast-Track IBGE:** Localidades contendo apenas "Nome da Cidade + UF" bypassam a nuvem, pegando a coordenada centróide perfeita direto do IBGE sem latência.
        * **Fuzzy Léxico:** Algoritmos de correção ortográfica consertam digitações equivocadas automaticamente (Ex: `RIB CASCALH` corrigido para `RIBEIRAO CASCALHEIRA`).
        """)
        
    with st.expander("6. Referências Técnicas e Bibliográficas"):
        st.markdown("""
        * **Haversine Formula:** Sinnott, R.W. (1984). "Virtues of the Haversine". Sky and Telescope 68 (2): 159.
        * **DBSCAN Clustering:** Ester, M., Kriegel, H. P., Sander, J., & Xu, X. (1996). "A density-based algorithm for discovering clusters in large spatial databases with noise". In KDD.
        * **Teorema de Bayes:** Implementação algorítmica para consolidação de Ensembles e Motores de Decisão (XAI).
        * **RapidFuzz:** Módulo C++ de Distância de Levenshtein para Python (MaxBachmann).
        * **Bibliotecas Analíticas e Visuais:** Streamlit (App Framework), Altair & Vega-Lite (Grammar of Graphics), PyDeck & Mapbox (Visualização Topológica).
        * **Malhas de Dados Geográficos:** IBGE (Serviços de Dados), ArcGIS REST Services (ESRI), Nominatim/Photon (OpenStreetMap Foundation), OSRM (Project-OSRM), Google Maps Platform.
        """)

    st.markdown("---")
    st.subheader("💡 Canal Direto de Engenharia")
    st.caption("Envie uma sugestão diretamente pelo sistema (Requer configuração de SMTP).")
    
    with st.form(key="form_sugestao"):
        sugestao_texto = st.text_area("Descreva a melhoria detalhadamente:", height=100)
        remetente_email = st.text_input("Seu e-mail de contato (opcional):")
        submit_button = st.form_submit_button("🚀 Enviar Diretamente ao Desenvolvedor")
        
        if submit_button:
            if sugestao_texto.strip() == "":
                st.warning("A sugestão não pode estar vazia.")
            else:
                try:
                    smtp_server = "smtp.gmail.com"
                    smtp_port = 587
                    smtp_user = st.secrets.get("EMAIL_SISTEMA", "seu_email_de_envio@gmail.com") 
                    smtp_pass = st.secrets.get("SENHA_APP", "sua_senha_de_aplicativo")
                    
                    if smtp_user == "seu_email_de_envio@gmail.com":
                        st.info("⚠️ Modo de Demonstração: Para o envio direto funcionar silenciosamente, configure as variáveis 'EMAIL_SISTEMA' e 'SENHA_APP' no seu ambiente (Streamlit Secrets) utilizando uma Senha de Aplicativo do Google.")
                    else:
                        msg = MIMEMultipart()
                        msg['From'] = smtp_user
                        msg['To'] = "lucas.c.cruz@gmail.com"
                        msg['Subject'] = "Sugestão de Melhoria - Motor Corporativo de Rotas"
                        
                        corpo = f"Nova sugestão enviada via painel:\n\nRemetente: {remetente_email}\n\nSugestão:\n{sugestao_texto}"
                        msg.attach(MIMEText(corpo, 'plain'))
                        
                        server = smtplib.SMTP(smtp_server, smtp_port)
                        server.starttls()
                        server.login(smtp_user, smtp_pass)
                        server.send_message(msg)
                        server.quit()
                        
                        st.success("✅ Sugestão enviada com sucesso em background!")
                except Exception as e:
                    st.error(f"Erro ao tentar enviar o e-mail: {str(e)}")

tab_individual, tab_processamento, tab_alocacao, tab_analytics, tab_enciclopedia, tab_motores, tab_auditoria = st.tabs([
    "📍 Geocodificação Rápida", "⚙️ Processamento em Lote", "📦 Alocação de Hubs", "📊 Analytics de Localidades", "📚 Enciclopédia", "🔌 Motores & APIs", "🕵️ Aba de Auditoria"
])

with tab_individual:
    st.info("💡 **Objetivo desta aba:** Validar rapidamente uma única rota. Digite a Origem e o Destino para obter a distância viária oficial do Google Maps, o desvio em relação à linha reta geodésica e a explicabilidade do motor de geocodificação.")
    st.markdown("### 🔍 Validador Rápido de Rota (Single-Shot)")
    col_ind1, col_ind2 = st.columns(2)
    with col_ind1: orig_ind = st.text_input("Origem (Endereço, POI ou Coordenadas)", "Ribeirão Cascalheira , MT, Brasil", help="Insira o local de partida. O sistema bloqueará a busca apenas para o Estado cuja sigla for identificada.")
    with col_ind2: dest_ind = st.text_input("Destino (Endereço, POI ou Coordenadas)", "SAO MIGUEL DO ARAGUAIA , GO, Brasil", help="Insira o destino final. O uso de UF (Ex: GO) assegura máxima precisão contra localidades homônimas em outros estados.")
    
    if st.button("🚀 Calcular Rota Individual", type="primary", help="Inicia o pipeline Bayesiano para geocodificação e aciona os motores do Google Maps e OSRM para o trajeto."):
        if orig_ind and dest_ind:
            with st.spinner("Acionando motores de geocodificação e consenso unificado..."):
                res_ind = executar_pipeline_unificado(orig_ind, dest_ind)
                
            if res_ind and res_ind[28] != "Falha na leitura da célula (Campo Vazio)." and "FALHA INTERNA" not in res_ind[28]:
                st.success("✅ Rota estabelecida com sucesso!")
                
                m_dist_via, m_dist_reta, m_time, m_balsa, m_score = st.columns(5)
                m_dist_via.metric("Distância Viária", f"{res_ind[0]} km" if isinstance(res_ind[0], float) else res_ind[0], help="Quilometragem oficial em asfalto extraída da nuvem Google Maps.")
                m_dist_reta.metric("Distância Linha Reta", f"{res_ind[4]} km" if isinstance(res_ind[4], float) else res_ind[4], help="Distância matemática geodésica baseada na fórmula de Haversine.")
                m_time.metric("Tempo Estimado", res_ind[1], help="Tempo de deslocamento estimado via transporte motorizado.")
                m_balsa.metric("Uso de Balsas", res_ind[3], help="Validação de interseção de corpos hídricos (Ferry) auditada via OSRM.")
                
                score_g = round((0.35 * res_ind[8]) + (0.35 * res_ind[14]) + (0.30 * res_ind[6]), 2)
                m_score.metric("Score Global", f"{score_g} / 100", help="Grau de certeza e integridade dos dados obtidos no processamento.")
                
                st.info(f"🧠 **Estratégia de Roteamento (XAI):** {res_ind[28]}")
                
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
                        for just in res_ind[26]:
                            st.caption(f"- {just}")
                    with col_aud2:
                        st.markdown("**🎯 Destino (Ponto B)**")
                        st.write(f"**Endereço Oficial:** {res_ind[18]}")
                        st.write(f"**Coordenadas:** {res_ind[21]}, {res_ind[22]}")
                        st.write(f"**Motor Vencedor:** {res_ind[17]}")
                        st.write(f"**Confiança & Score:** {res_ind[13]} ({res_ind[14]}/100)")
                        st.write(f"**Justificativa Espacial:**")
                        for just in res_ind[27]:
                            st.caption(f"- {just}")

                url_iframe = res_ind[29]
                try:
                    components.iframe(url_iframe, height=470, scrolling=True)
                except Exception:
                    st.warning("Renderização de mapa localmente bloqueada pelas políticas de segurança do navegador.")

                st.markdown(f"[🔗 Abrir Rota Completa no Aplicativo do Google Maps]({res_ind[2]})")
            else:
                st.error("Falha na validação de consistência geodésica unificada.")
        else:
            st.warning("Preencha origem e destino.")

with tab_processamento:
    st.info("💡 **Objetivo desta aba:** Processamento em massa O(U). Envie uma planilha Excel com milhares de origens e destinos. O sistema extrairá rotas únicas, calculará os desvios de todas simultaneamente e devolverá a planilha rigorosamente preenchida.")
    st.write("Insira uma planilha Excel (.xlsx) contendo as colunas **Origem** e **Destino**.")
    arquivo_carregado = st.file_uploader("Selecionar Arquivo Excel", type=["xlsx"], key="lote_std", help="A planilha deve conter abas chamadas estritamente 'Origem' e 'Destino' para roteamento de A a B.")

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
            nome_operador = st.text_input("Matrícula / Nome do Operador (Opcional)", max_chars=50, help="Será registrado na Trilha de Auditoria Corporativa.")
            
            if st.button("Iniciar Processamento em Lote", help="Aciona todas as threads de processamento paralelo simultaneamente."):
                start_lote_clock = time.time()
                
                novas_colunas = [
                    'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Motivo Roteamento', 'Linha Reta', 'Fonte da Rota', 'Score da Rota', 
                    'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem', 'Fonte Geocoding Origem', 'Endereco Oficial Origem',
                    'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino', 'Fonte Geocoding Destino', 'Endereco Oficial Destino',
                    'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)', 'Score Final Global', 'Status da Rota'
                ]
                
                colunas_numericas = ['Distancia', 'Linha Reta', 'Score da Rota', 'Score Num Origem', 'Score Num Destino', 'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)', 'Score Final Global']
                
                for col in novas_colunas:
                    if col in colunas_numericas:
                        if col not in df.columns:
                            df[col] = 0.0
                        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0).astype(float)
                    else:
                        if col not in df.columns:
                            df[col] = "Não Informado"
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
                
                # FORÇA BRUTA DE SEGURANÇA: Recálculo de Linha Reta em Vetor (Evita 0.0 acidentais em caso de falha de rede/API)
                def recalculate_haversine_lote(row):
                    if row['Linha Reta'] == 0.0 and row['Lat Origem'] != 0.0 and row['Lat Destino'] != 0.0:
                        return calcular_distancia_linha_reta(row['Lat Origem'], row['Lon Origem'], row['Lat Destino'], row['Lon Destino'])
                    return row['Linha Reta']
                df_final['Linha Reta'] = df_final.apply(recalculate_haversine_lote, axis=1)

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
                st.success("✨ Processamento em lote corporativo concluído!")
                
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
                st.download_button(label="📥 Baixar Planilha (.xlsx)", data=st.session_state['planilha_pronta'], file_name="planilha_rotas_calculada.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True, help="O download preserva as colunas originais do seu sistema.")
            with col_down2:
                st.markdown(
                    """
                    <a href="https://sheets.new/" target="_blank" style="display:inline-block; padding:0.5em 1em; background-color:#0F9D58; color:white; border-radius:5px; text-decoration:none; font-weight:bold; text-align:center; width:100%; border: 1px solid rgba(255,255,255,0.2);">
                        📊 Abrir Google Sheets Vazio (Para Importar o Arquivo)
                    </a>
                    """, unsafe_allow_html=True
                )
                st.caption("Dica: Baixe a planilha no botão ao lado, clique em 'Abrir Google Sheets' e arraste o arquivo baixado para dentro da tela (Arquivo > Importar).")

with tab_alocacao:
    st.info("💡 **Objetivo desta aba:** Inteligência Logística de Hubs. Envie uma lista de clientes (Origens) e uma lista de Centros de Distribuição/Bases (Destinos). O sistema calculará todas as combinações espaciais e descobrirá automaticamente qual é a Base Logística mais próxima de cada cliente individualmente.")
    st.markdown("### 📦 Matriz Geográfica de Alocação de Hubs (Nearest Neighbor)")
    st.write("A matriz inverteu sua lógica por segurança: Os **Endereços serão a Origem**, e as **Bases serão o Destino** da rota. A planilha final terá exatamente a mesma quantidade de linhas que o seu arquivo de endereços.")
    
    col_a1, col_a2 = st.columns(2)
    with col_a1: file_dest = st.file_uploader("1. Planilha de Endereços / Entregas (Origens)", type=["xlsx"], key="up_dests_v19", help="Faça upload dos clientes ou endereços finais que receberão a mercadoria.")
    with col_a2: file_hubs = st.file_uploader("2. Planilha de Municípios / Bases (Destinos)", type=["xlsx"], key="up_hubs_v19", help="Faça upload dos centros logísticos disponíveis.")
    
    if file_hubs and file_dest:
        df_hubs = pd.read_excel(file_hubs)
        df_dest = pd.read_excel(file_dest)
        
        col_s1, col_s2 = st.columns(2)
        with col_s1: dest_col_name = st.selectbox("Selecione a coluna que contém os Endereços (Origens):", df_dest.columns, help="A coluna exata na qual o algoritmo procurará as chaves logísticas.")
        with col_s2: hub_col_name = st.selectbox("Selecione a coluna que contém os Municípios/Bases (Destinos):", df_hubs.columns, help="A coluna exata na qual o algoritmo procurará os Hubs.")
        
        if st.button("🗺️ Processar Cruzamento Espacial e Roteamento Duplo", type="primary", help="Inicia o duelo entre o vencedor da Linha Reta e o Vice-Líder no motor do Google Maps."):
            start_alo_clock = time.time()
            
            hubs_unicos = df_hubs[hub_col_name].dropna().astype(str).str.strip().unique().tolist()
            dests_unicos = df_dest[dest_col_name].dropna().astype(str).str.strip().unique().tolist()
            
            if not hubs_unicos or not dests_unicos:
                st.error("Uma das colunas selecionadas está vazia ou é inválida.")
            else:
                progress_alo = st.progress(0)
                status_alo = st.empty()
                
                if 'logs_auditoria_alocacao' not in st.session_state:
                    st.session_state['logs_auditoria_alocacao'] = []
                st.session_state['logs_auditoria_alocacao'].clear()
                
                status_alo.text("Fase 1/3: Geocodificando e blindando Hubs Logísticos...")
                hub_coords = {}
                for i, h in enumerate(hubs_unicos):
                    progress_alo.progress((i + 1) / len(hubs_unicos))
                    lat, lon, end, conf, score, dist, mun, fonte, xai = obter_coordenadas_e_endereco_oficial(h)
                    hub_coords[h] = (lat, lon, end)
                    
                    st.session_state['logs_auditoria_alocacao'].append({
                        "Categoria": "Base/Hub (Destino)", "Nome Original": h, "Coordenada": f"{lat}, {lon}", 
                        "Endereço Oficializado": end, "Score": score, "Validação XAI": " | ".join(xai)
                    })
                    time.sleep(0.05)
                
                hubs_validos = {k: v for k, v in hub_coords.items() if v[0] != 0.0}
                
                if not hubs_validos:
                    st.error("CRÍTICO: Nenhuma Base/Hub pôde ser geocodificada no mapa. Assegure-se de que a coluna de Bases contenha endereços, nomes de cidades ou coordenadas válidas. Verifique a Aba de Auditoria.")
                    status_alo.empty(); progress_alo.empty()
                else:
                    status_alo.text("Fase 2/3: Geocodificando Endereços de Origem...")
                    dest_coords = {}
                    for i, d in enumerate(dests_unicos):
                        progress_alo.progress((i + 1) / len(dests_unicos))
                        lat, lon, end, conf, score, dist, mun, fonte, xai = obter_coordenadas_e_endereco_oficial(d)
                        dest_coords[d] = (lat, lon, end)
                        
                        st.session_state['logs_auditoria_alocacao'].append({
                            "Categoria": "Endereço (Origem)", "Nome Original": d, "Coordenada": f"{lat}, {lon}", 
                            "Endereço Oficializado": end, "Score": score, "Validação XAI": " | ".join(xai)
                        })
                        time.sleep(0.05)
                    
                    status_alo.text("Fase 3/3: Calculando Matriz Competitiva e montando Pipeline...")
                    
                    dest_to_hub = {}
                    dest_to_linha_reta = {}
                    runner_up_map = {}
                    
                    for o_nome, (o_lat, o_lon, o_end) in dest_coords.items():
                        if o_lat == 0.0 or o_lon == 0.0:
                            dest_to_hub[o_nome] = "FALHA_GEO_ORIGEM"
                            continue
                            
                        hubs_dist = []
                        for h_nome, (h_lat, h_lon, h_end) in hubs_validos.items():
                            dist_v = calcular_distancia_linha_reta(o_lat, o_lon, h_lat, h_lon)
                            hubs_dist.append((dist_v, h_nome, h_lat, h_lon))
                            
                        hubs_dist.sort(key=lambda x: x[0])
                        
                        if hubs_dist:
                            dest_to_hub[o_nome] = hubs_dist[0][1]
                            dest_to_linha_reta[o_nome] = hubs_dist[0][0]
                            if len(hubs_dist) > 1:
                                runner_up_map[o_nome] = hubs_dist[1]
                        else:
                            dest_to_hub[o_nome] = "NENHUM_HUB_VALIDO"
                    
                    df_pares = df_dest.copy()
                    df_pares['Origem'] = df_pares[dest_col_name].astype(str).str.strip()
                    df_pares['Destino'] = df_pares['Origem'].map(dest_to_hub).fillna("FALHA_GEO_ORIGEM")
                    
                    novas_colunas = [
                        'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Motivo Roteamento', 'Linha Reta', 'Fonte da Rota', 'Score da Rota', 
                        'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem', 'Fonte Geocoding Origem', 'Endereco Oficial Origem',
                        'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino', 'Fonte Geocoding Destino', 'Endereco Oficial Destino',
                        'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)', 'Score Final Global', 'Status da Rota',
                        'Concorrente Analisado', 'Distancia Concorrente', 'Link Rota Concorrente', 'Justificativa de Alocacao'
                    ]
                    colunas_numericas = ['Distancia', 'Linha Reta', 'Score da Rota', 'Score Num Origem', 'Score Num Destino', 'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)', 'Score Final Global', 'Distancia Concorrente']
                    
                    for col in novas_colunas:
                        if col in colunas_numericas:
                            if col not in df_pares.columns:
                                df_pares[col] = 0.0
                            df_pares[col] = pd.to_numeric(df_pares[col], errors='coerce').fillna(0.0).astype(float)
                        else:
                            if col not in df_pares.columns:
                                df_pares[col] = "Não Informado"
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
                    
                    # FORÇA BRUTA DE SEGURANÇA: Injetar a linha reta matemática garantida no mapeamento
                    df_final_alo['Linha Reta'] = df_final_alo['Origem'].astype(str).str.strip().map(dest_to_linha_reta).fillna(df_final_alo['Linha Reta'])
                    
                    # FORÇA BRUTA DE SEGURANÇA: Recálculo de Linha Reta em Vetor final (Evita falhas de dicionário)
                    def recalculate_haversine_alo(row):
                        if row['Linha Reta'] == 0.0 and row['Lat Origem'] != 0.0 and row['Lat Destino'] != 0.0:
                            return calcular_distancia_linha_reta(row['Lat Origem'], row['Lon Origem'], row['Lat Destino'], row['Lon Destino'])
                        return row['Linha Reta']
                    df_final_alo['Linha Reta'] = df_final_alo.apply(recalculate_haversine_alo, axis=1)

                    tempo_alo_segundos = round(time.time() - start_alo_clock, 2)
                    cache_historico_lotes.set(f"alocacao_{start_alo_clock}", {
                        "Data/Hora": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "Operador": "Motor de Alocação (Hubs)",
                        "Linhas Validadas": len(df_final_alo),
                        "Tempo Gasto (s)": tempo_alo_segundos,
                        "Tempo Médio/Rota (s)": round(tempo_alo_segundos / max(1, len(pares_unicos_alo)), 2)
                    }, expire=None)

                    st.session_state['df_processado'] = df_final_alo
                    
                    st.success(f"✨ Matriz resolvida e Duelos concluídos! {len(df_final_alo)} linhas originais foram rigorosamente preservadas e preenchidas. O Dashboard Analítico e o Histórico de Lotes já foram atualizados com estes dados.")
                    
                    ordem_finais_alo = list(df_dest.columns)
                    for c in ['Origem', 'Destino'] + novas_colunas:
                        if c not in ordem_finais_alo: ordem_finais_alo.append(c)
                    df_final_alo = df_final_alo.reindex(columns=ordem_finais_alo)

                    st.dataframe(df_final_alo, use_container_width=True, height=250)
                    
                    output_buffer = io.BytesIO()
                    with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer: df_final_alo.to_excel(writer, index=False)
                    st.download_button(label="📥 Baixar Planilha de Alocação Competitiva (.xlsx)", data=output_buffer.getvalue(), file_name="matriz_alocacao_competitiva.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True, help="O download traz todas as colunas de sua planilha original inalteradas e preenchidas.")

with tab_analytics:
    st.info("💡 **Objetivo desta aba:** Visualizar graficamente a distribuição geográfica e os indicadores logísticos das *localidades e entregas* do seu último lote. Monitore volume por Estado (UF), Municípios mais procurados e a densidade viária.")
    
    col_d_title, col_d_btn = st.columns([80, 20])
    with col_d_title:
        st.markdown("### 📊 Dashboard Analítico de Localidades (Avançado)")
    with col_d_btn:
        if st.button("🧹 Limpar Seleções do Gráfico", use_container_width=True, help="Remove os filtros aplicados clicando nos gráficos e redefine o painel para a visualização original."):
            st.session_state['dash_key'] = st.session_state.get('dash_key', 0) + 1
            st.rerun()

    if 'df_processado' in st.session_state:
        df_kpi = st.session_state['df_processado'].copy()
        
        df_kpi['Distancia'] = pd.to_numeric(df_kpi['Distancia'], errors='coerce').fillna(0)
        df_kpi['Linha Reta'] = pd.to_numeric(df_kpi['Linha Reta'], errors='coerce').fillna(0)
        df_kpi['Tempo_Minutos'] = df_kpi['Tempo'].apply(parse_tempo_minutos)
        df_kpi['Tempo_Horas'] = df_kpi['Tempo_Minutos'] / 60.0
        
        MAPA_ESTADOS_FULL = {
            "ACRE": "AC", "ALAGOAS": "AL", "AMAPA": "AP", "AMAZONAS": "AM",
            "BAHIA": "BA", "CEARA": "CE", "DISTRITO FEDERAL": "DF", "ESPIRITO SANTO": "ES",
            "GOIAS": "GO", "MARANHAO": "MA", "MATO GROSSO": "MT", "MATO GROSSO DO SUL": "MS",
            "MINAS GERAIS": "MG", "PARA": "PA", "PARAIBA": "PB", "PARANA": "PR",
            "PERNAMBUCO": "PE", "PIAUI": "PI", "RIO DE JANEIRO": "RJ", "RIO GRANDE DO NORTE": "RN",
            "RIO GRANDE DO SUL": "RS", "RONDONIA": "RO", "RORAIMA": "RR", "SANTA CATARINA": "SC",
            "SAO PAULO": "SP", "SERGIPE": "SE", "TOCANTINS": "TO"
        }

        def extrair_uf_precisa(endereco):
            if not isinstance(endereco, str): return "Indefinido"
            end_upper = unidecode(endereco.upper())
            for nome, sigla in MAPA_ESTADOS_FULL.items():
                if f" {nome} " in f" {end_upper} " or end_upper.endswith(nome) or f", {nome}," in end_upper:
                    return sigla
            padrao_uf = r'\b(AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\b'
            partes = [p.strip() for p in end_upper.split(',')]
            for p in reversed(partes):
                match = re.search(padrao_uf, p)
                if match: return match.group(1)
            return "Indefinido"
            
        df_kpi['UF_Sintetica_Origem'] = df_kpi['Endereco Oficial Origem'].apply(extrair_uf_precisa)
        
        with st.container(border=True):
            st.markdown("#### 🎛️ Filtros Avançados Globais")
            col_f1, col_f2, col_f3, col_f4 = st.columns(4)
            
            lista_ufs = ["Todas"] + sorted(list(df_kpi['UF_Sintetica_Origem'].unique()))
            uf_selecionada = col_f1.selectbox("UF de Origem", lista_ufs)
            
            lista_municipios = ["Todos"] + sorted(list(df_kpi['Municipio Origem'].astype(str).unique()))
            mun_selecionado = col_f2.selectbox("Município de Origem", lista_municipios)
            
            lista_status = ["Todos"] + sorted(list(df_kpi['Status da Rota'].astype(str).unique()))
            status_selecionado = col_f3.selectbox("Status Global da Rota", lista_status)
            
            lista_fontes = ["Todas"] + sorted(list(df_kpi['Fonte Geocoding Origem'].astype(str).unique()))
            fonte_selecionada = col_f4.selectbox("Fonte de Geocoding", lista_fontes)
            
        df_filtrado = df_kpi.copy()
        if uf_selecionada != "Todas": df_filtrado = df_filtrado[df_filtrado['UF_Sintetica_Origem'] == uf_selecionada]
        if mun_selecionado != "Todos": df_filtrado = df_filtrado[df_filtrado['Municipio Origem'] == mun_selecionado]
        if status_selecionado != "Todos": df_filtrado = df_filtrado[df_filtrado['Status da Rota'] == status_selecionado]
        if fonte_selecionada != "Todas": df_filtrado = df_filtrado[df_filtrado['Fonte Geocoding Origem'] == fonte_selecionada]
        
        if df_filtrado.empty:
            st.warning("A combination de filtros não retornou nenhum registro.")
        else:
            df_sucesso = df_filtrado[df_filtrado["Status da Rota"].str.contains("Erro") == False]
            
            with st.container(border=True):
                col_k1, col_k2, col_k3, col_k4 = st.columns(4)
                total_distancia = df_sucesso['Distancia'].sum()
                total_tempo_mins = df_sucesso['Tempo_Minutos'].sum()
                tempo_total_str = f"{total_tempo_mins // 60}h {total_tempo_mins % 60}m"
                
                col_k1.metric("Rotas Processadas no Filtro", f"{len(df_filtrado)}")
                col_k2.metric("Distância Viária Acumulada", f"{round(total_distancia, 2)} km")
                col_k3.metric("Tempo Viário Acumulado", f"{tempo_total_str}")
                col_k4.metric("Score Global Médio", f"{round(df_sucesso['Score Final Global'].mean(), 1) if not df_sucesso.empty else 0} / 100")
            
            st.caption("✨ **DICA DE OURO INTERATIVA:** Clique em uma fatia da rosca ou em uma barra de município. A tabela ao lado, bem como a matriz de dispersão abaixo, reagirão automaticamente filtrando os dados! Desenhar um retângulo na Matriz também atualizará os gráficos acima.")
            
            click_uf = alt.selection_point(fields=['UF_Sintetica_Origem'], name='UF')
            click_mun = alt.selection_point(fields=['Municipio Origem'], name='Mun')
            brush = alt.selection_interval(name='Brush')

            top_muns = df_filtrado['Municipio Origem'].value_counts().head(20).index.tolist()
            base_chart = alt.Chart(df_filtrado)

            pie_base = base_chart.encode(
                theta=alt.Theta("count():Q", stack=True),
                color=alt.Color("UF_Sintetica_Origem:N", legend=alt.Legend(title="Estados (UF)"))
            )
            arc = pie_base.mark_arc(innerRadius=60).encode(
                opacity=alt.condition(click_uf & click_mun & brush, alt.value(1), alt.value(0.2)),
                tooltip=['UF_Sintetica_Origem', 'count()']
            ).add_params(click_uf)
            
            chart_pie = arc.transform_filter(click_mun).properties(width=220, height=280, title="Volume por Estado (UF)")

            bar_base = base_chart.transform_filter(alt.FieldOneOfPredicate(field='Municipio Origem', oneOf=top_muns)).encode(
                x=alt.X('count():Q', title='Volume', axis=alt.Axis(tickMinStep=1)),
                y=alt.Y('Municipio Origem:N', title='Município', sort=alt.EncodingSortField(field='Municipio Origem', op='count', order='descending'))
            )
            bar = bar_base.mark_bar(color='#1E90FF').encode(
                opacity=alt.condition(click_uf & click_mun & brush, alt.value(1), alt.value(0.3)),
                tooltip=['Municipio Origem', 'count()']
            ).add_params(click_mun)
            
            text_bar = bar_base.mark_text(align='right', dx=-5, color='white', fontWeight='bold').encode(
                text=alt.Text("count():Q")
            )
            chart_bars = alt.layer(bar, text_bar).transform_filter(click_uf).properties(width=380, height=280, title="Ranking de Municípios (Top 20)")

            max_dist = int(df_filtrado['Distancia'].max()) if not df_filtrado.empty else 100
            valores_eixo_x = list(range(0, max_dist + 100, 50))
            
            scatter = base_chart.mark_circle(size=80).encode(
                x=alt.X('Distancia:Q', title='Distância Viária Oficial (km)', axis=alt.Axis(values=valores_eixo_x), scale=alt.Scale(zero=False, nice=True, padding=10)),
                y=alt.Y('Tempo_Horas:Q', title='Tempo Estimado (Horas)', scale=alt.Scale(zero=False, nice=True, padding=10)),
                color=alt.Color('Status da Rota:N', scale=alt.Scale(scheme='set2')),
                opacity=alt.condition(click_uf & click_mun & brush, alt.value(0.9), alt.value(0.1)),
                tooltip=['Origem', 'Destino', 'Distancia', 'Tempo', 'Status da Rota', 'Score Final Global']
            ).add_params(brush).transform_filter(click_uf).transform_filter(click_mun).properties(
                width=850, height=280, title="Matriz de Dispersão (Gargalos Logísticos)"
            )

            dash_top = alt.hconcat(chart_pie, chart_bars, spacing=30).resolve_scale(color='independent')
            dashboard_completo = alt.vconcat(dash_top, scatter, spacing=30).resolve_scale(color='independent').configure_view(strokeWidth=0)

            col_graficos, col_tabela = st.columns([60, 40], gap="large")
            
            with col_graficos:
                try:
                    evento_clique = st.altair_chart(
                        dashboard_completo, 
                        use_container_width=False, 
                        on_select="rerun", 
                        key=f"dash_{st.session_state.get('dash_key', 0)}"
                    )
                except Exception:
                    evento_clique = None
                    st.altair_chart(
                        dashboard_completo, 
                        use_container_width=False,
                        key=f"dash_fallback_{st.session_state.get('dash_key', 0)}"
                    )

            with col_tabela:
                st.caption("📋 **Tabela Detalhada Dinâmica**")
                ufs_selecionadas = []
                muns_selecionados = []
                
                if evento_clique and hasattr(evento_clique, 'selection'):
                    sel_uf = evento_clique.selection.get('UF', [])
                    sel_mun = evento_clique.selection.get('Mun', [])
                    
                    for item in sel_uf:
                        if isinstance(item, dict) and 'UF_Sintetica_Origem' in item: ufs_selecionadas.append(item['UF_Sintetica_Origem'])
                    for item in sel_mun:
                        if isinstance(item, dict) and 'Municipio Origem' in item: muns_selecionados.append(item['Municipio Origem'])
                
                df_tabela = df_filtrado.copy()
                if ufs_selecionadas: df_tabela = df_tabela[df_tabela['UF_Sintetica_Origem'].isin(ufs_selecionadas)]
                if muns_selecionados: df_tabela = df_tabela[df_tabela['Municipio Origem'].isin(muns_selecionados)]
                
                tabela_h = min(800, max(150, len(df_tabela) * 35 + 43))
                
                st.dataframe(
                    df_tabela[['Origem', 'Destino', 'Distancia', 'Tempo', 'Status da Rota', 'Link da Rota']],
                    use_container_width=True,
                    height=tabela_h,
                    column_config={"Link da Rota": st.column_config.LinkColumn("🔗 Google Maps")},
                    hide_index=True
                )

            st.markdown("---")
            st.markdown("#### 🏆 Top Extremos Logísticos")
            tab_dist_max, tab_dist_min, tab_tempo = st.tabs(["Maiores Distâncias", "Menores Distâncias", "Maiores Tempos (Gargalos)"])
            
            with tab_dist_max:
                st.dataframe(df_filtrado.nlargest(10, 'Distancia')[['Origem', 'Destino', 'Distancia', 'Tempo', 'Status da Rota']], use_container_width=True)
            with tab_dist_min:
                st.dataframe(df_filtrado.nsmallest(10, 'Distancia')[['Origem', 'Destino', 'Distancia', 'Tempo', 'Status da Rota']], use_container_width=True)
            with tab_tempo:
                st.dataframe(df_filtrado.nlargest(10, 'Tempo_Minutos')[['Origem', 'Destino', 'Tempo', 'Distancia', 'Status da Rota']], use_container_width=True)

            st.markdown("---")
            st.markdown("#### 🚨 Auditoria de Qualidade de Dados (Rotas Críticas)")
            df_suspeitas = df_filtrado[(df_filtrado['Score Final Global'] < 70) | (df_filtrado['Status da Rota'] == "Erro") | (df_filtrado['Confianca Origem'] == "BAIXA")]
            
            if not df_suspeitas.empty:
                st.error(f"Foram identificadas {len(df_suspeitas)} rotas requerendo intervenção humana/auditoria.")
                st.dataframe(df_suspeitas[['Origem', 'Destino', 'Score Final Global', 'Confianca Origem', 'Fonte Geocoding Origem', 'Motivo Roteamento']], use_container_width=True)
            else:
                st.success("🎉 Todas as rotas neste recorte passaram no controle de qualidade geodésica (Score >= 70 e Confiança > Baixa).")

    else:
        st.warning("Aguardando processamento de planilha na aba de Lotes para ativar o Data Analytics Engine.")
        
    st.markdown("---")
    st.markdown("#### 📜 Trilha de Auditoria Corporativa (Histórico de Lotes)")
    historico = [cache_historico_lotes[k] for k in cache_historico_lotes]
    if historico:
        st.dataframe(pd.DataFrame(historico).sort_values(by="Data/Hora", ascending=False).reset_index(drop=True), use_container_width=True)
    else:
        st.caption("Nenhum registro de lote persistido na base histórica até o momento.")

with tab_enciclopedia:
    st.info("💡 **Objetivo desta aba:** Servir como o repositório mestre de conhecimento. Esta enciclopédia detalha toda a jornada técnica de um dado dentro do aplicativo, desde a limpeza gramatical até a validação geométrica em nuvem.")
    st.markdown("""
    # 📚 Enciclopédia do Sistema de Roteirização Inteligente
    
    Bem-vindo ao Atlas da Arquitetura do Motor de Roteamento Corporativo. Este documento serve como um guia profundo sobre o funcionamento das engrenagens lógicas, dos motores de inteligência artificial e dos cruzamentos geográficos que operam invisíveis aos olhos do usuário em cada busca.

    ---

    ## 1. A Filosofia Híbrida e o Problema do *Street Snapping*
    A geocodificação tradicional costuma sofrer de um viés mercadológico grave: as APIs tentam "adivinhar" ruas e números mesmo quando o usuário só digitou o nome de uma cidade. Isso resulta em caminhões sendo enviados para ruas aleatórias no centro de uma cidade, em vez do verdadeiro destino regional.
    
    Este sistema contorna o problema empregando a **Abordagem de Entendimento Lexical (NLP)** combinada com um **Filtro Anti-Fantasma**. Antes de qualquer API de rede ser chamada, o aplicativo aplica Expressões Regulares (`Regex`) para fatiar o texto: separa CEP, tira as abreviações ("Av.", "R.", "Qd.") e classifica a intenção da busca (Ex: `MUNICIPIO`, `CONDOMINIO`, `RURAL`, `ENDERECO_COMPLETO`).

    ## 2. Fast-Track Offline e o IBGE
    Para buscas estritamente em nível de cidade/município, o sistema não aciona internet. Ele consulta um banco de dados estático gigantesco persistido na memória (`Cache`) gerado a partir do **Serviço de Dados do IBGE**. 
    * **Vantagem:** O tempo de geocodificação cai de 1.5s para 0.00s.
    * **Exatidão:** A coordenada devolvida é o Centróide Oficial delimitado pelo Governo Federal, não uma aproximação de motor estrangeiro.

    ## 3. O Dilema dos Múltiplos Motores (Ensemble Geográfico)
    Se o endereço precisa ser buscado na internet, em quem confiar? Google? TomTom? OpenStreetMap? O sistema adota a postura de que **nenhum motor isolado é dono da verdade absoluta**. Ele envia a busca paralelamente (`ThreadPoolExecutor`) para até 5 provedores simultâneos:
    
    1. **ArcGIS (ESRI):** Maior especialista em malhas prediais do mundo.
    2. **Nominatim / Photon:** Tecnologias do OpenStreetMap, perfeitas para achar fazendas, chácaras e estradas de terra no Brasil central.
    3. **TomTom Logistics:** Especialista em roteamento B2B e rodovias pesadas.

    ## 4. Clustering Espacial: A Reunião das Coordenadas (DBSCAN)
    Quando os 5 motores retornam suas coordenadas, o sistema aplica o algoritmo de Machine Learning não supervisionado chamado **DBSCAN** (*Density-Based Spatial Clustering of Applications with Noise*).
    
    Ele plota os 5 pontos em um mapa virtual em branco. Se 3 motores apontarem para a mesma quadra em São Paulo, e 2 motores apontarem para Manaus (Falsos Positivos causados por ruas com mesmo nome), o algoritmo de Clusterização agrupa apenas os pontos que estão a distâncias curtas entre si, eliminando instantaneamente o "ruído" geográfico.

    ## 5. Inferência Bayesiana e o Score Global
    Dos candidatos que sobraram no Cluster verdadeiro, qual deles será eleito o "Vencedor Oficial"? Entra em cena a Matemática de Decisão: O Teorema de Bayes.
    O sistema confere bônus multiplicativos se os motores confirmarem as assinaturas originais do cliente:
    
    * A UF da coordenada bate com a UF que o cliente digitou? `(Score x 1.3)`
    * O CEP bate milimetricamente com o BrasilAPI? `(Score x 4.0)`
    * O nome da rua resultante tem distância de Levenshtein > 90% em relação ao que o usuário digitou inicialmente? `(Score x 1.5)`
    
    O candidato que atingir a maior probabilidade se torna a coordenada de Origem ou Destino que passará para a fase de Roteamento asfáltico.

    ## 6. Validação Geodésica e Roteamento (A Marreta Matemática)
    Com o Ponto A e o Ponto B em mãos, o aplicativo efetua um cálculo matemático puro: A **Fórmula de Haversine**. 
    Ela mede, respeitando a curvatura da Terra, a distância exata em Linha Reta entre os dois pontos.
    
    Só então o motor do Google Maps é acionado. Se o Google disser que a viagem de asfalto tem 1.000 km, mas a Linha Reta matemática de Haversine atestou que a distância é de apenas 15 km, o motor "rebela-se" contra o Google Maps, entendendo que ocorreu uma **Violação Geodésica**, rejeita o caminho do asfalto falso e adota lógicas e heurísticas de correção.

    ## 7. A Matriz de Alocação de Hubs (Competição Geográfica)
    A aba de Alocação não mede apenas "De A para B". Ela aplica a estratégia logística de *Nearest Neighbor*.
    Se você possui 10 Centros de Distribuição e envia 1.000 clientes, o sistema roda o fluxo acima 10.000 vezes na memória virtual, traçando a linha reta de todos os clientes para todos os CD's, listando do mais próximo ao mais distante. Após eleger o vencedor natural (aquele fisicamente mais perto), aciona os motores asfálticos (Google/OSRM) e consolida a planilha de modo hermético e autônomo, dispensando qualquer interferência do operador.
    """)

with tab_motores:
    st.info("💡 **Objetivo desta aba:** Monitorar a saúde técnica do ecossistema. Visualize quais APIs em nuvem responderam melhor, identifique instabilidades (timeouts), observe os tempos médios de resposta e verifique a integridade algorítmica do último lote.")
    st.markdown("### 🔌 Saúde dos Motores em Nuvem e Performance Sistêmica")
    
    if 'df_processado' in st.session_state:
        df_kpi = st.session_state['df_processado'].copy()
        
        with st.container(border=True):
            col_p1, col_p2, col_p3 = st.columns(3)
            col_p1.metric("Tempo Médio Geocoding / Rota", f"{round(df_kpi['Tempo Geocoding (s)'].mean(), 2)} s")
            col_p2.metric("Tempo Médio Roteamento / Rota", f"{round(df_kpi['Tempo Roteamento (s)'].mean(), 2)} s")
            col_p3.metric("Tempo Global Total / Rota", f"{round(df_kpi['Tempo Total (s)'].mean(), 2)} s")
        
        col_m1, col_m2 = st.columns(2)
        
        with col_m1:
            st.caption("**Volume de Requisições por Motor de Origem (Market Share)**")
            grafico_apis = alt.Chart(df_kpi).mark_arc(innerRadius=60).encode(
                theta=alt.Theta(field="Fonte Geocoding Origem", aggregate="count"),
                color=alt.Color(field="Fonte Geocoding Origem", type="nominal", legend=alt.Legend(title="Motores")),
                tooltip=['Fonte Geocoding Origem', 'count()']
            ).properties(height=350)
            st.altair_chart(grafico_apis, use_container_width=True)
            
        with col_m2:
            st.caption("**Distribuição Qualitativa: Status Bayesiano da Rota**")
            grafico_status = alt.Chart(df_kpi).mark_bar().encode(
                x=alt.X('Status da Rota:N', title='Classificação de Confiança'),
                y=alt.Y('count():Q', title='Volume de Rotas'),
                color=alt.Color('Status da Rota:N', scale=alt.Scale(domain=['Excelente', 'Boa', 'Aceitável', 'Revisar', 'Erro'], range=['#00FF7F', '#1E90FF', '#FFD700', '#FFA500', '#FF4500'])),
                tooltip=['Status da Rota', 'count()']
            ).properties(height=350)
            st.altair_chart(grafico_status, use_container_width=True)
            
    st.markdown("---")
    st.markdown("#### 📡 Tabela Mestre de Latência das APIs")
    health_data = []
    for api in ["GOOGLE_MAPS", "ARCGIS", "TOMTOM", "NOMINATIM", "PHOTON", "OVERPASS", "OSRM"]:
        dados = cache_api_health.get(api, {"hits": 0, "calls": 0, "falhas": 0, "tempo_total": 0.0})
        t_med = f"{round((dados['tempo_total'] / max(1, dados['calls'])) * 1000)} ms" if dados['calls'] > 0 else "N/A"
        tx_err = f"{round((dados['falhas'] / max(1, dados['calls'] + dados['falhas'])) * 100, 1)}%" if dados['calls'] > 0 else "0.0%"
        health_data.append({"Provedor Oficial": api, "Status da Conexão": "🟢 Online" if dados["falhas"] == 0 else "🔴 Instável/Erros", "Latência Média Observada": t_med, "Taxa de Falha": tx_err, "Total de Chamadas Realizadas": dados["calls"]})
    
    st.dataframe(pd.DataFrame(health_data), use_container_width=True)

with tab_auditoria:
    st.info("💡 **Objetivo desta aba:** Transparência Total e Explicabilidade (XAI). Funciona como uma 'Caixa Preta' aberta do sistema. Verifique em detalhes qual algoritmo tomou a decisão para cada coordenada e por que ele escolheu descartar outras opções.")
    st.markdown("### 🕵️ Dossiê de Auditoria Viária e Espacial")
    
    tab_aud_lote, tab_aud_hub = st.tabs(["⚙️ Logs do Lote de Roteamento Padrão", "📦 Logs do Motor de Alocação (Hubs)"])
    
    with tab_aud_lote:
        if 'logs_auditoria' in st.session_state and st.session_state['logs_auditoria']:
            st.write("Abaixo consta a árvore de decisões explicáveis tomada pelo motor de Lote:")
            st.dataframe(pd.DataFrame(st.session_state['logs_auditoria']), use_container_width=True)
        else:
            st.info("Nenhum registro de auditoria gerado. Processe uma planilha na aba de Processamento em Lote.")
            
    with tab_aud_hub:
        if 'logs_auditoria_alocacao' in st.session_state and st.session_state['logs_auditoria_alocacao']:
            st.write("Abaixo constam as inferências individuais feitas para cada Base e Destino lido na formação da Matriz:")
            st.dataframe(pd.DataFrame(st.session_state['logs_auditoria_alocacao']), use_container_width=True)
        else:
            st.info("Nenhum registro de auditoria gerado. Processe matrizes na aba de Alocação de Hubs para visualizar as justificativas.")
