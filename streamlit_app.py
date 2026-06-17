import streamlit as st
import pandas as pd
import requests
import time
import math
import io
import re
import os
import pickle
import collections
import sqlite3
import threading
import hashlib
from unidecode import unidecode
from rapidfuzz import process, fuzz
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# 🎛️ CONFIGURAÇÃO DE UI/UX E AMBIENTE GLOBAL
# ==============================================================================
st.set_page_config(page_title="Gerenciador de Rotas Inteligentes", page_icon="🚗", layout="centered")

GRAPHHOPPER_API_KEY = "" # Preencha com sua chave de produção quando possuir
ORS_API_KEY = ""         # Preencha com sua chave OpenRouteService quando possuir

WORKERS_DISPONIVEIS = 8

if "executor_global" not in st.session_state:
    st.session_state["executor_global"] = ThreadPoolExecutor(max_workers=WORKERS_DISPONIVEIS)

if "executor_apis" not in st.session_state:
    MAX_WORKERS_API = min(32, (os.cpu_count() or 1) * 4)
    st.session_state["executor_apis"] = ThreadPoolExecutor(max_workers=MAX_WORKERS_API)

if "fila_nominatim" not in st.session_state:
    st.session_state["fila_nominatim"] = ThreadPoolExecutor(max_workers=1)

# ==============================================================================
# 🧠 ENGINE DE CACHE SQLITE NATIVO (Alta Performance & Thread-Safe)
# ==============================================================================
class SQLiteCacheDB:
    def __init__(self, db_name):
        self.db_name = f"{db_name}.sqlite"
        self.lock = threading.Lock()
        with sqlite3.connect(self.db_name) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value BLOB, expiry REAL)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_expiry ON cache(expiry);")
    
    def set(self, key, value, expire=2592000):
        expiry_time = time.time() + expire if expire else None
        val_blob = pickle.dumps(value)
        with self.lock:
            with sqlite3.connect(self.db_name, timeout=15.0) as conn:
                conn.execute("INSERT OR REPLACE INTO cache (key, value, expiry) VALUES (?, ?, ?)", (key, val_blob, expiry_time))
    
    def __contains__(self, key):
        with self.lock:
            with sqlite3.connect(self.db_name, timeout=15.0) as conn:
                cur = conn.execute("SELECT 1 FROM cache WHERE key = ? AND (expiry IS NULL OR expiry > ?)", (key, time.time()))
                return cur.fetchone() is not None
                
    def __getitem__(self, key):
        with self.lock:
            with sqlite3.connect(self.db_name, timeout=15.0) as conn:
                cur = conn.execute("SELECT value FROM cache WHERE key = ? AND (expiry IS NULL OR expiry > ?)", (key, time.time()))
                row = cur.fetchone()
                if row: return pickle.loads(row[0])
                raise KeyError(key)
                
    def get(self, key, default=None):
        try: return self.__getitem__(key)
        except KeyError: return default
        
    def cull(self):
        with self.lock:
            with sqlite3.connect(self.db_name, timeout=15.0) as conn:
                conn.execute("DELETE FROM cache WHERE expiry IS NOT NULL AND expiry <= ?", (time.time(),))

cache_classificacao = SQLiteCacheDB("cache_classificacao")
cache_fuzzy = SQLiteCacheDB("cache_fuzzy")
cache_geo = SQLiteCacheDB("cache_geo")
cache_rotas = SQLiteCacheDB("cache_rotas")
cache_poi = SQLiteCacheDB("cache_poi")
cache_cep = SQLiteCacheDB("cache_cep")
cache_google = SQLiteCacheDB("cache_google")
cache_reverse = SQLiteCacheDB("cache_reverse")
cache_base_local = SQLiteCacheDB("cache_base_local")
cache_aprendizado = SQLiteCacheDB("cache_aprendizado")
cache_feedback = SQLiteCacheDB("feedback_ground_truth")

for c in [cache_classificacao, cache_fuzzy, cache_geo, cache_rotas, cache_poi, cache_cep, cache_google, cache_reverse, cache_base_local, cache_aprendizado, cache_feedback]:
    c.cull()

# ==============================================================================
# 🤖 INTEGRAÇÃO DE MACHINE LEARNING (SHADOW MODE)
# ==============================================================================
MODELO_ML_GEO = None 
try:
    if os.path.exists("modelo_geocoding_xgb.pkl"):
        import xgboost as xgb
        MODELO_ML_GEO = pickle.load(open("modelo_geocoding_xgb.pkl", "rb"))
except Exception: pass

# ==============================================================================
# 🌐 SESSÃO E DADOS GLOBAIS THREAD-SAFE (HOMÔNIMOS E BASE IBGE)
# ==============================================================================
def realizar_manutencao_logs_google():
    diretorio_logs = "logs_google"
    os.makedirs(diretorio_logs, exist_ok=True)
    try:
        limite_tempo = time.time() - (30 * 86400)
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
    except Exception: pass
    
    lista_completa = list(base_mun.keys()) + list(base_dist.keys())
    return base_mun, base_est, base_dist, lista_completa

IBGE_MUNICIPIOS, IBGE_ESTADOS, IBGE_DISTRITOS, LISTA_TOPONIMOS = carregar_dados_ibge()

LISTA_CONTEXTO_FUZZY = list(set([f"{k} {v['uf']}" for k, v_list in IBGE_MUNICIPIOS.items() for v in v_list] + [f"{k} {v['uf']}" for k, v_list in IBGE_DISTRITOS.items() for v in v_list]))

SINONIMOS_SEMANTICOS = {
    "UNB": "UNIVERSIDADE DE BRASILIA", "CATOLICA": "UNIVERSIDADE CATOLICA",
    "JK": "JUSCELINO KUBITSCHEK", "HBDF": "HOSPITAL DE BASE DO DISTRITO FEDERAL",
    "HRAN": "HOSPITAL REGIONAL DA ASA NORTE", "RODOVIARIA": "TERMINAL RODOVIARIO"
}

