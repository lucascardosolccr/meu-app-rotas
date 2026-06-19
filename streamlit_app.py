import streamlit as stimport pandas as pdimport requestsimport timeimport mathimport ioimport reimport osimport pickleimport collectionsfrom unidecode import unidecodefrom rapidfuzz import process, fuzzfrom diskcache import Cachefrom concurrent.futures import ThreadPoolExecutor, as_completedfrom requests.adapters import HTTPAdapterfrom urllib3.util.retry import Retry

==============================================================================

CONFIGURAÇÃO DE UI/UX E AMBIENTE

==============================================================================

st.set_page_config(page_title="Gerenciador de Rotas Inteligentes", page_icon="🚗", layout="centered")

==============================================================================

 PERSISTÊNCIA EM DISCO E HIGIENIZAÇÃO DE AMBIENTE (GARBAGE COLLECTION)

==============================================================================

cache_classificacao = Cache("./cache_classificacao")cache_fuzzy = Cache("./cache_fuzzy")cache_geo = Cache("./cache_geo")cache_rotas = Cache("./cache_rotas")cache_poi = Cache("./cache_poi")cache_cep = Cache("./cache_cep")cache_google = Cache("./cache_google")cache_reverse = Cache("./cache_reverse")cache_base_local = Cache("./cache_base_local")cache_aprendizado = Cache("./cache_aprendizado")

for c in [cache_classificacao, cache_fuzzy, cache_geo, cache_rotas, cache_poi, cache_cep, cache_google, cache_reverse, cache_base_local, cache_aprendizado]:c.cull()

def realizar_manutencao_logs_google():diretorio_logs = "logs_google"os.makedirs(diretorio_logs, exist_ok=True)limite_tempo = time.time() - (30 * 86400)try:for arquivo in os.listdir(diretorio_logs):caminho_completo = os.path.join(diretorio_logs, arquivo)if os.path.isfile(caminho_completo) and os.path.getmtime(caminho_completo) < limite_tempo:os.remove(caminho_completo)except Exception: pass

realizar_manutencao_logs_google()

session = requests.Session()retry_strategy = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])adapter = HTTPAdapter(max_retries=retry_strategy)session.mount("https://", adapter)session.mount("http://", adapter)

CACHE_IBGE_PATH = "municipios_ibge.pkl"

==============================================================================

🎛️ INFRAESTRUTURA DE CONCORRÊNCIA E FILAS (FIM DO EFEITO COMBOIO)

==============================================================================

WORKERS_DISPONIVEIS = 8

if "executor_global" not in st.session_state:st.session_state["executor_global"] = ThreadPoolExecutor(max_workers=WORKERS_DISPONIVEIS)

if "fila_nominatim" not in st.session_state:st.session_state["fila_nominatim"] = ThreadPoolExecutor(max_workers=1)

O contexto_regional_window (estado global) foi removido para garantir idempotência.

==============================================================================

🎛️ DADOS GLOBAIS THREAD-SAFE (RESOLUÇÃO DE HOMÔNIMOS MATRICIAL)

==============================================================================

@st.cache_datadef carregar_dados_ibge():if os.path.exists(CACHE_IBGE_PATH):if time.time() - os.path.getmtime(CACHE_IBGE_PATH) > (30 * 86400):os.remove(CACHE_IBGE_PATH)else:try:with open(CACHE_IBGE_PATH, "rb") as f:d = pickle.load(f)return d.get("municipios", {}), d.get("estados", {}), d.get("distritos", {}), list(d.get("municipios", {}).keys()) + list(d.get("distritos", {}).keys())except Exception: pass

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
except Exception: pass

lista_completa = list(base_mun.keys()) + list(base_dist.keys())
return base_mun, base_est, base_dist, lista_completa

IBGE_MUNICIPIOS, IBGE_ESTADOS, IBGE_DISTRITOS, LISTA_TOPONIMOS = carregar_dados_ibge()

LISTA_CONTEXTO_FUZZY = []for k, v_list in IBGE_MUNICIPIOS.items():for v in v_list: LISTA_CONTEXTO_FUZZY.append(f"{k} {v['uf']}")for k, v_list in IBGE_DISTRITOS.items():for v in v_list: LISTA_CONTEXTO_FUZZY.append(f"{k} {v['uf']}")LISTA_CONTEXTO_FUZZY = list(set(LISTA_CONTEXTO_FUZZY))

SINONIMOS_SEMANTICOS = {"UNB": "UNIVERSIDADE DE BRASILIA", "CATOLICA": "UNIVERSIDADE CATOLICA","JK": "JUSCELINO KUBITSCHEK", "HBDF": "HOSPITAL DE BASE DO DISTRITO FEDERAL","HRAN": "HOSPITAL REGIONAL DA ASA NORTE", "RODOVIARIA": "TERMINAL RODOVIARIO"}

POI_KEYWORDS = ["AEROPORTO", "HOSPITAL", "UNIVERSIDADE", "FACULDADE", "ESCOLA", "SHOPPING","HOTEL", "RODOVIARIA", "ESTADIO", "MINISTERIO", "AGENCIA", "BANCO","IGREJA", "FORUM", "TRIBUNAL", "DELEGACIA", "PREFEITURA", "CLINICA"]

