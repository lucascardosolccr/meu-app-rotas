import streamlit as st
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
from unidecode import unidecode
from rapidfuzz import process, fuzz
from diskcache import Cache
from sklearn.neighbors import BallTree
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==============================================================================
# CONFIGURAÇÃO DE UI/UX E AMBIENTE
# ==============================================================================
st.set_page_config(page_title="Gerenciador de Rotas Inteligentes", page_icon="🚗", layout="wide")

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
cache_api_health = Cache("./api_health_metrics")

for c in [cache_classificacao, cache_fuzzy, cache_geo, cache_rotas, cache_poi, cache_cep, cache_google, cache_reverse, cache_base_local, cache_aprendizado, cache_aprendizado_auto, cache_api_health]:
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
# 🎛️ INFRAESTRUTURA DE CONCORRÊNCIA E FILAS (FIM DO EFEITO COMBOIO)
# ==============================================================================
WORKERS_DISPONIVEIS = 8

if "executor_global" not in st.session_state:
    st.session_state["executor_global"] = ThreadPoolExecutor(max_workers=WORKERS_DISPONIVEIS)

if "fila_nominatim" not in st.session_state:
    st.session_state["fila_nominatim"] = ThreadPoolExecutor(max_workers=1)

# ==============================================================================
# 🎛️ DADOS GLOBAIS THREAD-SAFE (RESOLUÇÃO DE HOMÔNIMOS MATRICIAL)
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
                cod_ibge = str(mun["id"])
                
                mun_data = {"uf": uf_sigla, "municipio": nome_norm, "lat": mun.get("lat", 0.0), "lon": mun.get("lon", 0.0)}
                if nome_norm not in base_mun: base_mun[nome_norm] = []
                base_mun[nome_norm].append(mun_data)
                base_mun[cod_ibge] = [mun_data] # Resolução Numérica de Municípios
                
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
    if not k.isdigit():
        for v in v_list: LISTA_CONTEXTO_FUZZY.append(f"{k} {v['uf']}")
for k, v_list in IBGE_DISTRITOS.items(): 
    for v in v_list: LISTA_CONTEXTO_FUZZY.append(f"{k} {v['uf']}")
LISTA_CONTEXTO_FUZZY = list(set(LISTA_CONTEXTO_FUZZY))

SINONIMOS_SEMANTICOS = {
    "UNB": "UNIVERSIDADE DE BRASILIA", "CATOLICA": "UNIVERSIDADE CATOLICA",
    "JK": "JUSCELINO KUBITSCHEK", "HBDF": "HOSPITAL DE BASE DO DISTRITO FEDERAL",
    "HRAN": "HOSPITAL REGIONAL DA ASA NORTE", "RODOVIARIA": "TERMINAL RODOVIARIO"
}

POI_KEYWORDS = [
    "AEROPORTO", "HOSPITAL", "UNIVERSIDADE", "FACULDADE", "ESCOLA", "SHOPPING", 
    "HOTEL", "RODOVIARIA", "ESTADIO", "MINISTERIO", "AGENCIA", "BANCO", 
    "IGREJA", "FORUM", "TRIBUNAL", "DELEGACIA", "PREFEITURA", "CLINICA"
]