POI_KEYWORDS = ["AEROPORTO", "HOSPITAL", "UNIVERSIDADE", "FACULDADE", "ESCOLA", "SHOPPING", "HOTEL", "RODOVIARIA", "ESTADIO", "MINISTERIO", "AGENCIA", "BANCO", "IGREJA", "FORUM", "TRIBUNAL", "DELEGACIA", "PREFEITURA", "CLINICA"]

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
        self.via_keys = [
            "RUA", "AVENIDA", "TRAVESSA", "ALAMEDA", "RODOVIA", "ESTRADA", "QUADRA", 
            "SQN", "SQS", "SHIS", "SHIN", "SCRN", "SCS", "SRTVN", "CLS", "CLN",
            "QNL", "QNM", "QNN", "QNG", "QNJ", "QNK", "QI", "QE", "QC", "QR", "QS", "QSC"
        ]
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
        
        def padronizar_rodovia(match): return f"{match.group(1)}-{match.group(2).zfill(3)}"
        t = re.sub(r'\b(BR|AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\s*[-]?\s*(\d{1,3})\b', padronizar_rodovia, t)
        
        abreviacoes = {
            r'\bAV\b': 'AVENIDA', r'\bR\b': 'RUA', r'\bQD\b': 'QUADRA', r'\bQ\b': 'QUADRA',
            r'\bLT\b': 'LOTE', r'\bL\b': 'LOTE', r'\bCJ\b': 'CONJUNTO', r'\bCONJ\b': 'CONJUNTO', 
            r'\bBL\b': 'BLOCO', r'\bAPT\b': 'APARTAMENTO', r'\bAPTO\b': 'APARTAMENTO',
            r'\bST\b': 'SETOR', r'\bCH\b': 'CHACARA', r'\bROD\b': 'RODOVIA', r'\bKM\b': 'QUILOMETRO', 
            r'\bAL\b': 'ALAMEDA', r'\bTR\b': 'TRAVESSA', r'\bTV\b': 'TRAVESSA', 
            r'\bPCA\b': 'PRACA', r'\bPQ\b': 'PARQUE', r'\bSQN\b': 'SUPERQUADRA NORTE', 
            r'\bSQS\b': 'SUPERQUADRA SUL', r'\bCLN\b': 'COMERCIO LOCAL NORTE', r'\bCLS\b': 'COMERCIO LOCAL SUL',
            r'\bVL\b': 'VILA', r'\bJD\b': 'JARDIM', r'\bRES\b': 'RESIDENCIAL', r'\bCOND\b': 'CONDOMINIO',
            r'\bED\b': 'EDIFICIO', r'\bGL\b': 'GLEBA', r'\bNR\b': 'NUCLEO RURAL', r'\bVCO\b': 'BECO'
        }
        for padrao, expansao in abreviacoes.items(): t = re.sub(padrao, expansao, t)
        for chave, valor in SINONIMOS_SEMANTICOS.items(): t = re.sub(r'\b' + chave + r'\b', valor, t)
        return re.sub(r'\s+', ' ', t).strip()

    def classificar_entrada(self, texto_norm):
        if texto_norm in cache_classificacao: return cache_classificacao[texto_norm]
        tipo = "LOGRADOURO"
        if re.search(r'\b\d{5}-?\d{3}\b', texto_norm): tipo = "CEP"
        elif any(k in texto_norm for k in POI_KEYWORDS): tipo = "POI"
        elif any(k in texto_norm for k in self.rural_keys): tipo = "RURAL"
        elif any(k in texto_norm for k in self.via_keys) and bool(re.search(r'\d+', texto_norm)): tipo = "ENDERECO_COMPLETO"
        elif any(k in texto_norm for k in self.bairro_keys): tipo = "BAIRRO"
        elif texto_norm in IBGE_MUNICIPIOS: tipo = "MUNICIPIO"
        elif texto_norm in IBGE_DISTRITOS: tipo = "DISTRITO"
        cache_classificacao.set(texto_norm, tipo, expire=2592000); return tipo

    def aplicar_fuzzy_multidimensional(self, texto_norm):
        if texto_norm in cache_fuzzy: return cache_fuzzy[texto_norm]
        for token in texto_norm.split():
            if len(token) >= 5 and token not in IBGE_MUNICIPIOS and token not in IBGE_DISTRITOS:
                top_matches = process.extract(token, LISTA_CONTEXTO_FUZZY, scorer=fuzz.WRatio, limit=5)
                if top_matches and top_matches[0][1] >= 85:
                    melhor_match = max(top_matches, key=lambda m: fuzz.token_set_ratio(texto_norm, m[0]))
                    if melhor_match[1] >= 85 and fuzz.token_set_ratio(texto_norm, melhor_match[0]) >= 90:
                        texto_norm = texto_norm.replace(token, melhor_match[0].rsplit(' ', 1)[0])
                        break
        cache_fuzzy.set(texto_norm, texto_norm, expire=2592000); return texto_norm

    def resolver_contexto_administrativo(self, texto_norm):
        tokens = texto_norm.split()
        uf_explicita = next((re.sub(r'[^A-Z]', '', t) for t in reversed(tokens) if re.sub(r'[^A-Z]', '', t) in IBGE_ESTADOS), None)

        if not uf_explicita or uf_explicita == "DF":
            for token in tokens:
                sigla_limpa = re.sub(r'[^A-Z]', '', token)
                if sigla_limpa in self.mapa_siglas_df and len(sigla_limpa) >= 2: return {"uf": "DF", "municipio": "BRASILIA", "distrito": self.mapa_siglas_df[sigla_limpa]}
            for chave, ra_oficial in self.mapa_contexto_df.items():
                if chave in texto_norm: return {"uf": "DF", "municipio": "BRASILIA", "distrito": ra_oficial}
                
        for i in range(len(tokens)):
            for j in range(i + 1, len(tokens) + 1):
                chunk = " ".join(tokens[i:j])
                if chunk in IBGE_MUNICIPIOS: return {"uf": uf_explicita if uf_explicita and any(item["uf"] == uf_explicita for item in IBGE_MUNICIPIOS[chunk]) else IBGE_MUNICIPIOS[chunk][0]["uf"], "municipio": chunk, "distrito": ""}
                if chunk in IBGE_DISTRITOS: return {"uf": uf_explicita if uf_explicita and any(item["uf"] == uf_explicita for item in IBGE_DISTRITOS[chunk]) else IBGE_DISTRITOS[chunk][0]["uf"], "municipio": IBGE_DISTRITOS[chunk][0]["municipio"], "distrito": chunk}
        return {"uf": uf_explicita if uf_explicita else "", "municipio": "", "distrito": ""}

    def construir_endereco_canonico(self, texto_cru):
        texto_norm = self.normalizar(texto_cru)
        texto_fuzzy = self.aplicar_fuzzy_multidimensional(texto_norm)
        tipo = self.classificar_entrada(texto_fuzzy)
        contexto = self.resolver_contexto_administrativo(texto_fuzzy)
        uf, municipio, distrito = contexto["uf"], contexto["municipio"], contexto["distrito"]
        
        componentes = [texto_fuzzy]
        if distrito and distrito not in texto_fuzzy: componentes.append(distrito)
        if municipio and municipio not in texto_fuzzy: componentes.append(municipio)
        if uf and IBGE_ESTADOS.get(uf, uf) not in texto_fuzzy: componentes.append(IBGE_ESTADOS.get(uf, uf))
        if "BRASIL" not in texto_fuzzy: componentes.append("BRASIL")
        
        return re.sub(r',\s*,', ',', ", ".join(componentes)).strip(), tipo, "", 0.0, 0.0

semantica = MotorEnderecoCanônico()

# ==============================================================================
# 🧮 LÓGICA GEODÉSICA E CASCATA POSTAL
# ==============================================================================
def validar_coordenada_brasil(lat, lon):
    try:
        lat_f, lon_f = float(lat), float(lon)
        if (-35.0 <= lat_f <= 6.0) and (-75.0 <= lon_f <= -28.0): return True, lat_f, lon_f
        if (-35.0 <= lon_f <= 6.0) and (-75.0 <= lat_f <= -28.0): return True, lon_f, lat_f 
        return False, lat_f, lon_f
    except (ValueError, TypeError): return False, 0.0, 0.0

def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    if not (-90 <= lat1 <= 90) or not (-90 <= lat2 <= 90) or not (-180 <= lon1 <= 180) or not (-180 <= lon2 <= 180) or (lat1==lat2 and lon1==lon2) or lat1==0.0 or lon1==0.0 or lat2==0.0 or lon2==0.0: return 0.0
    try:
        a, b, f = 6378137.0, 6356752.314245, 1 / 298.257223563
        L = math.radians(lon2 - lon1)
        U1, U2 = math.atan((1 - f) * math.tan(math.radians(lat1))), math.atan((1 - f) * math.tan(math.radians(lat2)))
        sinU1, cosU1, sinU2, cosU2 = math.sin(U1), math.cos(U1), math.sin(U2), math.cos(U2)
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
        return round((b * A * (sigma - deltaSigma)) / 1000, 2)
    except Exception:
        dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        m_a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
        return round(6371.0 * 2 * math.atan2(math.sqrt(m_a), math.sqrt(1 - m_a)), 2)