BOUNDING_BOXES_UF = {"DF": {"lat_min": -16.05, "lat_max": -15.50, "lon_min": -48.30, "lon_max": -47.30},"SP": {"lat_min": -25.50, "lat_max": -19.50, "lon_min": -53.50, "lon_max": -44.00},"GO": {"lat_min": -19.50, "lat_max": -12.40, "lon_min": -53.30, "lon_max": -45.90},# Expanda este dicionário para outras UFs conforme sua demanda operacional}

==============================================================================

🧹 ENGINE DE RESOLUÇÃO UNIVERSAL E ENDEREÇAMENTO CANÔNICO

==============================================================================

class ParserGeograficoBR:@staticmethoddef extrair_componentes(texto):componentes = {"cep": "", "numero": "", "complemento": "", "resto": texto}cep_match = re.search(r'\b\d{5}-?\d{3}\b', componentes["resto"])if cep_match:componentes["cep"] = cep_match.group(0).replace("-", "")componentes["resto"] = componentes["resto"].replace(cep_match.group(0), "").strip(" ,-")

    num_match = re.search(r'\b(?:N|NO|NUMERO|NUM)?\s*(\d{1,5})\b', componentes["resto"], re.IGNORECASE)
    if num_match: componentes["numero"] = num_match.group(1)
        
    comp_match = re.search(r'\b(BLOCO|BL|APTO|APT|APARTAMENTO|SALASL|SALA|CONJUNTO|CJ|CASA|LOJA|PAVIMENTO)\s*([A-Z0-9]+)\b', componentes["resto"], re.IGNORECASE)
    if comp_match: componentes["complemento"] = f"{comp_match.group(1)} {comp_match.group(2)}"
        
    return componentes

class MotorEnderecoCanônico:def init(self):self.rural_keys = ["FAZENDA", "SITIO", "ASSENTAMENTO", "CHACARA", "GLEBA", "NUCLEO RURAL"]self.bairro_keys = ["BAIRRO", "VILA", "JARDIM", "PARQUE", "RESIDENCIAL", "SETOR", "ASA SUL", "ASA NORTE", "LAGO SUL", "LAGO NORTE"]

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
    
    # Self-Healing Layer protegido (Ignora dicionários de coordenadas)
    chave_aprendizado = t_raw.upper()
    if chave_aprendizado in cache_aprendizado:
        dado_salvo = cache_aprendizado[chave_aprendizado]
        if isinstance(dado_salvo, str): 
            t_raw = dado_salvo

    t = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', t_raw)
    t = unidecode(t).upper()
    t = re.sub(r'\b0+(\d{1,4})\b', r'\1', t) 
    
    def padronizar_rodovia(match):
        sigla, numero = match.group(1), match.group(2).zfill(3)
        return f"{sigla}-{numero}"
        
    padrao_rodovia = r'\b(BR|AC|AL|AP|AM|BA|CE|DF|ES|GO|MA|MT|MS|MG|PA|PB|PR|PE|PI|RJ|RN|RS|RO|RR|SC|SP|SE|TO)\s*[-]?\s*(\d{1,3})\b'
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
            if chave in texto_norm:
                return {"uf": "DF", "municipio": "BRASILIA", "distrito": ra_oficial}
            
    for i in range(len(tokens)):
        for j in range(i + 1, len(tokens) + 1):
            chunk = " ".join(tokens[i:j])
            
            if chunk in IBGE_MUNICIPIOS:
                if uf_explicita:
                    for item in IBGE_MUNICIPIOS[chunk]:
                        if item["uf"] == uf_explicita:
                            return {"uf": uf_explicita, "municipio": chunk, "distrito": ""}
                else:
                    return {"uf": IBGE_MUNICIPIOS[chunk][0]["uf"], "municipio": chunk, "distrito": ""}
                    
            if chunk in IBGE_DISTRITOS:
                if uf_explicita:
                    for item in IBGE_DISTRITOS[chunk]:
                        if item["uf"] == uf_explicita:
                            return {"uf": uf_explicita, "municipio": item["municipio"], "distrito": chunk}
                else:
                    return {"uf": IBGE_DISTRITOS[chunk][0]["uf"], "municipio": IBGE_DISTRITOS[chunk][0]["municipio"], "distrito": chunk}
                
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

==============================================================================

🧮 LÓGICA GEODÉSICA E LIMITES ESPACIAIS DO BRASIL

==============================================================================

def validar_coordenada_brasil(lat, lon):try:lat_f, lon_f = float(lat), float(lon)if (-35.0 <= lat_f <= 6.0) and (-75.0 <= lon_f <= -28.0):return True, lat_f, lon_fif (-35.0 <= lon_f <= 6.0) and (-75.0 <= lat_f <= -28.0):return True, lon_f, lat_freturn False, lat_f, lon_fexcept (ValueError, TypeError):return False, 0.0, 0.0

def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):if not (-90 <= lat1 <= 90) or not (-90 <= lat2 <= 90) or not (-180 <= lon1 <= 180) or not (-180 <= lon2 <= 180): return 0.0if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0: return 0.0if lat1 == lat2 and lon1 == lon2: return 0.0try:a, b, f = 6378137.0, 6356752.314245, 1 / 298.257223563L = math.radians(lon2 - lon1)U1, U2 = math.atan((1 - f) * math.tan(math.radians(lat1))), math.atan((1 - f) * math.tan(math.radians(lat2)))sinU1, cosU1 = math.sin(U1), math.cos(U1)sinU2, cosU2 = math.sin(U2), math.cos(U2)lam = Lfor _ in range(100):sinLam, cosLam = math.sin(lam), math.cos(lam)sinSigma = math.sqrt((cosU2 * sinLam) ** 2 + (cosU1 * sinU2 - sinU1 * cosU2 * cosLam) ** 2)if sinSigma == 0: return 0.0cosSigma = sinU1 * sinU2 + cosU1 * cosU2 * cosLamsigma = math.atan2(sinSigma, cosSigma)sinAlpha = cosU1 * cosU2 * sinLam / sinSigmacosSqAlpha = 1 - sinAlpha ** 2cos2SigmaM = cosSigma - 2 * sinU1 * sinU2 / cosSqAlpha if cosSqAlpha != 0 else 0C = f / 16 * cosSqAlpha * (4 + f * (4 - 3 * cosSqAlpha))lambdaPrev = lamlam = L + (1 - f) * C * sinAlpha * (sigma + f * sinAlpha * (cos2SigmaM + C * cosSigma * (-1 + 2 * cos2SigmaM ** 2)))if abs(lam - lambdaPrev) < 1e-12: breakuSq = cosSqAlpha * (a ** 2 - b ** 2) / (b ** 2)A = 1 + uSq / 16384 * (4096 + uSq * (-768 + uSq * (320 - 175 * uSq)))B = uSq / 1024 * (256 + uSq * (-128 + uSq * (74 - 47 * uSq)))deltaSigma = B * sinSigma * (cos2SigmaM + B / 4 * (cosSigma * (-1 + 2 * cos2SigmaM ** 2) - B / 6 * cos2SigmaM * (-3 + 4 * sinSigma ** 2) * (-3 + 4 * cos2SigmaM ** 2)))s = b * A * (sigma - deltaSigma)return round(s / 1000, 2)except Exception:dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)m_a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2return round(6371.0 * 2 * math.atan2(math.sqrt(m_a), math.sqrt(1 - m_a)), 2)