BOUNDING_BOXES_UF = {
    "DF": {"lat_min": -16.05, "lat_max": -15.50, "lon_min": -48.30, "lon_max": -47.30},
    "SP": {"lat_min": -25.50, "lat_max": -19.50, "lon_min": -53.50, "lon_max": -44.00},
    "GO": {"lat_min": -19.50, "lat_max": -12.40, "lon_min": -53.30, "lon_max": -45.90}
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
        self.condo_keys = ["CONDOMINIO", "RESIDENCIAL", "VILLAGE", "ALDEIA", "RECANTO", "PORTAL", "PARK", "RESIDENCE"]
        
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
        
        def padronizar_rodovia(match): return f"{match.group(1)}-{match.group(2).zfill(3)}"
        padrao_rodovia = r'\b(BR|AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\s*[-]?\s*(\d+)\b'
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
        if re.fullmatch(r'^\d{7}$', texto_norm): tipo = "MUNICIPIO"
        elif re.search(r'\b\d{5}-?\d{3}\b', texto_norm): tipo = "CEP"
        elif any(k in texto_norm for k in POI_KEYWORDS): tipo = "POI"
        elif any(k in texto_norm for k in self.rural_keys): tipo = "RURAL"
        elif any(k in texto_norm for k in self.via_keys) and bool(re.search(r'\d+', texto_norm)): tipo = "ENDERECO_COMPLETO"
        elif any(k in texto_norm for k in self.condo_keys): tipo = "CONDOMINIO"
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
                        texto_norm = texto_norm.replace(token, melhor_match[0].rsplit(' ', 1)[0])
                        break
        cache_fuzzy.set(texto_norm, texto_norm, expire=2592000)
        return texto_norm

    def resolver_contexto_administrativo(self, texto_norm):
        tokens = texto_norm.split()
        uf_explicita = next((re.sub(r'[^A-Z]', '', t) for t in reversed(tokens) if re.sub(r'[^A-Z]', '', t) in IBGE_ESTADOS), None)

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
                if chunk in IBGE_MUNICIPIOS and not chunk.isdigit():
                    return {"uf": uf_explicita if uf_explicita and any(item["uf"] == uf_explicita for item in IBGE_MUNICIPIOS[chunk]) else IBGE_MUNICIPIOS[chunk][0]["uf"], "municipio": chunk, "distrito": ""}
                if chunk in IBGE_DISTRITOS:
                    return {"uf": uf_explicita if uf_explicita and any(item["uf"] == uf_explicita for item in IBGE_DISTRITOS[chunk]) else IBGE_DISTRITOS[chunk][0]["uf"], "municipio": IBGE_DISTRITOS[chunk][0]["municipio"], "distrito": chunk}
                    
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
                return f"{logr}{num_str}{comp_str}, {bair}, {loca}, {IBGE_ESTADOS.get(uf, uf)}, BRASIL", "CEP", parsed["cep"], lat_cep, lon_cep

        texto_fuzzy = self.aplicar_fuzzy_multidimensional(texto_norm)
        tipo = self.classificar_entrada(texto_fuzzy)
        contexto = self.resolver_contexto_administrativo(texto_fuzzy)
        
        componentes = [texto_fuzzy]
        if contexto["distrito"] and contexto["distrito"] not in texto_fuzzy: componentes.append(contexto["distrito"])
        if contexto["municipio"] and contexto["municipio"] not in texto_fuzzy: componentes.append(contexto["municipio"])
        if contexto["uf"] and IBGE_ESTADOS.get(contexto["uf"], contexto["uf"]) not in texto_fuzzy: componentes.append(IBGE_ESTADOS.get(contexto["uf"], contexto["uf"]))
        if "BRASIL" not in texto_fuzzy: componentes.append("BRASIL")
        
        return re.sub(r',\s*,', ',', ", ".join(componentes)).strip(), tipo, "", 0.0, 0.0

semantica = MotorEnderecoCanônico()

# ==============================================================================
# 🧮 LÓGICA GEODÉSICA E MOTOR DE AUDITORIA QA
# ==============================================================================
def validar_coordenada_brasil(lat, lon):
    try:
        lat_f, lon_f = float(lat), float(lon)
        if (-35.0 <= lat_f <= 6.0) and (-75.0 <= lon_f <= -28.0): return True, lat_f, lon_f
        if (-35.0 <= lon_f <= 6.0) and (-75.0 <= lat_f <= -28.0): return True, lon_f, lat_f 
        return False, lat_f, lon_f
    except (ValueError, TypeError): return False, 0.0, 0.0

def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    if not (-90 <= lat1 <= 90) or not (-90 <= lat2 <= 90) or not (-180 <= lon1 <= 180) or not (-180 <= lon2 <= 180): return 0.0
    if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0 or (lat1 == lat2 and lon1 == lon2): return 0.0
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

def auditoria_geografica(km_rota, minutos_str, dist_linha_reta, lat_o, lon_o, lat_d, lon_d):
    for lat, lon, loc in [(lat_o, lon_o, "Origem"), (lat_d, lon_d, "Destino")]:
        if not (-75.0 <= lon <= -28.0) or not (-35.0 <= lat <= 6.0): return f"AUDITORIA: Coordenada {loc} oceânica ou fora do BR ({lat},{lon})"
    if km_rota and dist_linha_reta and km_rota > 0 and dist_linha_reta > 0:
        if km_rota < (dist_linha_reta * 0.9): return f"AUDITORIA: Violação Geodésica (Rota {km_rota}km < Linha Reta {dist_linha_reta}km)"
        try:
            minutos = float(re.search(r'(\d+)', minutos_str).group(1)) if "min" in minutos_str else float(re.search(r'(\d+)\s*h', minutos_str).group(1)) * 60
            if minutos > 0 and (km_rota / (minutos / 60)) > 160.0: return f"AUDITORIA: Velocidade Absurda Detectada ({round(km_rota / (minutos / 60))} km/h)"
        except Exception: pass
    return None

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
            return session.get(f"https://nominatim.openstreetmap.org/search?format=json&postalcode={cep_limpo}&countrycodes=br&limit=1", headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
        if r_nom := st.session_state["fila_nominatim"].submit(_nom_cep).result(): lat, lon = float(r_nom[0]['lat']), float(r_nom[0]['lon'])
    except Exception: pass
    try:
        r = session.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=4).json()
        if "erro" not in r: d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon); cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception: pass
    try:
        r = session.get(f"https://opencep.com/v1/{cep_limpo}", timeout=4).json()
        if "error" not in r: d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon); cache_cep.set(cep_limpo, d, expire=2592000); return d
    except Exception: pass
    return "", "", "", "", 0.0, 0.0

def validar_consistencia_administrativa(candidato, uf_inf):
    est_api = unidecode(candidato.get('estado', '')).upper().strip()
    return False if uf_inf and est_api and uf_inf != est_api else True

def validar_consistencia_municipal(candidato, mun_inf):
    if not mun_inf: return True
    cid_api = unidecode(candidato.get('cidade', '')).upper().strip()
    return True if cid_api and (mun_inf in cid_api or cid_api in mun_inf or fuzz.token_set_ratio(mun_inf, cid_api) >= 95) else False

# ==============================================================================
# 🗺️ MÓDULOS DE GEOCODIFICAÇÃO E REVERSE
# ==============================================================================
def API_Google_Geocoding_Scraper(query):
    try:
        r = session.get(f"https://www.google.com/maps/search/{requests.utils.quote(query)}", headers={"User-Agent": "Mozilla/5.0"}, timeout=5, allow_redirects=True)
        match = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.url) or re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', r.text)
        if match: return [{"lat": float(match.group(1)), "lon": float(match.group(2)), "fonte": "GOOGLE_MAPS", "score_base": 40, "cidade": "", "estado": "", "bairro": "", "logradouro": "", "numero": "", "cep": ""}]
    except Exception: pass
    return None

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
    return None

def API_Nominatim(query, ctx=None):
    try:
        def _call_nom():
            time.sleep(1.1)
            url = f"https://nominatim.openstreetmap.org/search?format=json&street={requests.utils.quote(ctx['logradouro'])}&city={requests.utils.quote(ctx['municipio'])}&state={requests.utils.quote(ctx.get('uf', ''))}&limit=5&addressdetails=1&countrycodes=br" if ctx and ctx.get("logradouro") and ctx.get("municipio") else f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query)}&limit=5&addressdetails=1&countrycodes=br"
            return session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()
        if r := st.session_state["fila_nominatim"].submit(_call_nom).result():
            return [{"lat": float(a['lat']), "lon": float(a['lon']), "fonte": "NOMINATIM", "score_base": 25, "cidade": a.get("address", {}).get('city', a.get("address", {}).get('town', '')).upper(), "estado": a.get("address", {}).get('state', '').upper(), "bairro": a.get("address", {}).get('neighbourhood', a.get("address", {}).get('suburb', '')).upper(), "logradouro": a.get("address", {}).get('road', '').upper(), "numero": str(a.get("address", {}).get('house_number', '')).upper(), "cep": a.get("address", {}).get('postcode', '').replace("-", "")} for a in r[:5]]
    except Exception: pass
    return None