def cascata_postal_tripla(cep_limpo):
    if cep_limpo in cache_cep:
        d = cache_cep[cep_limpo]
        return d if len(d) == 6 else (d[0], d[1], d[2], d[3], 0.0, 0.0)
    lat, lon = 0.0, 0.0
    for url_fmt, timeout, ext in [
        (f"https://brasilapi.com.br/api/cep/v2/{cep_limpo}", 4, lambda r: (r.get('street', ''), r.get('neighborhood', ''), r.get('city', ''), r.get('state', ''), float(r.get("location", {}).get("coordinates", {}).get("latitude", 0)), float(r.get("location", {}).get("coordinates", {}).get("longitude", 0)))),
        (f"https://viacep.com.br/ws/{cep_limpo}/json/", 4, lambda r: (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), 0.0, 0.0) if "erro" not in r else None),
        (f"https://opencep.com/v1/{cep_limpo}", 4, lambda r: (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), 0.0, 0.0) if "error" not in r else None)
    ]:
        try:
            r = session.get(url_fmt, timeout=timeout).json()
            if d := ext(r): cache_cep.set(cep_limpo, d, expire=2592000); return d
        except Exception: pass
    try:
        def _nom_cep():
            time.sleep(1.1)
            return session.get(f"https://nominatim.openstreetmap.org/search?format=json&postalcode={cep_limpo}&countrycodes=br&limit=1", headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
        if r_nom := st.session_state["fila_nominatim"].submit(_nom_cep).result(): return "", "", "", "", float(r_nom[0]['lat']), float(r_nom[0]['lon'])
    except Exception: pass
    return "", "", "", "", 0.0, 0.0

def validar_consistencia_administrativa(candidato, uf_inf):
    est_api = unidecode(candidato.get('estado', '')).upper().strip()
    return False if uf_inf and est_api and uf_inf != est_api else True

def validar_consistencia_municipal(candidato, mun_inf):
    if not mun_inf: return True
    cid_api = unidecode(candidato.get('cidade', '')).upper().strip()
    return True if cid_api and (mun_inf == cid_api or mun_inf in cid_api or cid_api in mun_inf or fuzz.token_set_ratio(mun_inf, cid_api) >= 95) else False

# ==============================================================================
# 🗺️ MÓDULOS DE GEOCODIFICAÇÃO E REVERSE
# ==============================================================================
def API_Google_Geocoding_Scraper(query):
    try:
        r = session.get(f"https://www.google.com/maps/search/{requests.utils.quote(query)}", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=5, allow_redirects=True)
        if match := re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.url) or re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.text): return [{"lat": float(match.group(1)), "lon": float(match.group(2)), "fonte": "GOOGLE_MAPS", "score_base": 40, "cidade": "", "estado": "", "bairro": "", "logradouro": "", "numero": "", "cep": ""}]
    except Exception: pass
    return []

def executar_reverse_geocoding_multimotor(lat, lon):
    rev_key = f"{round(lat,5)}|{round(lon,5)}"
    if rev_key in cache_reverse: return cache_reverse[rev_key]
    res = {"logradouro": "", "bairro": "", "cidade": "", "municipio": "", "distrito": "", "estado": "", "cep": ""}
    try:
        def _nom_rev():
            time.sleep(1.1)
            return session.get(f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&addressdetails=1", headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
        a = st.session_state["fila_nominatim"].submit(_nom_rev).result().get("address", {})
        res.update({"logradouro": a.get("road", a.get("pedestrian", "")), "bairro": a.get("neighbourhood", a.get("suburb", a.get("city_district", ""))), "cidade": a.get("city", a.get("town", a.get("municipality", ""))), "estado": a.get("state", "").upper(), "cep": a.get("postcode", "")})
        cache_reverse.set(rev_key, res, expire=2592000); return res
    except Exception: pass
    try:
        if addr := session.get(f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/reverseGeocode?location={lon},{lat}&f=json", timeout=4).json().get('address'):
            res.update({"logradouro": addr.get('Address', ''), "bairro": addr.get('Neighborhood', ''), "cidade": addr.get('City', ''), "estado": addr.get('RegionAbbr', '').upper(), "cep": addr.get('Postal', '')})
            cache_reverse.set(rev_key, res, expire=2592000)
    except Exception: pass
    return res

def API_ArcGIS(query, ctx=None):
    try:
        url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&Address={requests.utils.quote(ctx.get('logradouro', ''))}&Neighborhood={requests.utils.quote(ctx.get('bairro', ''))}&City={requests.utils.quote(ctx.get('municipio', ''))}&Region={requests.utils.quote(ctx.get('uf', ''))}&Postal={requests.utils.quote(ctx.get('cep', ''))}&maxLocations=5&sourceCountry=BRA&outFields=*" if ctx and (ctx.get("logradouro") or ctx.get("municipio")) else f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=5&sourceCountry=BRA&outFields=*"
        if cands := session.get(url, timeout=4).json().get('candidates'):
            return [{"lat": float(c['location']['y']), "lon": float(c['location']['x']), "fonte": "ARCGIS", "score_base": 30, "cidade": c.get('attributes', {}).get('City', '').upper(), "estado": c.get('attributes', {}).get('RegionAbbr', '').upper(), "bairro": c.get('attributes', {}).get('Neighborhood', '').upper(), "logradouro": c.get('attributes', {}).get('StName', c.get('attributes', {}).get('Address', '')).upper(), "numero": str(c.get('attributes', {}).get('AddNum', '')).upper(), "cep": c.get('attributes', {}).get('Postal', '')} for c in cands[:5]]
    except Exception: pass
    return []

def API_Nominatim(query, ctx=None):
    try:
        def _call_nom():
            time.sleep(1.1)
            url = f"https://nominatim.openstreetmap.org/search?format=json&street={requests.utils.quote(ctx['logradouro'])}&city={requests.utils.quote(ctx['municipio'])}&state={requests.utils.quote(ctx.get('uf', ''))}&limit=5&addressdetails=1&countrycodes=br" if ctx and ctx.get("logradouro") and ctx.get("municipio") else f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query)}&limit=5&addressdetails=1&countrycodes=br"
            return session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
        if r := st.session_state["fila_nominatim"].submit(_call_nom).result():
            return [{"lat": float(a['lat']), "lon": float(a['lon']), "fonte": "NOMINATIM", "score_base": 25, "cidade": a.get("address", {}).get('city', a.get("address", {}).get('town', '')).upper(), "estado": a.get("address", {}).get('state', '').upper(), "bairro": a.get("address", {}).get('neighbourhood', a.get("address", {}).get('suburb', '')).upper(), "logradouro": a.get("address", {}).get('road', '').upper(), "numero": str(a.get("address", {}).get('house_number', '')).upper(), "cep": a.get("address", {}).get('postcode', '').replace("-", "")} for a in r[:5]]
    except Exception: pass
    return []

def API_Photon(query):
    try:
        if r := session.get(f"https://photon.komoot.io/api/?q={requests.utils.quote(query)}&limit=5&filter=countrycode:br", timeout=4).json().get("features"):
            return [{"lat": f["geometry"]["coordinates"][1], "lon": f["geometry"]["coordinates"][0], "fonte": "PHOTON", "score_base": 20, "cidade": f.get("properties", {}).get("city", "").upper(), "estado": f.get("properties", {}).get("state", "").upper(), "bairro": f.get("properties", {}).get("district", "").upper(), "logradouro": f.get("properties", {}).get("street", "").upper(), "numero": str(f.get("properties", {}).get("housenumber", "")).upper(), "cep": f.get("properties", {}).get("postcode", "").replace("-", "")} for f in r[:5]]
    except Exception: pass
    return []