def cascata_postal_tripla(cep_limpo):if cep_limpo in cache_cep:d = cache_cep[cep_limpo]if len(d) == 4: return d[0], d[1], d[2], d[3], 0.0, 0.0return dlat, lon = 0.0, 0.0try:r = session.get(f"https://brasilapi.com.br/api/cep/v2/{cep_limpo}", timeout=4).json()if "city" in r:loc = r.get("location", {}).get("coordinates", {})if loc and "latitude" in loc and "longitude" in loc:try: lat, lon = float(loc["latitude"]), float(loc["longitude"])except (ValueError, TypeError): passd = (r.get('street', ''), r.get('neighborhood', ''), r.get('city', ''), r.get('state', ''), lat, lon)cache_cep.set(cep_limpo, d, expire=2592000); return dexcept Exception: passtry:def _nom_cep():time.sleep(1.1)url = f"https://nominatim.openstreetmap.org/search?format=json&postalcode={cep_limpo}&countrycodes=br&limit=1"return session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()r_nom = st.session_state["fila_nominatim"].submit(_nom_cep).result()if r_nom: lat, lon = float(r_nom[0]['lat']), float(r_nom[0]['lon'])except Exception: passtry:r = session.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=4).json()if "erro" not in r:d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon)cache_cep.set(cep_limpo, d, expire=2592000); return dexcept Exception: passtry:r = session.get(f"https://opencep.com/v1/{cep_limpo}", timeout=4).json()if "error" not in r:d = (r.get('logradouro', ''), r.get('bairro', ''), r.get('localidade', ''), r.get('uf', ''), lat, lon)cache_cep.set(cep_limpo, d, expire=2592000); return dexcept Exception: passreturn "", "", "", "", 0.0, 0.0

def validar_consistencia_administrativa(candidato, uf_inf):est_api = unidecode(candidato.get('estado', '')).upper().strip()if uf_inf and est_api:if uf_inf != est_api:return Falsereturn True

def validar_consistencia_municipal(candidato, mun_inf):if not mun_inf: return Truecid_api = unidecode(candidato.get('cidade', '')).upper().strip()if not cid_api: return Falseif mun_inf == cid_api or mun_inf in cid_api or cid_api in mun_inf: return Trueif fuzz.token_set_ratio(mun_inf, cid_api) >= 95: return Truereturn False

==============================================================================

🗺️ MÓDULOS DE GEOCODIFICAÇÃO (CONTRATO LISTA TOP-K)

==============================================================================

def API_Google_Geocoding_Scraper(query):try:url = f"https://www.google.com/maps/search/{requests.utils.quote(query)}"headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}r = session.get(url, headers=headers, timeout=5, allow_redirects=True)match = re.search(r'@(-?\d+.\d+),(-?\d+.\d+)', r.url)if not match: match = re.search(r'@(-?\d+.\d+),(-?\d+.\d+)', r.text)if match: return [{"lat": float(match.group(1)), "lon": float(match.group(2)), "fonte": "GOOGLE_MAPS", "score_base": 40, "cidade": "", "estado": "", "bairro": ""}]except Exception: passreturn None

def executar_reverse_geocoding_multimotor(lat, lon):rev_key = f"{round(lat,5)}|{round(lon,5)}"if rev_key in cache_reverse: return cache_reverse[rev_key]res = {"logradouro": "", "bairro": "", "cidade": "", "municipio": "", "distrito": "", "estado": "", "cep": ""}try:def _nom_rev():time.sleep(1.1)url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&addressdetails=1"return session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()r_nom = st.session_state["fila_nominatim"].submit(_nom_rev).result()a = r_nom.get("address", {})res.update({"logradouro": a.get("road", a.get("pedestrian", "")), "bairro": a.get("neighbourhood", a.get("suburb", a.get("city_district", ""))), "cidade": a.get("city", a.get("town", a.get("municipality", ""))), "estado": a.get("state", "").upper(), "cep": a.get("postcode", "")})cache_reverse.set(rev_key, res, expire=2592000); return resexcept Exception: passtry:url_arc = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/reverseGeocode?location={lon},{lat}&f=json"r_arc = session.get(url_arc, timeout=4).json()if 'address' in r_arc:addr = r_arc['address']res.update({"logradouro": addr.get('Address', ''), "bairro": addr.get('Neighborhood', ''), "cidade": addr.get('City', ''), "estado": addr.get('RegionAbbr', '').upper(), "cep": addr.get('Postal', '')})cache_reverse.set(rev_key, res, expire=2592000)except Exception: passreturn res

def API_ArcGIS(query, ctx=None):try:if ctx and (ctx.get("logradouro") or ctx.get("municipio")):end = requests.utils.quote(ctx.get("logradouro", ""))cid = requests.utils.quote(ctx.get("municipio", ""))uf = requests.utils.quote(ctx.get("uf", ""))bair = requests.utils.quote(ctx.get("bairro", ""))cep = requests.utils.quote(ctx.get("cep", ""))url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&Address={end}&Neighborhood={bair}&City={cid}&Region={uf}&Postal={cep}&maxLocations=5&sourceCountry=BRA&outFields="else:url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=5&sourceCountry=BRA&outFields="

    r = session.get(url, timeout=4).json()
    resultados = []
    if r.get('candidates'):
        for c in r['candidates'][:5]:
            attr = c.get('attributes', {})
            resultados.append({"lat": float(c['location']['y']), "lon": float(c['location']['x']), "fonte": "ARCGIS", "score_base": 30, "cidade": attr.get('City', '').upper(), "estado": attr.get('RegionAbbr', '').upper(), "bairro": attr.get('Neighborhood', '').upper(), "logradouro": attr.get('StName', attr.get('Address', '')).upper(), "numero": str(attr.get('AddNum', '')).upper(), "cep": attr.get('Postal', '')})
    return resultados if resultados else None