def API_Photon(query):
    try:
        if r := session.get(f"https://photon.komoot.io/api/?q={requests.utils.quote(query)}&limit=5&filter=countrycode:br", timeout=4).json().get("features"):
            return [{"lat": f["geometry"]["coordinates"][1], "lon": f["geometry"]["coordinates"][0], "fonte": "PHOTON", "score_base": 20, "cidade": f.get("properties", {}).get("city", "").upper(), "estado": f.get("properties", {}).get("state", "").upper(), "bairro": f.get("properties", {}).get("district", "").upper(), "logradouro": f.get("properties", {}).get("street", "").upper(), "numero": str(f.get("properties", {}).get("housenumber", "")).upper(), "cep": f.get("properties", {}).get("postcode", "").replace("-", "")} for f in r[:5]]
    except Exception: pass
    return None

def API_Overpass_POIs(texto_norm):
    if len(texto_norm) < 10: return None
    if texto_norm in cache_poi: return cache_poi[texto_norm]
    for url in ["https://overpass-api.de/api/interpreter", "https://lz4.overpass-api.de/api/interpreter"]:
        try:
            if elems := session.post(url, data={"data": f'[out:json][timeout:3];(node["name"~"{re.escape(texto_norm)}",i]["amenity"];way["name"~"{re.escape(texto_norm)}",i]["amenity"];node["name"~"{re.escape(texto_norm)}",i]["building"];way["name"~"{re.escape(texto_norm)}",i]["building"];node["name"~"{re.escape(texto_norm)}",i]["healthcare"];way["name"~"{re.escape(texto_norm)}",i]["healthcare"];);out center;'}, timeout=4).json().get("elements", []):
                tags = elems[0].get("tags", {})
                res_poi = {"lat": elems[0].get("lat", elems[0].get("center", {}).get("lat", 0.0)), "lon": elems[0].get("lon", elems[0].get("center", {}).get("lon", 0.0)), "fonte": "OVERPASS", "score_base": 40, "cidade": tags.get("addr:city", "").upper(), "estado": tags.get("addr:state", "").upper(), "bairro": tags.get("addr:suburb", "").upper(), "logradouro": tags.get("addr:street", "").upper(), "numero": str(tags.get("addr:housenumber", "")).upper(), "cep": tags.get("addr:postcode", "").replace("-", "")}
                cache_poi.set(texto_norm, [res_poi], expire=7776000); return [res_poi]
        except Exception: continue
    return None