def API_Overpass_POIs(texto_norm):
    if len(texto_norm) < 10: return []
    if texto_norm in cache_poi: return cache_poi[texto_norm]
    for url in ["https://overpass-api.de/api/interpreter", "https://lz4.overpass-api.de/api/interpreter"]:
        try:
            if elems := session.post(url, data={"data": f'[out:json][timeout:3];(node["name"~"{re.escape(texto_norm)}",i]["amenity"];way["name"~"{re.escape(texto_norm)}",i]["amenity"];node["name"~"{re.escape(texto_norm)}",i]["building"];way["name"~"{re.escape(texto_norm)}",i]["building"];node["name"~"{re.escape(texto_norm)}",i]["healthcare"];way["name"~"{re.escape(texto_norm)}",i]["healthcare"];);out center;'}, timeout=4).json().get("elements", []):
                tags = elems[0].get("tags", {})
                res_poi = {"lat": elems[0].get("lat", elems[0].get("center", {}).get("lat", 0.0)), "lon": elems[0].get("lon", elems[0].get("center", {}).get("lon", 0.0)), "fonte": "OVERPASS", "score_base": 40, "cidade": tags.get("addr:city", "").upper(), "estado": tags.get("addr:state", "").upper(), "bairro": tags.get("addr:suburb", "").upper(), "logradouro": tags.get("addr:street", "").upper(), "numero": str(tags.get("addr:housenumber", "")).upper(), "cep": tags.get("addr:postcode", "").replace("-", "")}
                cache_poi.set(texto_norm, [res_poi], expire=7776000); return [res_poi]
        except Exception: continue
    return []

# ==============================================================================
# 🧠 MOTOR DE CONSENSO STATELESS MULTIDIMENSIONAL
# ==============================================================================
def processar_consenso_dinamico(candidatos, tipo_entrada, texto_cru):
    candidatos_validos = []
    ctx_inf = semantica.resolver_contexto_administrativo(texto_cru.upper())
    uf_inf, mun_inf, dist_inf = ctx_inf.get("uf", ""), ctx_inf.get("municipio", ""), ctx_inf.get("distrito", "")
    box = BOUNDING_BOXES_UF.get(uf_inf) if uf_inf else None
    
    for c in candidatos:
        valido, lat_c, lon_c = validar_coordenada_brasil(c["lat"], c["lon"])
        if valido and (not box or (box["lat_min"] <= lat_c <= box["lat_max"] and box["lon_min"] <= lon_c <= box["lon_max"])):
            c["lat"], c["lon"] = lat_c, lon_c; candidatos_validos.append(c)
            
    if not candidatos_validos: return None, "Fora da Bounding Box ou Inválido"
    
    validados_semantica = []
    for c in candidatos_validos:
        cid_api, est_api = unidecode(c.get('cidade', '')).upper().strip(), unidecode(c.get('estado', '')).upper().strip()
        if cid_api and est_api:
            if (cid_api in IBGE_MUNICIPIOS and any(item["uf"] == est_api for item in IBGE_MUNICIPIOS[cid_api])) or (cid_api in IBGE_DISTRITOS and any(item["uf"] == est_api for item in IBGE_DISTRITOS[cid_api])): validados_semantica.append(c)
            elif cid_api not in IBGE_MUNICIPIOS and cid_api not in IBGE_DISTRITOS: validados_semantica.append(c)
        elif cid_api:
            if cid_api in IBGE_MUNICIPIOS or cid_api in IBGE_DISTRITOS: validados_semantica.append(c)
        else: validados_semantica.append(c)
        
    candidatos_validos = validados_semantica
    if not candidatos_validos: return None, "Falha na Validação Semântica IBGE"

    raio_cluster_km = 0.5 if tipo_entrada in ["ENDERECO_COMPLETO", "POI", "CEP"] else 2.0 if tipo_entrada in ["BAIRRO", "RURAL"] else 10.0
    clusters = []
    for c in candidatos_validos:
        alocado = False
        for cluster in clusters:
            if unidecode(c.get('cidade', '')).upper() == unidecode(cluster[0].get('cidade', '')).upper() and fuzz.token_set_ratio(c.get('bairro', ''), cluster[0].get('bairro', '')) > 90 and calcular_distancia_vincenty(c["lat"], c["lon"], cluster[0]["lat"], cluster[0]["lon"]) <= raio_cluster_km:
                cluster.append(c); alocado = True; break
        if not alocado: clusters.append([c])
        
    if clusters:
        tamanho_maior_cluster = max(len(cluster) for cluster in clusters)
        if tamanho_maior_cluster > 1: candidatos_validos = [c for cluster in clusters if len(cluster) == tamanho_maior_cluster for c in cluster]
    if not candidatos_validos: return None, "Clusters Espaciais Inconsistentes"

    input_usuario = ParserGeograficoBR.extrair_componentes(texto_cru.upper())

    if c_uf := [c for c in candidatos_validos if validar_consistencia_administrativa(c, uf_inf)]: candidatos_validos = c_uf
    if c_mun := [c for c in candidatos_validos if validar_consistencia_municipal(c, mun_inf)]: candidatos_validos = c_mun
        
    for c1 in candidatos_validos:
        score_centesimal = c1["score_base"]
        
        feat_mun = 1 if mun_inf and c1.get("cidade") and (mun_inf in c1["cidade"] or fuzz.token_set_ratio(mun_inf, c1["cidade"]) >= 95) else 0
        feat_uf = 1 if uf_inf and c1.get("estado") and uf_inf in c1["estado"] else 0
        feat_cep = 1 if input_usuario.get("cep") and c1.get("cep") and input_usuario["cep"] in c1["cep"].replace("-", "") else 0
        fuzz_rua = fuzz.token_set_ratio(texto_cru.upper(), c1.get("logradouro", "")) if c1.get("logradouro") else 0
        feat_bairro = 1 if dist_inf and c1.get("bairro") and dist_inf in c1["bairro"] else 0
        feat_numero = 1 if input_usuario.get("numero") and c1.get("numero") and input_usuario["numero"] in c1["numero"] else 0
        
        input_tem_rodovia = bool(re.search(r'\b(BR|RODOVIA|KM|ESTRADA)\b', texto_cru.upper()))
        api_tem_rodovia = bool(re.search(r'\b(BR|RODOVIA|KM|ESTRADA)\b', c1.get("logradouro", "").upper()))
        feat_punicao_rodovia = 1 if not input_tem_rodovia and api_tem_rodovia else 0
        
        consenso_espacial = sum(1 for c2 in candidatos_validos if c1["fonte"] != c2["fonte"] and calcular_distancia_vincenty(c1["lat"], c1["lon"], c2["lat"], c2["lon"]) <= raio_cluster_km)

        if MODELO_ML_GEO is not None:
            try:
                c1["score_final"] = MODELO_ML_GEO.predict_proba([[c1["score_base"], feat_mun, feat_uf, feat_cep, fuzz_rua, feat_bairro, feat_numero, feat_punicao_rodovia, consenso_espacial]])[0][1] * 100
                continue 
            except Exception: pass
            
        if feat_mun: score_centesimal += 50
        if feat_uf: score_centesimal += 20
        if feat_cep: score_centesimal += 20
        if fuzz_rua > 80: score_centesimal += 10
        if feat_bairro: score_centesimal += 15
        if feat_numero: score_centesimal += 25
        if feat_punicao_rodovia: score_centesimal -= 60
        
        api_end_str = f"{c1.get('logradouro','')} {c1.get('bairro','')} {c1.get('cidade','')} {c1.get('estado','')}".upper()
        if tipo_entrada == "RURAL" and any(urb in api_end_str for urb in ["QUADRA ", "SQN ", "SQS ", "APARTAMENTO ", "EDIFICIO ", "BLOCO "]): score_centesimal -= 60
        if tipo_entrada in ["ENDERECO_COMPLETO", "BAIRRO"] and any(rur in api_end_str for rur in ["CHACARA ", "FAZENDA ", "GLEBA "]): score_centesimal -= 40
            
        c1["score_final"] = score_centesimal + (consenso_espacial * 35)
        
    candidatos_validos.sort(key=lambda x: x["score_final"], reverse=True)
    
    vencedor = None
    for cand in candidatos_validos[:3]:
        m = executar_reverse_geocoding_multimotor(cand["lat"], cand["lon"])
        estado_reverse, cidade_reverse = m.get("estado", "").upper().strip(), m.get("cidade", "").upper().strip()
        
        if uf_inf and estado_reverse and uf_inf != estado_reverse: continue 
        if mun_inf and cidade_reverse and not ((mun_inf in cidade_reverse) or (cidade_reverse in mun_inf) or (fuzz.token_set_ratio(mun_inf, cidade_reverse) >= 85)): continue
        
        end_reverse = ", ".join([c for c in [m.get("logradouro", ""), m.get("bairro", ""), m.get("cidade", ""), estado_reverse] if c.strip()])
        if fuzz.token_set_ratio(texto_cru.upper(), end_reverse.upper()) >= 70:
            vencedor = cand; break
            
    if not vencedor: return None, "Candidatos reprovados na Validação Reversa (Top 3)"
    score_consenso = min(int(vencedor["score_final"]), 100)
    
    if tipo_entrada in ["ENDERECO_COMPLETO", "CEP"] and score_consenso < 80: return None, "Score de Consenso < 80"
    
    m = {"logradouro": vencedor.get("logradouro", ""), "bairro": vencedor["bairro"], "cidade": vencedor["cidade"], "municipio": vencedor["cidade"], "distrito": "", "estado": vencedor["estado"], "cep": vencedor.get("cep", "")}
    score_completude = 100 if tipo_entrada == "CEP" else 95 if (bool(input_usuario.get("numero") or input_usuario.get("complemento")) and bool(mun_inf) and bool(uf_inf)) else 80 if (bool(mun_inf) and bool(uf_inf)) else 70 if bool(mun_inf) else 60
    if tipo_entrada == "POI": score_completude = 90
    elif tipo_entrada == "RURAL": score_completude = 75
    elif tipo_entrada == "BAIRRO": score_completude = 60

    score_limitado = min(score_consenso, score_completude)
    if m.get("cep") and score_limitado < 100: score_limitado = min(score_limitado + 10, 100 if tipo_entrada == "CEP" else 95)
    confianca = "MUNICIPAL" if tipo_entrada in ["ENDERECO_COMPLETO", "CEP"] and not vencedor.get("logradouro") else "ALTISSIMA" if score_limitado >= 85 else "ALTA" if score_limitado >= 75 else "MEDIA" if score_limitado >= 60 else "BAIXA"

    rua_f = m["logradouro"] if m["logradouro"] else texto_cru.upper()
    return vencedor, ", ".join([c for c in [rua_f, m["bairro"], m["cidade"], m["estado"]] if c.strip()]) + ", BRASIL", confianca, score_limitado, m