except Exception: pass
return None

def API_Nominatim(query, ctx=None):try:def _call_nom():time.sleep(1.1)if ctx and ctx.get("logradouro") and ctx.get("municipio"):rua = requests.utils.quote(ctx["logradouro"])cid = requests.utils.quote(ctx["municipio"])est = requests.utils.quote(ctx.get("uf", ""))url = f"https://nominatim.openstreetmap.org/search?format=json&street={rua}&city={cid}&state={est}&limit=5&addressdetails=1&countrycodes=br"else:url = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query)}&limit=5&addressdetails=1&countrycodes=br"return session.get(url, headers={"User-Agent": "RotasEnterprise/8.0"}, timeout=4).json()

    r = st.session_state["fila_nominatim"].submit(_call_nom).result()
    resultados = []
    if r:
        for a in r[:5]:
            addr = a.get("address", {})
            resultados.append({"lat": float(a['lat']), "lon": float(a['lon']), "fonte": "NOMINATIM", "score_base": 25, "cidade": addr.get('city', addr.get('town', '')).upper(), "estado": addr.get('state', '').upper(), "bairro": addr.get('neighbourhood', addr.get('suburb', '')).upper(), "logradouro": addr.get('road', '').upper(), "numero": str(addr.get('house_number', '')).upper(), "cep": addr.get('postcode', '').replace("-", "")})
    return resultados if resultados else None
except Exception: pass
return None

def API_Photon(query):try:url = f"https://photon.komoot.io/api/?q={requests.utils.quote(query)}&limit=5&filter=countrycode:br"r = session.get(url, timeout=4).json()resultados = []if r.get("features"):for f in r["features"][:5]:lon, lat = f["geometry"]["coordinates"]props = f.get("properties", {})resultados.append({"lat": lat, "lon": lon, "fonte": "PHOTON", "score_base": 20, "cidade": props.get("city", "").upper(), "estado": props.get("state", "").upper(), "bairro": props.get("district", "").upper(), "logradouro": props.get("street", "").upper(), "numero": str(props.get("housenumber", "")).upper(), "cep": props.get("postcode", "").replace("-", "")})return resultados if resultados else Noneexcept Exception: passreturn None