# ==============================================================================
# 🧠 MOTOR DE CONSENSO PROBABILÍSTICO BAYESIANO E BALLTREE CLUSTERING
# ==============================================================================
def processar_consenso_dinamico(candidatos, tipo_entrada, texto_cru):
    candidatos_validos, candidatos_para_avaliacao = [], candidatos.copy()
    ctx_inf = semantica.resolver_contexto_administrativo(texto_cru.upper())
    uf_inf, mun_inf, dist_inf = ctx_inf.get("uf", ""), ctx_inf.get("municipio", ""), ctx_inf.get("distrito", "")
    box = BOUNDING_BOXES_UF.get(uf_inf) if uf_inf else None
    
    # 1. Bounding Box
    for c in candidatos:
        valido, lat_c, lon_c = validar_coordenada_brasil(c["lat"], c["lon"])
        if valido and (not box or (box["lat_min"] <= lat_c <= box["lat_max"] and box["lon_min"] <= lon_c <= box["lon_max"])):
            c["lat"], c["lon"] = lat_c, lon_c; candidatos_validos.append(c)
    if not candidatos_validos: return None

    # 2. Semântica IBGE Matricial
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
    if not candidatos_validos: return None

    # 3. Clustering Espacial O(log N) - BallTree
    raio_cluster_km = 0.5 if tipo_entrada in ["ENDERECO_COMPLETO", "POI", "CEP"] else 2.0 if tipo_entrada in ["BAIRRO", "RURAL"] else 10.0
    raio_radianos = raio_cluster_km / 6371.0
    coords_rad = np.radians([[c["lat"], c["lon"]] for c in candidatos_validos])
    tree = BallTree(coords_rad, metric='haversine')
    indices_vizinhos = tree.query_radius(coords_rad, r=raio_radianos)
    
    visitados, clusters = set(), []
    for i, vizinhos in enumerate(indices_vizinhos):
        if i in visitados: continue
        cluster_atual = []
        for v_idx in vizinhos:
            if unidecode(candidatos_validos[i].get('cidade', '')).upper() == unidecode(candidatos_validos[v_idx].get('cidade', '')).upper() and fuzz.token_set_ratio(candidatos_validos[i].get('bairro', ''), candidatos_validos[v_idx].get('bairro', '')) > 85:
                cluster_atual.append(candidatos_validos[v_idx]); visitados.add(v_idx)
        if cluster_atual: clusters.append(cluster_atual)
        
    if clusters:
        t_max = max(len(cluster) for cluster in clusters)
        if t_max > 1: candidatos_validos = [c for cluster in clusters if len(cluster) == t_max for c in cluster]
    if not candidatos_validos: return None

    # 4. Hard Drops Administrativos
    input_usuario = ParserGeograficoBR.extrair_componentes(texto_cru.upper())
    if c_uf := [c for c in candidatos_validos if validar_consistencia_administrativa(c, uf_inf)]: candidatos_validos = c_uf
    if c_mun := [c for c in candidatos_validos if validar_consistencia_municipal(c, mun_inf)]: candidatos_validos = c_mun
        
    # 5. Score Bayesiano + Self-Tuning + Reputação
    REPUTACAO_API = {"GOOGLE_MAPS": 1.0, "ARCGIS": 0.95, "NOMINATIM": 0.80, "PHOTON": 0.75, "OVERPASS": 0.75}
    
    for c1 in candidatos_validos:
        fonte = c1.get("fonte", "")
        metricas_api = cache_api_health.get(fonte, {"hits": 0, "calls": 0})
        if metricas_api["calls"] >= 20: c1["score_base"] += (metricas_api["hits"] / metricas_api["calls"]) * 10.0
            
        p_prior = min(c1["score_base"] / 100.0, 0.50)
        
        feat_mun = mun_inf and c1.get("cidade") and (mun_inf in c1["cidade"] or fuzz.token_set_ratio(mun_inf, c1["cidade"]) >= 95)
        feat_uf = uf_inf and c1.get("estado") and uf_inf in c1["estado"]
        feat_cep = input_usuario.get("cep") and c1.get("cep") and input_usuario["cep"] in c1["cep"].replace("-", "")
        feat_bairro = dist_inf and c1.get("bairro") and dist_inf in c1["bairro"]
        feat_numero = input_usuario.get("numero") and c1.get("numero") and input_usuario["numero"] in c1["numero"]
        fuzz_rua = fuzz.token_set_ratio(texto_cru.upper(), c1.get("logradouro", "")) / 100.0 if c1.get("logradouro") else 0.1
        
        api_tem_rodovia = bool(re.search(r'\b(BR|RODOVIA|KM|ESTRADA)\b', c1.get("logradouro", "").upper()))
        feat_punicao_rodovia = not bool(re.search(r'\b(BR|RODOVIA|KM|ESTRADA)\b', texto_cru.upper())) and api_tem_rodovia
        
        vizinhos_ponto = tree.query_radius(np.radians([[c1["lat"], c1["lon"]]]), r=raio_radianos)[0]
        consenso_espacial = sum(REPUTACAO_API.get(candidatos_validos[v].get("fonte", ""), 0.5) for v in vizinhos_ponto if c1.get("fonte") != candidatos_validos[v].get("fonte") and v < len(candidatos_validos))
        
        odds = (p_prior / (1 - p_prior)) * (1.8 if feat_mun else 0.4) * (1.3 if feat_uf else 0.7) * (1.5 if feat_cep else 0.9) * (1.2 if feat_bairro else 0.9) * (1.4 if feat_numero else 0.8) * (0.5 + fuzz_rua) * (1.0 + (consenso_espacial * 0.3)) * (0.1 if feat_punicao_rodovia else 1.0) * (0.2 if (tipo_entrada == "RURAL" and any(urb in f"{c1.get('logradouro','')} {c1.get('bairro','')} {c1.get('cidade','')} {c1.get('estado','')}".upper() for urb in ["QUADRA ", "SQN ", "SQS ", "APARTAMENTO ", "BLOCO "])) else 1.0)
        c1["score_final"] = min((odds / (1 + odds)) * 100, 99.9)
        
    candidatos_validos.sort(key=lambda x: x["score_final"], reverse=True)
    
    # 6. Validação Reversa Obrigatória (Top 3)
    vencedor = None
    for cand in candidatos_validos[:3]:
        m = executar_reverse_geocoding_multimotor(cand["lat"], cand["lon"])
        estado_reverse, cidade_reverse = m.get("estado", "").upper().strip(), m.get("cidade", "").upper().strip()
        if uf_inf and estado_reverse and uf_inf != estado_reverse: continue 
        if mun_inf and cidade_reverse and not ((mun_inf in cidade_reverse) or (cidade_reverse in mun_inf) or (fuzz.token_set_ratio(mun_inf, cidade_reverse) >= 85)): continue
        end_reverse = ", ".join([c for c in [m.get("logradouro", ""), m.get("bairro", ""), m.get("cidade", ""), estado_reverse] if c.strip()])
        if fuzz.token_set_ratio(texto_cru.upper(), end_reverse.upper()) >= 70:
            vencedor = cand; break
            
    if not vencedor: return None
    
    # 7. Auto-Aprendizado das APIs
    for cand in candidatos_para_avaliacao:
        if cand.get("lat", 0.0) == 0.0 or cand.get("lon", 0.0) == 0.0: continue
        fonte = cand.get("fonte", "")
        metricas = cache_api_health.get(fonte, {"hits": 0, "calls": 0})
        metricas["calls"] += 1
        if calcular_distancia_vincenty(cand["lat"], cand["lon"], vencedor["lat"], vencedor["lon"]) <= 0.05: metricas["hits"] += 1
        cache_api_health.set(fonte, metricas, expire=None)

    score_consenso = min(int(vencedor["score_final"]), 100)
    if tipo_entrada in ["ENDERECO_COMPLETO", "CEP"] and score_consenso < 80: return None
    
    m = {"logradouro": vencedor.get("logradouro", ""), "bairro": vencedor["bairro"], "cidade": vencedor["cidade"], "municipio": vencedor["cidade"], "distrito": "", "estado": vencedor["estado"], "cep": vencedor.get("cep", "")}
        
    score_completude = 50
    if tipo_entrada == "CEP": score_completude = 100
    elif tipo_entrada == "ENDERECO_COMPLETO":
        tem_numero = bool(input_usuario.get("numero") or input_usuario.get("complemento"))
        tem_cidade = bool(mun_inf); tem_uf = bool(uf_inf)
        if tem_numero and tem_cidade and tem_uf: score_completude = 95
        elif tem_cidade and tem_uf: score_completude = 80
        elif tem_cidade: score_completude = 70
        else: score_completude = 60
    elif tipo_entrada == "POI": score_completude = 90
    elif tipo_entrada == "CONDOMINIO": score_completude = 85
    elif tipo_entrada == "RURAL": score_completude = 75
    elif tipo_entrada == "BAIRRO": score_completude = 60

    score_limitado = min(score_consenso, score_completude)
    if m.get("cep") and score_limitado < 100: score_limitado = min(score_limitado + 10, 100 if tipo_entrada == "CEP" else 95)
    
    confianca = "MUNICIPAL" if tipo_entrada in ["ENDERECO_COMPLETO", "CEP"] and not vencedor.get("logradouro") else "ALTISSIMA" if score_limitado >= 85 else "ALTA" if score_limitado >= 75 else "MEDIA" if score_limitado >= 60 else "BAIXA"

    # 8. Cross-Validation por CEP (Postal Shield)
    if m.get("cep"):
        logr_cep, _, _, _, _, _ = cascata_postal_tripla(m["cep"].replace("-", ""))
        if logr_cep and m.get("logradouro") and fuzz.token_set_ratio(logr_cep, m["logradouro"]) < 50:
            score_limitado = min(score_limitado, 60)
            confianca = "BAIXA"

    rua_f = m["logradouro"] if m["logradouro"] else texto_cru.upper()
    endereco_f = ", ".join([c for c in [rua_f, m["bairro"], m["cidade"], m["estado"]] if c.strip()]) + ", BRASIL"
    return vencedor["lat"], vencedor["lon"], endereco_f, confianca, score_limitado, m["distrito"], m["municipio"], vencedor["fonte"]