# ==============================================================================
# 🎚️ ORQUESTRADOR EM CASCATA HIERÁRQUICA COM TELEMETRIA
# ==============================================================================
def obter_coordenadas_e_endereco_oficial(localidade):
    texto_cru = str(localidade).strip()
    if not texto_cru or texto_cru.lower() == 'nan': return 0.0, 0.0, "", "BAIXA", 0, "", "", "N/A", 0, "Texto Vazio"
    
    if (chave_feedback := texto_cru.upper()) in cache_feedback:
        d = cache_feedback[chave_feedback]
        if isinstance(d, dict) and "lat" in d and "lon" in d: return d["lat"], d["lon"], d.get("endereco", texto_cru.upper()), "ABSOLUTA", 100, d.get("distrito", ""), d.get("municipio", ""), "FEEDBACK_HUMANO", 1, "OK"

    if match_decimal := re.search(r'([-+]?\d{1,2}\.\d+)\s*,\s*([-+]?\d{1,3}\.\d+)', texto_cru):
        valido, lat_b, lon_b = validar_coordenada_brasil(float(match_decimal.group(1)), float(match_decimal.group(2)))
        if valido: return lat_b, lon_b, f"COORDENADA GPS: {lat_b}, {lon_b}", "ALTISSIMA", 100, "", "", "GPS_DIRECT", 1, "OK"
        
    if match_dms := re.search(r"(\d+)[°\s](\d+)['\s](\d+(?:\.\d+)?)[″\"\s]*([NS])\s*[,;\s]\s*(\d+)[°\s](\d+)['\s](\d+(?:\.\d+)?)[″\"\s]*([EW])", texto_cru, re.IGNORECASE):
        valido, lat_b, lon_b = validar_coordenada_brasil((float(match_dms.group(1)) + float(match_dms.group(2))/60 + float(match_dms.group(3))/3600) * (-1 if match_dms.group(4).upper() == 'S' else 1), (float(match_dms.group(5)) + float(match_dms.group(6))/60 + float(match_dms.group(7))/3600) * (-1 if match_dms.group(8).upper() == 'W' else 1))
        if valido: return lat_b, lon_b, f"COORDENADA GPS: {lat_b}, {lon_b}", "ALTISSIMA", 100, "", "", "GPS_DIRECT", 1, "OK"

    if (chave_aprendizado_coord := texto_cru.upper()) in cache_aprendizado:
        d = cache_aprendizado[chave_aprendizado_coord]
        if isinstance(d, dict) and "lat" in d and "lon" in d: return d["lat"], d["lon"], d.get("endereco", texto_cru.upper()), "ALTISSIMA", 100, d.get("distrito", ""), d.get("municipio", ""), "APRENDIZADO_LOCAL", 1, "OK"

    if cep_match := re.search(r'\b\d{5}-?\d{3}\b', texto_cru):
        logr, bair, loca, uf, lat_c, lon_c = cascata_postal_tripla(cep_match.group(0).replace("-", ""))
        if loca:
            addr_c = re.sub(r',\s*,', ',', f"{logr}, {bair}, {loca}, {IBGE_ESTADOS.get(uf, uf)}, CEP {cep_match.group(0)}, BRASIL").strip(' ,')
            val_c, lat_c, lon_c = validar_coordenada_brasil(lat_c, lon_c)
            if val_c and lat_c != 0.0 and lon_c != 0.0:
                cache_geo.set(f"CEP_{addr_c}", {"lat": lat_c, "lon": lon_c, "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca, "fonte": "BrasilAPI/OSM Postal"}, expire=2592000)
                return lat_c, lon_c, addr_c, "ALTISSIMA", 100, bair, loca, "BrasilAPI/OSM Postal", 1, "OK"
            texto_cru = addr_c 

    endereco_canonico, tipo_entrada, _, _, _ = semantica.construir_endereco_canonico(texto_cru)
    ctx = semantica.resolver_contexto_administrativo(texto_cru.upper())
    parsed_comp = ParserGeograficoBR.extrair_componentes(texto_cru.upper())
    
    cache_key = hashlib.md5(f"{tipo_entrada}_{endereco_canonico}".encode('utf-8')).hexdigest()
    if cache_key in cache_geo:
        c = cache_geo[cache_key]
        return c["lat"], c["lon"], c["endereco"], c["confianca"], c["score_num"], c["distrito"], c["municipio"], c["fonte"], 1, "OK"

    rua_suja = parsed_comp["resto"]
    for loc in [ctx.get("municipio", ""), ctx.get("distrito", ""), ctx.get("uf", ""), "BRASIL", "DF"]:
        if loc: rua_suja = re.sub(rf'\b{loc}\b', '', rua_suja).strip(" ,-")
    rua_limpa = re.sub(r'\s+', ' ', rua_suja).strip()
    if parsed_comp["numero"]: rua_limpa = f"{rua_limpa} {parsed_comp['numero']}".strip()
    
    contexto_estruturado = {"logradouro": rua_limpa if rua_limpa else texto_cru.upper(), "bairro": ctx.get("distrito", ""), "municipio": ctx.get("municipio", ""), "uf": ctx.get("uf", ""), "cep": parsed_comp.get("cep", "")}

    if contexto_estruturado["logradouro"] and contexto_estruturado["municipio"] and contexto_estruturado["uf"]:
        if (chave_cnefe := f"{contexto_estruturado['logradouro']}_{contexto_estruturado['municipio']}_{contexto_estruturado['uf']}") in cache_base_local:
            b = cache_base_local[chave_cnefe]
            return b["lat"], b["lon"], b["endereco"], "ALTISSIMA", 100, b.get("distrito", ""), b.get("municipio", ""), "BASE_NACIONAL_OFFLINE", 1, "OK"

    if not ctx.get("municipio") and tipo_entrada not in ["POI", "CEP"]: return 0.0, 0.0, endereco_canonico, "BAIXA", 0, "", "", "N/A", 0, "Sem Âncora Municipal"

    if tipo_entrada == "MUNICIPIO" and ctx.get("municipio") and ctx.get("uf"):
        if ctx["municipio"] in IBGE_MUNICIPIOS:
            for item in IBGE_MUNICIPIOS[ctx["municipio"]]:
                if item["uf"] == ctx["uf"] and item.get("lat", 0.0) != 0.0 and item.get("lon", 0.0) != 0.0:
                    res_ibge = (item["lat"], item["lon"], f"{ctx['municipio']}, {IBGE_ESTADOS.get(ctx['uf'], ctx['uf'])}, BRASIL", "ALTISSIMA", 100, "", ctx["municipio"], "BASE_IBGE_LOCAL", 1, "OK")
                    cache_geo.set(cache_key, {"lat": res_ibge[0], "lon": res_ibge[1], "endereco": res_ibge[2], "confianca": res_ibge[3], "score_num": res_ibge[4], "distrito": res_ibge[5], "municipio": res_ibge[6], "fonte": res_ibge[7]}, expire=2592000); return res_ibge

    candidatos_validos = []
    def disparar_apis_paralelas(tarefas):
        resultados = []
        futuros = [st.session_state["executor_apis"].submit(func, *args, **kwargs) for func, args, kwargs in tarefas]
        for f in as_completed(futuros):
            if res := f.result(): resultados.extend(res)
        return resultados

    if tipo_entrada == "POI": candidatos_validos.extend(disparar_apis_paralelas([(API_Google_Geocoding_Scraper, (endereco_canonico,), {}), (API_Overpass_POIs, (semantica.normalizar(texto_cru),), {})]))
    elif tipo_entrada in ["ENDERECO_COMPLETO", "LOGRADOURO"]:
        candidatos_validos.extend(disparar_apis_paralelas([(API_ArcGIS, (endereco_canonico,), {"ctx": contexto_estruturado}), (API_Google_Geocoding_Scraper, (endereco_canonico,), {})]))
        if res_nom := API_Nominatim(endereco_canonico, ctx=contexto_estruturado): candidatos_validos.extend(res_nom)
    elif tipo_entrada in ["BAIRRO", "MUNICIPIO", "DISTRITO"]:
        candidatos_validos.extend(disparar_apis_paralelas([(API_Photon, (endereco_canonico,), {})]))
        if res_nom := API_Nominatim(endereco_canonico, ctx=contexto_estruturado): candidatos_validos.extend(res_nom)
    else: candidatos_validos.extend(disparar_apis_paralelas([(API_Google_Geocoding_Scraper, (endereco_canonico,), {}), (API_Photon, (endereco_canonico,), {}), (API_ArcGIS, (endereco_canonico,), {"ctx": contexto_estruturado})]))
            
    qtd_cand = len(candidatos_validos)
    if qtd_cand == 0: return 0.0, 0.0, endereco_canonico, "BAIXA", 0, "", "", "N/A", 0, "Nenhum candidato retornado pelas APIs"
    
    consenso_ret = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru)
    
    if (not consenso_ret or consenso_ret[0] is None) and tipo_entrada not in ["BAIRRO", "MUNICIPIO"]:
        if res_nom := API_Nominatim(endereco_canonico, ctx=contexto_estruturado):
            candidatos_validos.extend(res_nom)
            consenso_ret = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru)

    if consenso_ret and consenso_ret[0] is not None:
        vencedor, endereco_f, confianca, score_limitado, m = consenso_ret
        cache_geo.set(cache_key, {"lat": vencedor["lat"], "lon": vencedor["lon"], "endereco": endereco_f, "confianca": confianca, "score_num": score_limitado, "distrito": m["distrito"], "municipio": m["municipio"], "fonte": vencedor["fonte"]}, expire=2592000)
        return vencedor["lat"], vencedor["lon"], endereco_f, confianca, score_limitado, m["distrito"], m["municipio"], vencedor["fonte"], qtd_cand, "OK"
        
    if tipo_entrada in ["MUNICIPIO", "DISTRITO"] and ctx.get("municipio") and ctx.get("uf"):
        cidade_resgate = ctx["municipio"]
        if cidade_resgate not in IBGE_MUNICIPIOS:
            if best_match := process.extractOne(cidade_resgate, list(IBGE_MUNICIPIOS.keys()), scorer=fuzz.WRatio):
                if best_match[1] > 85: cidade_resgate = best_match[0]
        if cidade_resgate in IBGE_MUNICIPIOS:
            for item in IBGE_MUNICIPIOS[cidade_resgate]:
                if item["uf"] == ctx["uf"] and item.get("lat", 0.0) != 0.0 and item.get("lon", 0.0) != 0.0:
                    res_ibge = (item["lat"], item["lon"], f"{cidade_resgate}, {IBGE_ESTADOS.get(ctx['uf'], ctx['uf'])}, BRASIL", "MUNICIPAL_FALLBACK", 80, "", cidade_resgate, "BASE_IBGE_FALLBACK", qtd_cand, "Consenso Falhou - Resgate Fuzzy Centróide")
                    cache_geo.set(cache_key, {"lat": res_ibge[0], "lon": res_ibge[1], "endereco": res_ibge[2], "confianca": res_ibge[3], "score_num": res_ibge[4], "distrito": res_ibge[5], "municipio": res_ibge[6], "fonte": res_ibge[7]}, expire=2592000)
                    return res_ibge
                    
    return 0.0, 0.0, endereco_canonico, "BAIXA", 0, "", "", "N/A", qtd_cand, consenso_ret[1] if consenso_ret else "Erro de Consenso Desconhecido"