def API_Overpass_POIs(texto_norm):if len(texto_norm) < 10: return Noneif texto_norm in cache_poi: return cache_poi[texto_norm]endpoints = ["https://overpass-api.de/api/interpreter", "https://lz4.overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]texto_seguro = re.escape(texto_norm)query_osm = f'[out][timeout:3];(node["name"~"{texto_seguro}",i]["amenity"];way["name"~"{texto_seguro}",i]["amenity"];node["name"~"{texto_seguro}",i]["building"];way["name"~"{texto_seguro}",i]["building"];node["name"~"{texto_seguro}",i]["healthcare"];way["name"~"{texto_seguro}",i]["healthcare"];node["name"~"{texto_seguro}",i]["education"];way["name"~"{texto_seguro}",i]["education"];);out center;'for url in endpoints:try:r = session.post(url, data={"data": query_osm}, timeout=4)if r.status_code == 200:elems = r.json().get("elements", [])if elems:e = elems[0]lat, lon = e.get("lat", e.get("center", {}).get("lat", 0.0)), e.get("lon", e.get("center", {}).get("lon", 0.0))tags = e.get("tags", {})res_poi = {"lat": lat, "lon": lon, "fonte": "OVERPASS", "score_base": 40, "cidade": tags.get("addr", "").upper(), "estado": tags.get("addr", "").upper(), "bairro": tags.get("addr", "").upper(), "logradouro": tags.get("addr", "").upper(), "numero": str(tags.get("addr", "")).upper(), "cep": tags.get("addr", "").replace("-", "")}cache_poi.set(texto_norm, [res_poi], expire=7776000)return [res_poi]except Exception: continuereturn None

==============================================================================

🧠 MOTOR DE CONSENSO STATELESS MULTIDIMENSIONAL (HYBRID CLUSTERING & SCORES)

==============================================================================

def processar_consenso_dinamico(candidatos, tipo_entrada, texto_cru):candidatos_validos = []

ctx_inf = semantica.resolver_contexto_administrativo(texto_cru.upper())
uf_inf = ctx_inf.get("uf", "")
mun_inf = ctx_inf.get("municipio", "")
dist_inf = ctx_inf.get("distrito", "")

box = BOUNDING_BOXES_UF.get(uf_inf) if uf_inf else None

# Filtro 1: Bounding Box Nacional e Estadual Estrita
for c in candidatos:
    valido, lat_c, lon_c = validar_coordenada_brasil(c["lat"], c["lon"])
    if valido:
        if box:
            if not (box["lat_min"] <= lat_c <= box["lat_max"] and box["lon_min"] <= lon_c <= box["lon_max"]):
                continue
        c["lat"], c["lon"] = lat_c, lon_c 
        candidatos_validos.append(c)
        
if not candidatos_validos: return None

# Filtro 2: Validação Semântica Cruzada IBGE Matricial
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

# Filtro 3: Clustering Híbrido Dinâmico
if tipo_entrada in ["ENDERECO_COMPLETO", "POI", "CEP"]: raio_cluster_km = 0.5
elif tipo_entrada in ["BAIRRO", "RURAL"]: raio_cluster_km = 2.0
else: raio_cluster_km = 10.0
    
clusters = []
for c in candidatos_validos:
    alocado = False
    for cluster in clusters:
        semantica_match = (
            (unidecode(c.get('cidade', '')).upper() == unidecode(cluster[0].get('cidade', '')).upper()) and
            (fuzz.token_set_ratio(c.get('bairro', ''), cluster[0].get('bairro', '')) > 90)
        )
        dist = calcular_distancia_vincenty(c["lat"], c["lon"], cluster[0]["lat"], cluster[0]["lon"])
        if semantica_match and dist <= raio_cluster_km:
            cluster.append(c)
            alocado = True
            break
    if not alocado: clusters.append([c])
        
if clusters:
    tamanho_maior_cluster = max(len(cluster) for cluster in clusters)
    if tamanho_maior_cluster > 1:
        candidatos_validos = [c for cluster in clusters if len(cluster) == tamanho_maior_cluster for c in cluster]
if not candidatos_validos: return None

tolerancia_km = raio_cluster_km
input_usuario = ParserGeograficoBR.extrair_componentes(texto_cru.upper())

# Filtro 4: Validação Administrativa Forte (Hard Drop de Estado e Município)
candidatos_consistentes_uf = [c for c in candidatos_validos if validar_consistencia_administrativa(c, uf_inf)]
if candidatos_consistentes_uf: candidatos_validos = candidatos_consistentes_uf

candidatos_consistentes_mun = [c for c in candidatos_validos if validar_consistencia_municipal(c, mun_inf)]
if candidatos_consistentes_mun: candidatos_validos = candidatos_consistentes_mun
    
for c1 in candidatos_validos:
    score_centesimal = c1["score_base"]
    
    if mun_inf and c1.get("cidade") and (mun_inf in c1["cidade"] or fuzz.token_set_ratio(mun_inf, c1["cidade"]) >= 95): score_centesimal += 50
    if uf_inf and c1.get("estado") and uf_inf in c1["estado"]: score_centesimal += 20
    if input_usuario.get("cep") and c1.get("cep") and input_usuario["cep"] in c1["cep"].replace("-", ""): score_centesimal += 20
    if c1.get("logradouro") and fuzz.token_set_ratio(texto_cru.upper(), c1["logradouro"]) > 80: score_centesimal += 10
    if dist_inf and c1.get("bairro") and dist_inf in c1["bairro"]: score_centesimal += 15
    if input_usuario.get("numero") and c1.get("numero") and input_usuario["numero"] in c1["numero"]: score_centesimal += 25
    
    # Filtro de Rodovias
    input_tem_rodovia = bool(re.search(r'\b(BR|RODOVIA|KM|ESTRADA)\b', texto_cru.upper()))
    api_tem_rodovia = bool(re.search(r'\b(BR|RODOVIA|KM|ESTRADA)\b', c1.get("logradouro", "").upper()))
    if not input_tem_rodovia and api_tem_rodovia: score_centesimal -= 60
    
    api_end_str = f"{c1.get('logradouro','')} {c1.get('bairro','')} {c1.get('cidade','')} {c1.get('estado','')}".upper()
    if tipo_entrada == "RURAL" and any(urb in api_end_str for urb in ["QUADRA ", "SQN ", "SQS ", "APARTAMENTO ", "EDIFICIO ", "BLOCO "]): score_centesimal -= 60
    if tipo_entrada in ["ENDERECO_COMPLETO", "BAIRRO"] and any(rur in api_end_str for rur in ["CHACARA ", "FAZENDA ", "GLEBA "]): score_centesimal -= 40
        
    consenso_espacial = 0
    for c2 in candidatos_validos:
        if c1["fonte"] != c2["fonte"]:
            dist = calcular_distancia_vincenty(c1["lat"], c1["lon"], c2["lat"], c2["lon"])
            if dist <= tolerancia_km: consenso_espacial += 1; score_centesimal += 15 
            if c1.get("cidade") and c1.get("cidade") == c2.get("cidade"): score_centesimal += 10
            if c1.get("estado") and c1.get("estado") == c2.get("estado"): score_centesimal += 5
            if c1.get("bairro") and c1.get("bairro") == c2.get("bairro"): score_centesimal += 10
            
    c1["score_final"] = score_centesimal + (consenso_espacial * 20)
    
candidatos_validos.sort(key=lambda x: x["score_final"], reverse=True)

# Filtro 5: Validação Reversa Obrigatória (Closed-Loop)
vencedor = None
for cand in candidatos_validos:
    m = executar_reverse_geocoding_multimotor(cand["lat"], cand["lon"])
    estado_reverse = m.get("estado", "").upper().strip()
    cidade_reverse = m.get("cidade", "").upper().strip()
    
    if uf_inf and estado_reverse:
        if uf_inf != estado_reverse: continue 
        
    if mun_inf and cidade_reverse:
        match_cid = (mun_inf in cidade_reverse) or (cidade_reverse in mun_inf) or (fuzz.token_set_ratio(mun_inf, cidade_reverse) >= 85)
        if not match_cid: continue
    
    end_reverse = ", ".join([c for c in [m.get("logradouro", ""), m.get("bairro", ""), m.get("cidade", ""), estado_reverse] if c.strip()])
    similaridade = fuzz.token_set_ratio(texto_cru.upper(), end_reverse.upper())
    if similaridade >= 70:
        vencedor = cand
        break
        
if not vencedor: return None
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
elif tipo_entrada == "RURAL": score_completude = 75
elif tipo_entrada == "BAIRRO": score_completude = 60

score_limitado = min(score_consenso, score_completude)
if m.get("cep") and score_limitado < 100: score_limitado = min(score_limitado + 10, 100 if tipo_entrada == "CEP" else 95)

if tipo_entrada in ["ENDERECO_COMPLETO", "CEP"] and not vencedor.get("logradouro"): confianca = "MUNICIPAL"
else: confianca = "ALTISSIMA" if score_limitado >= 85 else "ALTA" if score_limitado >= 75 else "MEDIA" if score_limitado >= 60 else "BAIXA"

rua_f = m["logradouro"] if m["logradouro"] else texto_cru.upper()
endereco_f = ", ".join([c for c in [rua_f, m["bairro"], m["cidade"], m["estado"]] if c.strip()]) + ", BRASIL"
return vencedor["lat"], vencedor["lon"], endereco_f, confianca, score_limitado, m["distrito"], m["municipio"], vencedor["fonte"]

==============================================================================

🎚️ ORQUESTRADOR EM CASCATA HIERÁRQUICA E OFFLINE-FIRST

==============================================================================

def obter_coordenadas_e_endereco_oficial(localidade):texto_cru = str(localidade).strip()if not texto_cru or texto_cru.lower() == 'nan': return 0.0, 0.0, "", "BAIXA", 0, "", "", "N/A"

# Aprendizado Local Espacial O(1)
chave_aprendizado_coord = texto_cru.upper()
if chave_aprendizado_coord in cache_aprendizado:
    dado_salvo = cache_aprendizado[chave_aprendizado_coord]
    if isinstance(dado_salvo, dict) and "lat" in dado_salvo and "lon" in dado_salvo:
        return dado_salvo["lat"], dado_salvo["lon"], dado_salvo.get("endereco", texto_cru.upper()), "ALTISSIMA", 100, dado_salvo.get("distrito", ""), dado_salvo.get("municipio", ""), "APRENDIZADO_LOCAL"

endereco_canonico, tipo_entrada, _, _, _ = semantica.construir_endereco_canonico(texto_cru)
ctx = semantica.resolver_contexto_administrativo(texto_cru.upper())
parsed_comp = ParserGeograficoBR.extrair_componentes(texto_cru.upper())

cache_key = f"{tipo_entrada}_{endereco_canonico}"
if cache_key in cache_geo:
    c = cache_geo[cache_key]
    return c["lat"], c["lon"], c["endereco"], c["confianca"], c["score_num"], c["distrito"], c["municipio"], c["fonte"]

rua_suja = parsed_comp["resto"]
for loc in [ctx.get("municipio", ""), ctx.get("distrito", ""), ctx.get("uf", ""), "BRASIL", "DF"]:
    if loc: rua_suja = re.sub(rf'\b{loc}\b', '', rua_suja).strip(" ,-")
    
rua_limpa = re.sub(r'\s+', ' ', rua_suja).strip()
if parsed_comp["numero"]: rua_limpa = f"{rua_limpa} {parsed_comp['numero']}".strip()

contexto_estruturado = {
    "logradouro": rua_limpa if rua_limpa else texto_cru.upper(),
    "bairro": ctx.get("distrito", ""),
    "municipio": ctx.get("municipio", ""),
    "uf": ctx.get("uf", ""),
    "cep": parsed_comp.get("cep", "")
}

# Interceptação Base Nacional Offline
if contexto_estruturado["logradouro"] and contexto_estruturado["municipio"] and contexto_estruturado["uf"]:
    chave_cnefe = f"{contexto_estruturado['logradouro']}_{contexto_estruturado['municipio']}_{contexto_estruturado['uf']}"
    if chave_cnefe in cache_base_local:
        b = cache_base_local[chave_cnefe]
        return b["lat"], b["lon"], b["endereco"], "ALTISSIMA", 100, b.get("distrito", ""), b.get("municipio", ""), "BASE_NACIONAL_OFFLINE"

if not ctx.get("municipio") and tipo_entrada not in ["POI", "CEP"]:
    return 0.0, 0.0, endereco_canonico, "BAIXA", 0, "", "", "N/A"

candidatos_validos = []

# Nível 1: CEP (Short-circuit)
if tipo_entrada == "CEP":
    cep_estrito = re.search(r'\b\d{5}-?\d{3}\b', texto_cru)
    if cep_estrito:
        cep_limpo = cep_estrito.group(0).replace("-", "")
        logr, bair, loca, uf, lat_c, lon_c = cascata_postal_tripla(cep_limpo)
        if loca:
            nome_est_cep = IBGE_ESTADOS.get(uf, uf) if uf else ""
            addr_c = f"{logr}, {bair}, {loca}, {nome_est_cep}, CEP {cep_estrito.group(0)}, BRASIL"
            addr_c = re.sub(r',\s*,', ',', addr_c).strip(' ,')
            
            val_c, lat_corrigida_c, lon_corrigida_c = validar_coordenada_brasil(lat_c, lon_c)
            if lat_c != 0.0 and lon_c != 0.0 and val_c:
                res_final = (lat_corrigida_c, lon_corrigida_c, addr_c, "ALTISSIMA", 100, bair, loca, "BrasilAPI/OSM Postal")
                cache_geo.set(cache_key, {"lat": lat_corrigida_c, "lon": lon_corrigida_c, "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca, "fonte": "BrasilAPI/OSM Postal"}, expire=2592000)
                return res_final
            
            res_arc = API_ArcGIS(addr_c)
            if res_arc:
                if isinstance(res_arc, list): res_arc = res_arc[0]
                val_arc, lat_corrigida_arc, lon_corrigida_arc = validar_coordenada_brasil(res_arc["lat"], res_arc["lon"])
                if val_arc:
                    res_final = (lat_corrigida_arc, lon_corrigida_arc, addr_c, "ALTISSIMA", 100, bair, loca, "ViaCEP/ArcGIS")
                    cache_geo.set(cache_key, {"lat": lat_corrigida_arc, "lon": lon_corrigida_arc, "endereco": addr_c, "confianca": "ALTISSIMA", "score_num": 100, "distrito": bair, "municipio": loca, "fonte": "ViaCEP/ArcGIS"}, expire=2592000)
                    return res_final

# Resolução O(1) Municipal (Centróides Offline)
if tipo_entrada == "MUNICIPIO" and ctx.get("municipio") and ctx.get("uf"):
    mun_nome, uf_nome = ctx["municipio"], ctx["uf"]
    if mun_nome in IBGE_MUNICIPIOS:
        for item in IBGE_MUNICIPIOS[mun_nome]:
            if item["uf"] == uf_nome and item.get("lat", 0.0) != 0.0 and item.get("lon", 0.0) != 0.0:
                endereco_ibge = f"{mun_nome}, {IBGE_ESTADOS.get(uf_nome, uf_nome)}, BRASIL"
                res_ibge = (item["lat"], item["lon"], endereco_ibge, "ALTISSIMA", 100, "", mun_nome, "BASE_IBGE_LOCAL")
                cache_geo.set(cache_key, {"lat": res_ibge[0], "lon": res_ibge[1], "endereco": res_ibge[2], "confianca": res_ibge[3], "score_num": res_ibge[4], "distrito": res_ibge[5], "municipio": res_ibge[6], "fonte": res_ibge[7]}, expire=2592000)
                return res_ibge

# Orquestração por Alta Hierarquia
if tipo_entrada == "POI":
    res_google_geo = API_Google_Geocoding_Scraper(endereco_canonico)
    if res_google_geo: candidatos_validos.extend(res_google_geo)
    res_poi = API_Overpass_POIs(semantica.normalizar(texto_cru))
    if res_poi: candidatos_validos.extend(res_poi)
    
elif tipo_entrada in ["ENDERECO_COMPLETO", "LOGRADOURO"]:
    res_arc = API_ArcGIS(endereco_canonico, ctx=contexto_estruturado)
    if res_arc: candidatos_validos.extend(res_arc)
    res_google_geo = API_Google_Geocoding_Scraper(endereco_canonico)
    if res_google_geo: candidatos_validos.extend(res_google_geo)
    res_nom = API_Nominatim(endereco_canonico, ctx=contexto_estruturado)
    if res_nom: candidatos_validos.extend(res_nom)
    
elif tipo_entrada in ["BAIRRO", "MUNICIPIO", "DISTRITO"]:
    res_pho = API_Photon(endereco_canonico)
    if res_pho: candidatos_validos.extend(res_pho)
    res_nom = API_Nominatim(endereco_canonico, ctx=contexto_estruturado)
    if res_nom: candidatos_validos.extend(res_nom)
    
else:
    res_google_geo = API_Google_Geocoding_Scraper(endereco_canonico)
    if res_google_geo: candidatos_validos.extend(res_google_geo)
    res_pho = API_Photon(endereco_canonico)
    if res_pho: candidatos_validos.extend(res_pho)
    res_arc = API_ArcGIS(endereco_canonico, ctx=contexto_estruturado)
    if res_arc: candidatos_validos.extend(res_arc)
        
res_final = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru)

if not res_final and tipo_entrada not in ["BAIRRO", "MUNICIPIO"]:
    res_nom = API_Nominatim(endereco_canonico, ctx=contexto_estruturado)
    if res_nom:
        candidatos_validos.extend(res_nom)
        res_final = processar_consenso_dinamico(candidatos_validos, tipo_entrada, texto_cru)

if res_final:
    cache_geo.set(cache_key, {"lat": res_final[0], "lon": res_final[1], "endereco": res_final[2], "confianca": res_final[3], "score_num": res_final[4], "distrito": res_final[5], "municipio": res_final[6], "fonte": res_final[7]}, expire=2592000)
    return res_final
    
return 0.0, 0.0, endereco_canonico, "BAIXA", 0, "", "", "N/A"

==============================================================================

🚀 MOTOR DE ROTEAMENTO (ARBITRAGEM DE PROVEDORES E PERFIS DE DISTÂNCIA)

==============================================================================

def extrair_dados_reais_google(origem_raw, destino_raw, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=True):cache_key = f"{origem_raw}|{destino_raw}|{usar_coordenadas}"if cache_key in cache_google: return cache_google[cache_key]

if not usar_coordenadas and lat_d != 0.0 and lon_d != 0.0:
    google_dest_geo = API_Google_Geocoding_Scraper(destino_raw)
    if google_dest_geo:
        dist_cross = calcular_distancia_vincenty(lat_d, lon_d, google_dest_geo[0]["lat"], google_dest_geo[0]["lon"])
        if dist_cross > 20.0: return None 

origem_param = f"{lat_o},{lon_o}" if usar_coordenadas else requests.utils.quote(origem_raw)
destino_param = f"{lat_d},{lon_d}" if usar_coordenadas else requests.utils.quote(destino_raw)
url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
link_maps = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(origem_raw)}&destination={requests.utils.quote(destino_raw)}&travelmode=driving"
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": "https://www.google.com/maps"}

try:
    resposta = session.get(url_api, headers=headers, timeout=8)
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
        score_google = 70 + (10 if km_puro > 0 else 0) + (10 if match_tempo[0] else 0) + (10 if km_puro >= dist_linha_reta else 0)
        res = (km_puro, match_tempo[0], link_maps, envolve_balsa, score_google)
        cache_google.set(cache_key, res, expire=2592000); return res
except Exception: pass
return None

def rota_osrm(lat_o, lon_o, lat_d, lon_d):try:url = f"https://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=false"r = session.get(url, timeout=5).json()if r.get("routes"):km = round(r["routes"][0]["distance"] / 1000, 2)minutos = round(r["routes"][0]["duration"] / 60)return km, f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min", "OSRM", 95except Exception: passreturn None

def obter_fator_desvio_rodoviario(linha_reta):return 1.45 if linha_reta < 5.0 else 1.35 if linha_reta < 20.0 else 1.25 if linha_reta < 100.0 else 1.18

def calcular_pipeline_logistico(origem, destino, perfil_rota="shortest"):start_total = time.time()origem_clean, destino_clean = str(origem).strip(), str(destino).strip()

chave_rota_cache = f"ROTA_{semantica.normalizar(origem_clean)}->{semantica.normalizar(destino_clean)}"
if chave_rota_cache in cache_rotas: return cache_rotas[chave_rota_cache]

start_geo = time.time()
lat_o, lon_o, end_oficial_o, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o = obter_coordenadas_e_endereco_oficial(origem_clean)
lat_d, lon_d, end_oficial_d, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d = obter_coordenadas_e_endereco_oficial(destino_clean)
tempo_geocoding = round(time.time() - start_geo, 2)

start_rot = time.time()

# Blindagem e Verificação de Coordenadas
if all([lat_o is not None, lon_o is not None, lat_d is not None, lon_d is not None]) and lat_o != 0.0 and lat_d != 0.0:
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)
else:
    dist_linha_reta = 0.0
    