# ==============================================================================
# 🎚️ ORQUESTRADOR EM CASCATA HIERÁRQUICA E OFFLINE-FIRST
# ==============================================================================
def obter_coordenadas_e_endereco_oficial(localidade):
    texto_cru = str(localidade).strip()
    if not texto_cru or texto_cru.lower() == 'nan': return 0.0, 0.0, "", "BAIXA", 0, "", "", "N/A"
    
    if re.fullmatch(r'^\d{7}$', texto_cru):
        if texto_cru in IBGE_MUNICIPIOS and (item := IBGE_MUNICIPIOS[texto_cru][0]).get("lat", 0.0) != 0.0:
            return item["lat"], item["lon"], f"{item['municipio']}, {IBGE_ESTADOS.get(item['uf'], item['uf'])}, BRASIL, CÓDIGO {texto_cru}", "ALTISSIMA", 100, "", item["municipio"], "BASE_IBGE_CODIGO"

    chave_auto = texto_cru.upper()
    if chave_auto in cache_aprendizado_auto:
        if isinstance(d := cache_aprendizado_auto[chave_auto], dict) and "lat" in d and "lon" in d:
            return d["lat"], d["lon"], d.get("endereco", texto_cru.upper()), "ALTISSIMA", 100, d.get("distrito", ""), d.get("municipio", ""), "APRENDIZADO_AUTO"

    if chave_auto in cache_aprendizado:
        if isinstance(d := cache_aprendizado[chave_auto], dict) and "lat" in d and "lon" in d:
            return d["lat"], d["lon"], d.get("endereco", texto_cru.upper()), "ALTISSIMA", 100, d.get("distrito", ""), d.get("municipio", ""), "APRENDIZADO_LOCAL"

    endereco_canonico, tipo_entrada, _, _, _ = semantica.construir_endereco_canonico(texto_cru)
    ctx = semantica.resolver_contexto_administrativo(texto_cru.upper())
    parsed_comp = ParserGeograficoBR.extrair_componentes(texto_cru.upper())
    
    if tipo_entrada in ["MUNICIPIO", "DISTRITO"] and ctx.get("municipio") and not ctx.get("uf"):
        if ctx["municipio"] in IBGE_MUNICIPIOS and len(IBGE_MUNICIPIOS[ctx["municipio"]]) > 1:
            return 0.0, 0.0, endereco_canonico, "AMBIGUA", 0, "", ctx["municipio"], f"AMBÍGUO: Múltiplas opções ({', '.join([f'{ctx['municipio']}-{item['uf']}' for item in IBGE_MUNICIPIOS[ctx['municipio']]])})"

    cache_key = hashlib.md5(f"{tipo_entrada}_{endereco_canonico}".encode('utf-8')).hexdigest()
    if cache_key in cache_geo:
        c = cache_geo[cache_key]; return c["lat"], c["lon"], c["endereco"], c["confianca"], c["score_num"], c["distrito"], c["municipio"], c["fonte"]

    rua_suja = parsed_comp["resto"]
    for loc in [ctx.get("municipio", ""), ctx.get("distrito", ""), ctx.get("uf", ""), "BRASIL", "DF"]:
        if loc: rua_suja = re.sub(rf'\b{loc}\b', '', rua_suja).strip(" ,-")
    rua_limpa = re.sub(r'\s+', ' ', rua_suja).strip()
    if parsed_comp["numero"]: rua_limpa = f"{rua_limpa} {parsed_comp['numero']}".strip()
    
    contexto_estruturado = {"logradouro": rua_limpa if rua_limpa else texto_cru.upper(), "bairro": ctx.get("distrito", ""), "municipio": ctx.get("municipio", ""), "uf": ctx.get("uf", ""), "cep": parsed_comp.get("cep", "")}

    if contexto_estruturado["logradouro"] and contexto_estruturado["municipio"] and contexto_estruturado["uf"]:
        chave_cnefe = f"{contexto_estruturado['logradouro']}_{contexto_estruturado['municipio']}_{contexto_estruturado['uf']}"
        if chave_cnefe in cache_base_local:
            b = cache_base_local[chave_cnefe]; return b["lat"], b["lon"], b["endereco"], "ALTISSIMA", 100, b.get("distrito", ""), b.get("municipio", ""), "BASE_NACIONAL_OFFLINE"

    if not ctx.get("municipio") and tipo_entrada not in ["POI", "CEP"]: return 0.0, 0.0, endereco_canonico, "BAIXA", 0, "", "", "N/A"

    candidatos_validos = []

    if tipo_entrada == "CEP":
        cep_estrito = re.search(r'\b\d{5}-?\d{3}\b', texto_cru)
        if cep_estrito:
            cep_limpo = cep_estrito.group(0).replace("-", "")
            logr, bair, loca, uf, lat_c, lon_c = cascata_postal_tripla(cep_limpo)
            if loca:
                addr_c = re.sub(r',\s*,', ',', f"{logr}, {bair}, {loca}, {IBGE_ESTADOS.get(uf, uf)}, CEP {cep_estrito.group(0)}, BRASIL").strip(' ,')
                val_c, lat_corrigida_c, lon_corrigida_c = validar_coordenada_brasil(lat_c, lon_c)
                if val_c and lat_c != 0.0 and lon_c != 0.0:
                    cache_geo.set(cache_key, {"lat": lat_corrigida_c, "lon": lon_corrigida_c, "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca, "fonte": "BrasilAPI/OSM Postal"}, expire=2592000)
                    return lat_corrigida_c, lon_corrigida_c, addr_c, "ALTISSIMA", 100, bair, loca, "BrasilAPI/OSM Postal"
                
                res_arc = API_ArcGIS(addr_c)
                if res_arc:
                    if isinstance(res_arc, list): res_arc = res_arc[0]
                    val_arc, lat_corrigida_arc, lon_corrigida_arc = validar_coordenada_brasil(res_arc["lat"], res_arc["lon"])
                    if val_arc:
                        cache_geo.set(cache_key, {"lat": lat_corrigida_arc, "lon": lon_corrigida_arc, "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca, "fonte": "ViaCEP/ArcGIS"}, expire=2592000)
                        return lat_corrigida_arc, lon_corrigida_arc, addr_c, "ALTISSIMA", 100, bair, loca, "ViaCEP/ArcGIS"

    if tipo_entrada == "MUNICIPIO" and ctx.get("municipio") and ctx.get("uf"):
        mun_nome, uf_nome = ctx["municipio"], ctx["uf"]
        if mun_nome in IBGE_MUNICIPIOS:
            for item in IBGE_MUNICIPIOS[mun_nome]:
                if item["uf"] == uf_nome and item.get("lat", 0.0) != 0.0 and item.get("lon", 0.0) != 0.0:
                    res_ibge = (item["lat"], item["lon"], f"{mun_nome}, {IBGE_ESTADOS.get(uf_nome, uf_nome)}, BRASIL", "ALTISSIMA", 100, "", mun_nome, "BASE_IBGE_LOCAL")
                    cache_geo.set(cache_key, {"lat": res_ibge[0], "lon": res_ibge[1], "endereco": res_ibge[2], "confianca": res_ibge[3], "score_num": res_ibge[4], "distrito": res_ibge[5], "municipio": res_ibge[6], "fonte": res_ibge[7]}, expire=2592000)
                    return res_ibge

    if match_rodovia := re.search(r'\b(BR|AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\s*[-]?\s*(\d+)\b\s*(?:KM|QUILOMETRO)?\s*(\d{1,4})?', texto_cru.upper()):
        if not ctx.get("municipio") and not ctx.get("bairro"):
            texto_cru = f"{match_rodovia.group(1)}-{match_rodovia.group(2)}{f' KM {match_rodovia.group(3)}' if match_rodovia.group(3) else ''}, BRASIL"
            endereco_canonico = texto_cru

    def disparar_apis_paralelas(tarefas):
        resultados = []
        for f in as_completed([st.session_state["executor_apis"].submit(func, *args, **kwargs) for func, args, kwargs in tarefas]):
            if res := f.result(): resultados.extend(res)
        return resultados

    if tipo_entrada == "POI":
        candidatos_validos.extend(disparar_apis_paralelas([(API_Google_Geocoding_Scraper, (endereco_canonico,), {}), (API_Overpass_POIs, (semantica.normalizar(texto_cru),), {})]))
    elif tipo_entrada in ["ENDERECO_COMPLETO", "LOGRADOURO", "CONDOMINIO"]:
        candidatos_validos.extend(disparar_apis_paralelas([(API_ArcGIS, (endereco_canonico,), {"ctx": contexto_estruturado}), (API_Google_Geocoding_Scraper, (endereco_canonico,), {})]))
        if res_nom := API_Nominatim(endereco_canonico, ctx=contexto_estruturado): candidatos_validos.extend(res_nom)
    elif tipo_entrada in ["BAIRRO", "MUNICIPIO", "DISTRITO"]:
        candidatos_validos.extend(disparar_apis_paralelas([(API_Photon, (endereco_canonico,), {})]))
        if res_nom := API_Nominatim(endereco_canonico, ctx=contexto_estruturado): candidatos_validos.extend(res_nom)
    else:
        candidatos_validos.extend(disparar_apis_paralelas([(API_Google_Geocoding_Scraper, (endereco_canonico,), {}), (API_Photon, (endereco_canonico,), {}), (API_ArcGIS, (endereco_canonico,), {"ctx": contexto_estruturado})]))
            
    res_final = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru)
    
    if not res_final and tipo_entrada not in ["BAIRRO", "MUNICIPIO"]:
        if res_nom := API_Nominatim(endereco_canonico, ctx=contexto_estruturado):
            candidatos_validos.extend(res_nom)
            res_final = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru)

    if res_final:
        cache_geo.set(cache_key, {"lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], "confianca": res_final[3], "score_num": res_final[4], "distrito": res_final[5], "municipio": res_final[6], "fonte": res_final[7]}, expire=2592000)
        if res_final[4] >= 95 and res_final[3] == "ALTISSIMA":
            cache_aprendizado_auto.set(chave_auto, {"lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], "distrito": res_final[5], "municipio": res_final[6]}, expire=7776000)
        return res_final
        
    return 0.0, 0.0, endereco_canonico, "BAIXA", 0, "", "", "N/A"

# ==============================================================================
# 🚀 MOTOR DE ROTEAMENTO (ARBITRAGEM DE PROVEDORES E PERFIS DE DISTÂNCIA)
# ==============================================================================
def extrair_dados_reais_google(origem_raw, destino_raw, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=True):
    cache_key = f"{origem_raw}|{destino_raw}|{usar_coordenadas}"
    if cache_key in cache_google: return cache_google[cache_key]

    if not usar_coordenadas and lat_d != 0.0 and lon_d != 0.0:
        if (google_dest_geo := API_Google_Geocoding_Scraper(destino_raw)) and calcular_distancia_vincenty(lat_d, lon_d, google_dest_geo[0]["lat"], google_dest_geo[0]["lon"]) > 20.0: return None 

    origem_param = f"{lat_o},{lon_o}" if usar_coordenadas else requests.utils.quote(origem_raw)
    destino_param = f"{lat_d},{lon_d}" if usar_coordenadas else requests.utils.quote(destino_raw)
    url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(origem_raw)}&destination={requests.utils.quote(destino_raw)}&travelmode=driving"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.google.com/maps"}
    
    try:
        resposta = session.get(url_api, headers=headers, timeout=8)
        texto_resposta = resposta.text
        if len(texto_resposta) < 500 or "directions" not in texto_resposta.lower(): return None
            
        match_km = re.findall(r'\"(\d+[\.,]?\d*)\s*km\"', texto_resposta)
        match_tempo = re.findall(r'\"(\d+\s*h\s*\d+\s*min|\d+\s*h|\d+\s*min)\"', texto_resposta)
        if match_km and match_tempo:
            km_puro = float(match_km[0].replace('.', '').replace(',', '.'))
            if dist_linha_reta > 0:
                limite_curto = max(dist_linha_reta * 2.0, dist_linha_reta + 15.0)
                if dist_linha_reta <= 50.0 and km_puro > limite_curto: return None  
                elif km_puro < dist_linha_reta * 0.8 or km_puro > dist_linha_reta * 4.0: return None  

            envolve_balsa = "Sim" if any(re.search(p, texto_resposta.lower()) for p in [r'\"utilizar\s+balsa\b', r'\"ferry\b']) else "Não"
            score_google = 70 + (10 if km_puro > 0 else 0) + (10 if match_tempo[0] else 0) + (10 if km_puro >= dist_linha_reta else 0)
            res = (km_puro, match_tempo[0], link_maps, envolve_balsa, score_google)
            cache_google.set(cache_key, res, expire=2592000); return res
    except Exception: pass
    return None

def rota_osrm(lat_o, lon_o, lat_d, lon_d):
    try:
        url = f"https://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=false"
        r = session.get(url, timeout=5).json()
        if r.get("routes"):
            km = round(r["routes"][0]["distance"] / 1000, 2)
            minutos = round(r["routes"][0]["duration"] / 60)
            return km, f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min", "OSRM", 95
    except Exception: pass
    return None

def obter_fator_desvio_rodoviario(linha_reta):
    return 1.45 if linha_reta < 5.0 else 1.35 if linha_reta < 20.0 else 1.25 if linha_reta < 100.0 else 1.18

def calcular_pipeline_logistico(origem, destino, perfil_rota="shortest"):
    start_total = time.time()
    origem_clean, destino_clean = str(origem).strip(), str(destino).strip()
    
    chave_rota_cache = f"ROTA_{semantica.normalizar(origem_clean)}->{semantica.normalizar(destino_clean)}"
    if chave_rota_cache in cache_rotas: return cache_rotas[chave_rota_cache]
    
    start_geo = time.time()
    lat_o, lon_o, end_oficial_o, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    lat_d, lon_d, end_oficial_d, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d = obter_coordenadas_e_endereco_oficial(destino_clean)
    tempo_geocoding = round(time.time() - start_geo, 2)
    start_rot = time.time()

    if all([lat_o is not None, lon_o is not None, lat_d is not None, lon_d is not None]) and lat_o != 0.0 and lat_d != 0.0:
        dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)
    else: dist_linha_reta = 0.0

    def formatar_retorno(tupla_dados):
        falha_qa = auditoria_geografica(tupla_dados[0], tupla_dados[1], dist_linha_reta, lat_o, lon_o, lat_d, lon_d)
        if falha_qa:
            return ("QA_REJEITADO", "QA_REJEITADO", "N/A", "N/A", dist_linha_reta, f"QA Falhou: {falha_qa}", 0, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, 0.0, round(time.time() - start_total, 2))
        return tupla_dados

    usar_coords = True if (lat_o != 0.0 and lat_d != 0.0) else False
    if usar_coords and dist_linha_reta > 150.0:
        siglas_originais = re.findall(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM|PA|MT|MS)\b', origem_clean.upper() + " " + destino_clean.upper())
        if len(set(siglas_originais)) <= 1: usar_coords = False

    link_fallback = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(end_oficial_o)}&destination={requests.utils.quote(end_oficial_d)}&travelmode=driving"

    res_osrm = None
    if usar_coords:
        res_osrm = rota_osrm(lat_o, lon_o, lat_d, lon_d)
        if res_osrm and perfil_rota == "fastest":
            tempo_roteamento = round(time.time() - start_rot, 2); tempo_total = round(time.time() - start_total, 2)
            retorno = (res_osrm[0], res_osrm[1], link_fallback, "Não", dist_linha_reta, res_osrm[2], res_osrm[3], conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total)
            ret_formatado = formatar_retorno(retorno); cache_rotas.set(chave_rota_cache, ret_formatado, expire=2592000); return ret_formatado

    res_google = extrair_dados_reais_google(end_oficial_o, end_oficial_d, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=usar_coords)

    if perfil_rota == "shortest":
        opcoes = []
        if res_osrm: opcoes.append((res_osrm[0], res_osrm[1], link_fallback, "Não", dist_linha_reta, res_osrm[2], res_osrm[3]))
        if res_google: opcoes.append((res_google[0], res_google[1], res_google[2], res_google[3], dist_linha_reta, "Google Preview", res_google[4]))
        
        if opcoes:
            melhor_opcao = min(opcoes, key=lambda x: x[0]) 
            tempo_roteamento = round(time.time() - start_rot, 2); tempo_total = round(time.time() - start_total, 2)
            retorno = (*melhor_opcao, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total)
            ret_formatado = formatar_retorno(retorno); cache_rotas.set(chave_rota_cache, ret_formatado, expire=2592000); return ret_formatado

    if res_google:
        tempo_roteamento = round(time.time() - start_rot, 2); tempo_total = round(time.time() - start_total, 2)
        retorno = (res_google[0], res_google[1], res_google[2], res_google[3], dist_linha_reta, "Google Preview", res_google[4], conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total)
        ret_formatado = formatar_retorno(retorno); cache_rotas.set(chave_rota_cache, ret_formatado, expire=2592000); return ret_formatado

    km_terrestre = round(dist_linha_reta * obter_fator_desvio_rodoviario(dist_linha_reta), 2)
    v_comercial = 45.0 if km_terrestre < 50.0 else 65.0
    minutos_est = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0 else 0
    tempo_geo_str = f"{minutos_est} min" if minutos_est < 60 else f"{minutos_est // 60} h {minutos_est % 60} min"
    tempo_roteamento = round(time.time() - start_rot, 2); tempo_total = round(time.time() - start_total, 2)
    
    retorno = (km_terrestre, tempo_geo_str, link_fallback, "Não", dist_linha_reta, "Geodésico Adaptativo", 70, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total)
    ret_formatado = formatar_retorno(retorno); cache_rotas.set(chave_rota_cache, ret_formatado, expire=2592000); return ret_formatado

def embrulhar_task_paralela(item):
    par_id, orig, dest = item
    try: return par_id, calcular_pipeline_logistico(orig, dest, perfil_rota="shortest")
    except Exception: return par_id, None

# ==============================================================================
# 🚗 INTERFACE STREAMLIT COM ENGINE DE DEDUPLICAÇÃO ASINTÓTICA E VETORIZAÇÃO
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
            st.error(f"⚠️ Limite arquitetural de {MAX_LINHAS} linhas excedido. Fracione o arquivo.")
            st.stop()
            
        st.success(f"Tabela com {len(df)} registros mapeada! Pronto para processar.")
        
        if st.button("Iniciar Processamento em Lote"):
            novas_colunas = [
                'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta', 'Fonte da Rota', 'Score da Rota', 
                'Confianca Origem', 'Score Num Origem', 'Distrito Origem', 'Municipio Origem', 'Fonte Geocoding Origem', 'Endereco Oficial Origem',
                'Confianca Destino', 'Score Num Destino', 'Distrito Destino', 'Municipio Destino', 'Fonte Geocoding Destino', 'Endereco Oficial Destino',
                'Lat Origem', 'Lon Origem', 'Lat Destino', 'Lon Destino', 'Tempo Geocoding (s)', 'Tempo Roteamento (s)', 'Tempo Total (s)', 'Score Final Global', 'Status da Rota'
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
            
            if not pares_unicos:
                st.warning("Nenhuma linha contendo endereços válidos detectada.")
                st.stop()
                
            st.info(f"Otimização O(U) Ativa: Detectadas {len(pares_unicos)} rotas únicas em {len(mapeamento_linhas)} linhas válidas.")
                
            resultados_unicos = {}
            executor_lote = st.session_state["executor_global"]
            tarefas_unicas = [(par, par[0], par[1]) for par in pares_unicos]
            futuros = {executor_lote.submit(embrulhar_task_paralela, t): t for t in tarefas_unicas}
            
            concluidos = 0
            barra_progresso = st.progress(0)
            container_status = st.empty()
            
            for f in as_completed(futuros):
                par_id, res = f.result()
                resultados_unicos[par_id] = res
                    
                concluidos += 1
                container_status.text(f"🚀 Roteamento Assíncrono (Rotas Únicas): {concluidos} / {len(pares_unicos)}")
                barra_progresso.progress(concluidos / len(pares_unicos))
                
            container_status.text("✨ Distribuindo resultados na matriz principal...")
            
            registros_atualizados = []
            for idx, origem, destino in mapeamento_linhas:
                par = (origem, destino)
                res = resultados_unicos.get(par)
                
                if res:
                    if res[0] == "QA_REJEITADO" or "QA Falhou" in str(res[5]):
                        status_rota = "Erro Crítico / QA Falhou"
                        score_global = 0.0
                    else:
                        score_o, score_d, score_r = res[8], res[14], res[6]
                        score_global = round((0.35 * score_o) + (0.35 * score_d) + (0.30 * score_r), 2)
                        status_rota = "Excelente" if score_global >= 90 else "Boa" if score_global >= 80 else "Aceitável" if score_global >= 70 else "Revisar"
                    
                    registros_atualizados.append({
                        'index': idx,
                        'Distancia': res[0], 'Tempo': res[1], 'Link da Rota': res[2], 'Balsas': res[3],
                        'Linha Reta': res[4], 'Fonte da Rota': res[5], 'Score da Rota': res[6],
                        'Confianca Origem': res[7], 'Score Num Origem': res[8], 'Distrito Origem': res[9],
                        'Municipio Origem': res[10], 'Fonte Geocoding Origem': res[11], 'Endereco Oficial Origem': res[12],
                        'Confianca Destino': res[13], 'Score Num Destino': res[14], 'Distrito Destino': res[15],
                        'Municipio Destino': res[16], 'Fonte Geocoding Destino': res[17], 'Endereco Oficial Destino': res[18],
                        'Lat Origem': res[19], 'Lon Origem': res[20], 'Lat Destino': res[21], 'Lon Destino': res[22],
                        'Tempo Geocoding (s)': res[23], 'Tempo Roteamento (s)': res[24], 'Tempo Total (s)': res[25],
                        'Score Final Global': score_global, 'Status da Rota': status_rota
                    })
                else:
                    registros_atualizados.append({'index': idx, 'Status da Rota': "Erro de Processamento"})

            if registros_atualizados:
                df_temp = pd.DataFrame(registros_atualizados).set_index('index')
                for col in df_temp.columns:
                    df.loc[df_temp.index, col] = df_temp[col]

            container_status.empty(); barra_progresso.empty()
            st.success("✨ Processamento em lote corporativo concluído!")
            
            ordem_finais = ['Origem', 'Destino'] + novas_colunas
            df = df.reindex(columns=ordem_finais)
            
            output_buffer = io.BytesIO()
            with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer: df.to_excel(writer, index=False)
            st.session_state['planilha_pronta'] = output_buffer.getvalue()

    if 'planilha_pronta' in st.session_state:
        st.write("---"); st.balloons()
        st.download_button(label="📥 Baixar Planilha Logística Processada", data=st.session_state['planilha_pronta'], file_name="planilha_rotas_calculada.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