# ==============================================================================
# 🚀 MOTOR DE ROTEAMENTO (ARBITRAGEM MULTI-PROVEDORES O(1))
# ==============================================================================
def extrair_dados_reais_google(origem_raw, destino_raw, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=True):
    cache_key = f"{origem_raw}|{destino_raw}|{usar_coordenadas}"
    if cache_key in cache_google: return cache_google[cache_key]
    if not usar_coordenadas and lat_d != 0.0 and lon_d != 0.0:
        if (google_dest_geo := API_Google_Geocoding_Scraper(destino_raw)) and calcular_distancia_vincenty(lat_d, lon_d, google_dest_geo[0]["lat"], google_dest_geo[0]["lon"]) > 20.0: return None 

    url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{f'{lat_o},{lon_o}' if usar_coordenadas else requests.utils.quote(origem_raw)}!1m2!1m1!1s{f'{lat_d},{lon_d}' if usar_coordenadas else requests.utils.quote(destino_raw)}!3e0"
    try:
        texto_resposta = session.get(url_api, headers={"User-Agent": "Mozilla/5.0"}, timeout=8).text
        if len(texto_resposta) < 500 or "directions" not in texto_resposta.lower(): return None
        if (match_km := re.findall(r'\"(\d+[\.,]?\d*)\s*km\"', texto_resposta)) and (match_tempo := re.findall(r'\"(\d+\s*h\s*\d+\s*min|\d+\s*h|\d+\s*min)\"', texto_resposta)):
            km_puro = float(match_km[0].replace('.', '').replace(',', '.'))
            if dist_linha_reta and dist_linha_reta > 0:
                if dist_linha_reta <= 50.0 and km_puro > max(dist_linha_reta * 2.0, dist_linha_reta + 15.0): return None  
                elif km_puro < dist_linha_reta * 0.8 or km_puro > dist_linha_reta * 4.0: return None  

            envolve_balsa = "Sim" if any(re.search(p, texto_resposta.lower()) for p in [r'\"utilizar\s+balsa\b', r'\"ferry\b', r'\bbalsa\b', r'\bbarca\b', r'\btravessia\s+de\s+barco\b', r'\bferry\s+boat\b']) else "Não"
            res = (km_puro, match_tempo[0], f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(origem_raw)}&destination={requests.utils.quote(destino_raw)}&travelmode=driving", envolve_balsa, 70 + (10 if km_puro > 0 else 0) + (10 if match_tempo[0] else 0) + (10 if dist_linha_reta and km_puro >= dist_linha_reta else 0))
            cache_google.set(cache_key, res, expire=2592000); return res
    except Exception: pass
    return None