print(f"Linha reta: {dist_linha_reta} km | Origem=({lat_o},{lon_o}) | Destino=({lat_d},{lon_d})")

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
        cache_rotas.set(chave_rota_cache, retorno, expire=2592000); return retorno

res_google = extrair_dados_reais_google(end_oficial_o, end_oficial_d, lat_o, lon_o, lat_d, lon_d, dist_linha_reta, usar_coordenadas=usar_coords)

# Arbitragem de Provedores Logísticos
if perfil_rota == "shortest":
    opcoes = []
    if res_osrm: opcoes.append((res_osrm[0], res_osrm[1], link_fallback, "Não", dist_linha_reta, res_osrm[2], res_osrm[3]))
    if res_google: opcoes.append((res_google[0], res_google[1], res_google[2], res_google[3], dist_linha_reta, "Google Preview", res_google[4]))
    
    if opcoes:
        melhor_opcao = min(opcoes, key=lambda x: x[0]) 
        tempo_roteamento = round(time.time() - start_rot, 2); tempo_total = round(time.time() - start_total, 2)
        retorno = (*melhor_opcao, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total)
        cache_rotas.set(chave_rota_cache, retorno, expire=2592000); return retorno

if res_google:
    tempo_roteamento = round(time.time() - start_rot, 2); tempo_total = round(time.time() - start_total, 2)
    retorno = (res_google[0], res_google[1], res_google[2], res_google[3], dist_linha_reta, "Google Preview", res_google[4], conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total)
    cache_rotas.set(chave_rota_cache, retorno, expire=2592000); return retorno

km_terrestre = round(dist_linha_reta * obter_fator_desvio_rodoviario(dist_linha_reta), 2)
v_comercial = 45.0 if km_terrestre < 50.0 else 65.0
minutos_est = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0 else 0
tempo_geo_str = f"{minutos_est} min" if minutos_est < 60 else f"{minutos_est // 60} h {minutos_est % 60} min"
tempo_roteamento = round(time.time() - start_rot, 2); tempo_total = round(time.time() - start_total, 2)

retorno = (km_terrestre, tempo_geo_str, link_fallback, "Não", dist_linha_reta, "Geodésico Adaptativo", 70, conf_o, score_num_o, dist_o, mun_o, fonte_geo_o, end_oficial_o, conf_d, score_num_d, dist_d, mun_d, fonte_geo_d, end_oficial_d, lat_o, lon_o, lat_d, lon_d, tempo_geocoding, tempo_roteamento, tempo_total)
cache_rotas.set(chave_rota_cache, retorno, expire=2592000)
return retorno

def embrulhar_task_paralela(item):par_id, orig, dest = itemtry: return par_id, calcular_pipeline_logistico(orig, dest, perfil_rota="shortest")except Exception: return par_id, None

==============================================================================

🚗 INTERFACE STREAMLIT COM ENGINE DE DEDUPLICAÇÃO ASINTÓTICA O(U)