def rota_osrm(lat_o, lon_o, lat_d, lon_d):
    try:
        if r := session.get(f"https://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=false", timeout=5).json().get("routes"):
            m = round(r[0]["duration"] / 60)
            return round(r[0]["distance"] / 1000, 2), f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min", "OSRM", 95
    except Exception: pass
    return None

def rota_graphhopper(lat_o, lon_o, lat_d, lon_d):
    if not GRAPHHOPPER_API_KEY: return None
    try:
        if p := session.get(f"https://graphhopper.com/api/1/route?point={lat_o},{lon_o}&point={lat_d},{lon_d}&profile=car&locale=pt_BR&calc_points=false&key={GRAPHHOPPER_API_KEY}", timeout=5).json().get("paths"):
            m = round(p[0]["time"] / 60000)
            return round(p[0]["distance"] / 1000, 2), f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min", "GraphHopper", 92
    except Exception: pass
    return None

def rota_ors(lat_o, lon_o, lat_d, lon_d):
    if not ORS_API_KEY: return None
    try:
        if f := session.get(f"https://api.openrouteservice.org/v2/directions/driving-car?start={lon_o},{lat_o}&end={lon_d},{lat_d}", headers={"Authorization": ORS_API_KEY}, timeout=5).json().get("features"):
            m = round(f[0]["properties"]["segments"][0]["duration"] / 60)
            return round(f[0]["properties"]["segments"][0]["distance"] / 1000, 2), f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min", "OpenRouteService", 90
    except Exception: pass
    return None