==============================================================================

st.title("🚗 Gerenciador de Rotas Inteligentes")st.subheader("Engine de Resolução Espacial Nacional — Operação Corporativa")st.write("Insira uma planilha Excel (.xlsx) contendo as colunas Origem e Destino.")

arquivo_carregado = st.file_uploader("Selecionar Arquivo Excel", type=["xlsx"])

if arquivo_carregado is not None:df = pd.read_excel(arquivo_carregado)df.columns = df.columns.str.strip().str.title()

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
        
        for idx, origem, destino in mapeamento_linhas:
            par = (origem, destino)
            res = resultados_unicos.get(par)
            
            if res:
                df.at[idx, 'Distancia'] = res[0]; df.at[idx, 'Tempo'] = res[1]
                df.at[idx, 'Link da Rota'] = res[2]; df.at[idx, 'Balsas'] = res[3]
                df.at[idx, 'Linha Reta'] = res[4]; df.at[idx, 'Fonte da Rota'] = res[5]
                df.at[idx, 'Score da Rota'] = res[6]; df.at[idx, 'Confianca Origem'] = res[7]
                df.at[idx, 'Score Num Origem'] = res[8]; df.at[idx, 'Distrito Origem'] = res[9]
                df.at[idx, 'Municipio Origem'] = res[10]; df.at[idx, 'Fonte Geocoding Origem'] = res[11]
                df.at[idx, 'Endereco Oficial Origem'] = res[12]; df.at[idx, 'Confianca Destino'] = res[13]
                df.at[idx, 'Score Num Destino'] = res[14]; df.at[idx, 'Distrito Destino'] = res[15]
                df.at[idx, 'Municipio Destino'] = res[16]; df.at[idx, 'Fonte Geocoding Destino'] = res[17]
                df.at[idx, 'Endereco Oficial Destino'] = res[18]; df.at[idx, 'Lat Origem'] = res[19]
                df.at[idx, 'Lon Origem'] = res[20]; df.at[idx, 'Lat Destino'] = res[21]
                df.at[idx, 'Lon Destino'] = res[22]; df.at[idx, 'Tempo Geocoding (s)'] = res[23]
                df.at[idx, 'Tempo Roteamento (s)'] = res[24]; df.at[idx, 'Tempo Total (s)'] = res[25]
                
                score_o, score_d, score_r = res[8], res[14], res[6]
                score_global = round((0.35 * score_o) + (0.35 * score_d) + (0.30 * score_r), 2)
                df.at[idx, 'Score Final Global'] = score_global
                df.at[idx, 'Status da Rota'] = "Excelente" if score_global >= 90 else "Boa" if score_global >= 80 else "Aceitável" if score_global >= 70 else "Revisar"
            else:
                df.at[idx, 'Status da Rota'] = "Erro de Processamento"

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