def calcular_pipeline_logistico(origem, destino, perfil_rota="shortest"):
    start_total = time.time()
    origem_clean, destino_clean = str(origem).strip(), str(destino).strip()
    
    chave_rota_cache = hashlib.md5(f"ROTA_{semantica.normalizar(origem_clean)}->{semantica.normalizar(destino_clean)}".encode('utf-8')).hexdigest()
    if chave_rota_cache in cache_rotas: return cache_rotas[chave_rota_cache]
    
    start_geo = time.time()
    lat_o, lon_o, end_oficial_o, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, qtd_cand_o, motivo_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    lat_d, lon_d, end_oficial_d, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, qtd_cand_d, motivo_d = obter_coordenadas_e_endereco_oficial(destino_clean)
    tempo_geocoding = round(time.time() - start_geo, 2)
    start_rot = time.time()

    if all([lat_o is not None, lon_o is not None, lat_d is not None, lon_d is not None]) and lat_o != 0.0 and lat_d != 0.0:
        dist_linha_reta, flag_geocoding_falhou = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d), False
    else: dist_linha_reta, flag_geocoding_falhou = None, True

    if flag_geocoding_falhou:
        retorno_falha = ("GEOCODING_FALHOU", "GEOCODING_FALHOU", "N/A", "N/A", "GEOCODING_FALHOU", "Falha de Origem/Destino", 0, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, 0.0, round(time.time() - start_total, 2), qtd_cand_o, motivo_o, qtd_cand_d, motivo_d)
        cache_rotas.set(chave_rota_cache, retorno_falha, expire=2592000); return retorno_falha

    usar_coords = False if dist_linha_reta > 150.0 and len(set(re.findall(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM|PA|MT|MS)\b', origem_clean.upper() + " " + destino_clean.upper()))) <= 1 else True
    link_fallback = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(end_oficial_o)}&destination={requests.utils.quote(end_oficial_d)}&travelmode=driving"

    res_osrm, res_gh, res_ors = None, None, None
    if usar_coords:
        res_osrm, res_gh, res_ors = rota_osrm(lat_o, lon_o, lat_d, lon_d), rota_graphhopper(lat_o, lon_o, lat_d, lon_d), rota_ors(lat_o, lon_o, lat_d, lon_d)
        if perfil_rota == "fastest" and (campeao_rapido := res_osrm or res_gh or res_ors):
            retorno = (campeao_rapido[0], campeao_rapido[1], link_fallback, "Não", dist_linha_reta, campeao_rapido[2], campeao_rapido[3], conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, round(time.time() - start_rot, 2), round(time.time() - start_total, 2), qtd_cand_o, motivo_o, qtd_cand_d, motivo_d)
            cache_rotas.set(chave_rota_cache, retorno, expire=2592000); return retorno

    res_google = extrair_dados_reais_google(end_oficial_o, end_oficial_d, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=usar_coords)

    if perfil_rota == "shortest":
        opcoes = [opt for opt in [(res_osrm[0], res_osrm[1], link_fallback, "Não", dist_linha_reta, res_osrm[2], res_osrm[3]) if res_osrm else None, (res_gh[0], res_gh[1], link_fallback, "Não", dist_linha_reta, res_gh[2], res_gh[3]) if res_gh else None, (res_ors[0], res_ors[1], link_fallback, "Não", dist_linha_reta, res_ors[2], res_ors[3]) if res_ors else None, (res_google[0], res_google[1], res_google[2], res_google[3], dist_linha_reta, "Google Preview", res_google[4]) if res_google else None] if opt]
        if opcoes:
            melhor_opcao = min(opcoes, key=lambda x: x[0]) 
            retorno = (*melhor_opcao, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, round(time.time() - start_rot, 2), round(time.time() - start_total, 2), qtd_cand_o, motivo_o, qtd_cand_d, motivo_d)
            cache_rotas.set(chave_rota_cache, retorno, expire=2592000); return retorno

    if res_google:
        retorno = (res_google[0], res_google[1], res_google[2], res_google[3], dist_linha_reta, "Google Preview", res_google[4], conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, round(time.time() - start_rot, 2), round(time.time() - start_total, 2), qtd_cand_o, motivo_o, qtd_cand_d, motivo_d)
        cache_rotas.set(chave_rota_cache, retorno, expire=2592000); return retorno

    km_terrestre = round(dist_linha_reta * (1.45 if dist_linha_reta < 5.0 else 1.35 if dist_linha_reta < 20.0 else 1.25 if dist_linha_reta < 100.0 else 1.18), 2)
    minutos_est = round((km_terrestre / (45.0 if km_terrestre < 50.0 else 65.0)) * 60) if km_terrestre > 0 else 0
    retorno = (km_terrestre, f"{minutos_est} min" if minutos_est < 60 else f"{minutos_est // 60} h {minutos_est % 60} min", link_fallback, "Não", dist_linha_reta, "Geodésico Adaptativo", 70, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, round(time.time() - start_rot, 2), round(time.time() - start_total, 2), qtd_cand_o, motivo_o, qtd_cand_d, motivo_d)
    cache_rotas.set(chave_rota_cache, retorno, expire=2592000); return retorno

def embrulhar_task_paralela(item):
    par_id, orig, dest = item
    try: return par_id, calcular_pipeline_logistico(orig, dest, perfil_rota="shortest")
    except Exception: return par_id, None

# ==============================================================================
# 🚗 INTERFACE STREAMLIT COM VETORIZAÇÃO PANDAS
# ==============================================================================
st.title("🚗 Gerenciador de Rotas Inteligentes")
st.subheader("Engine de Resolução Espacial Nacional — Operação Corporativa")
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
            st.error(f"⚠️ Limite arquitetural de {MAX_LINHAS} linhas excedido. Fracione o arquivo."); st.stop()
            
        st.success(f"Tabela com {len(df)} registros mapeada! Pronto para processar.")
        
        if st.button("Iniciar Processamento em Lote"):
            novas_colunas = [
                'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta', 'Fonte da Rota', 'Score da Rota', 
                'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem', 'Fonte Geocoding Origem', 'Endereco Oficial Origem',
                'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino', 'Fonte Geocoding Destino', 'Endereco Oficial Destino',
                'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)', 'Score Final Global', 'Status da Rota',
                'Qtd Candidatos Origem', 'Motivo Falha Origem', 'Qtd Candidatos Destino', 'Motivo Falha Destino'
            ]
            for col in novas_colunas: df[col] = None
                
            pares_unicos, enderecos_unicos, mapeamento_linhas = set(), set(), []
            for index, linha in df.iterrows():
                origem = str(getattr(linha, 'Origem', '')).strip() if pd.notna(getattr(linha, 'Origem', '')) else ""
                destino = str(getattr(linha, 'Destino', '')).strip() if pd.notna(getattr(linha, 'Destino', '')) else ""
                if origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan':
                    par = (origem, destino)
                    pares_unicos.add(par); enderecos_unicos.add(origem); enderecos_unicos.add(destino)
                    mapeamento_linhas.append((index, origem, destino))
            
            if not pares_unicos:
                st.warning("Nenhuma linha contendo endereços válidos detectada."); st.stop()
                
            executor_lote = st.session_state["executor_global"]
            container_status, barra_progresso = st.empty(), st.progress(0)
            
            # Fase 1: Pré-Warming (O(E) Deduplication)
            container_status.text(f"🌍 Fase 1/2: Pré-Geocodificando {len(enderecos_unicos)} endereços únicos globais...")
            futuros_geo = [executor_lote.submit(lambda e: obter_coordenadas_e_endereco_oficial(e), end) for end in enderecos_unicos]
            for i, f in enumerate(as_completed(futuros_geo)): barra_progresso.progress((i + 1) / len(enderecos_unicos))
                
            # Fase 2: Roteamento Paralelo
            resultados_unicos = {}
            futuros_rotas = {executor_lote.submit(embrulhar_task_paralela, (p, p[0], p[1])): p for p in pares_unicos}
            for i, f in enumerate(as_completed(futuros_rotas)):
                par_id, res = f.result()
                resultados_unicos[par_id] = res
                container_status.text(f"🚀 Fase 2/2: Roteamento Assíncrono (Rotas Únicas): {i + 1} / {len(pares_unicos)}")
                barra_progresso.progress((i + 1) / len(pares_unicos))
                
            container_status.text("✨ Consolidando matriz e vetorizando resultados...")
            registros_atualizados = []
            for idx, origem, destino in mapeamento_linhas:
                if res := resultados_unicos.get((origem, destino)):
                    if res[4] == "GEOCODING_FALHOU":
                        status, score_global = "Erro de Geocodificação", 0.0
                    else:
                        score_global = round((0.35 * res[8]) + (0.35 * res[14]) + (0.30 * res[6]), 2)
                        status = "Excelente" if score_global >= 90 else "Boa" if score_global >= 80 else "Aceitável" if score_global >= 70 else "Revisar"
                    
                    registros_atualizados.append({
                        'index': idx, 'Distancia': res[0], 'Tempo': res[1], 'Link da Rota': res[2], 'Balsas': res[3],
                        'Linha Reta': res[4], 'Fonte da Rota': res[5], 'Score da Rota': res[6],
                        'Confianca Origem': res[7], 'Score Num Origem': res[8], 'Distrito Origem': res[9],
                        'Municipio Origem': res[10], 'Fonte Geocoding Origem': res[11], 'Endereco Oficial Origem': res[12],
                        'Confianca Destino': res[13], 'Score Num Destino': res[14], 'Distrito Destino': res[15],
                        'Municipio Destino': res[16], 'Fonte Geocoding Destino': res[17], 'Endereco Oficial Destino': res[18],
                        'Lat Origem': res[19], 'Lon Origem': res[20], 'Lat Destino': res[21], 'Lon Destino': res[22],
                        'Tempo Geocoding (s)': res[23], 'Tempo Roteamento (s)': res[24], 'Tempo Total (s)': res[25],
                        'Qtd Candidatos Origem': res[26], 'Motivo Falha Origem': res[27] if res[27] else "OK",
                        'Qtd Candidatos Destino': res[28], 'Motivo Falha Destino': res[29] if res[29] else "OK",
                        'Score Final Global': score_global, 'Status da Rota': status
                    })
                else: registros_atualizados.append({'index': idx, 'Status da Rota': "Erro de Processamento"})

            if registros_atualizados:
                df_resultados = pd.DataFrame(registros_atualizados).set_index('index')
                for col in df_resultados.columns: df[col] = df_resultados[col]

            container_status.empty(); barra_progresso.empty()
            st.success("✨ Processamento logístico em lote concluído com telemetria ativa!")
            
            ordem_finais = ['Origem', 'Destino'] + novas_colunas
            df = df.reindex(columns=ordem_finais)
            output_buffer = io.BytesIO()
            with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer: df.to_excel(writer, index=False)
            st.session_state['planilha_pronta'] = output_buffer.getvalue()

    if 'planilha_pronta' in st.session_state:
        st.write("---"); st.balloons()
        st.download_button(label="📥 Baixar Planilha Logística Processada", data=st.session_state['planilha_pronta'], file_name="planilha_rotas_calculada.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
