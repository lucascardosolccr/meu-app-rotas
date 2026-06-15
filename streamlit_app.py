import streamlit as st
import pandas as pd
import requests
import time
import math
import io
import re
import json
from unidecode import unidecode

# Configuração da página do site seguindo rigorosamente as boas práticas de UI/UX
st.set_page_config(
    page_title="Gerenciador de Rotas Inteligentes", 
    page_icon="🚗", 
    layout="centered"
)

# ==============================================================================
# 🧠 MEMÓRIA CACHE DE LONGO PRAZO E DICIONÁRIOS RESTRITOS (CAMADA 10, 19, 20, 24)
# ==============================================================================
if "cache_geocodificacao" not in st.session_state:
    st.session_state["cache_geocodificacao"] = {}

if "ibge_estados" not in st.session_state:
    st.session_state["ibge_estados"] = {}

if "ibge_municipios" not in st.session_state:
    st.session_state["ibge_municipios"] = {}

SINONIMOS = {
    "UNB": "UNIVERSIDADE DE BRASILIA",
    "CATOLICA": "UNIVERSIDADE CATOLICA",
    "JK": "JUSCELINO KUBITSCHEK",
    "HBDF": "HOSPITAL DE BASE",
    "HRAN": "HOSPITAL REGIONAL DA ASA NORTE",
    "RODOVIARIA": "TERMINAL RODOVIARIO"
}

POI_KEYWORDS = [
    "AEROPORTO", "HOSPITAL", "UNIVERSIDADE", "FACULDADE", "ESCOLA",
    "SHOPPING", "HOTEL", "RODOVIARIA", "ESTADIO", "MINISTERIO",
    "IBAMA", "ANTAQ", "INCRA", "CONDOMINIO", "PARQUE", "FAZENDA", "ASSENTAMENTO"
]

# ==============================================================================
# 🎛️ INFRAESTRUTURA DE DADOS IBGE (CAMADA 19 E 20)
# ==============================================================================
def inicializar_bases_ibge_cache():
    """Carrega dinamicamente os dicionários de cidades e estados do IBGE de forma agnóstica"""
    if not st.session_state["ibge_estados"]:
        try:
            r = requests.get("https://servicodados.ibge.gov.br/api/v1/localidades/estados", timeout=5)
            if r.status_code == 200:
                for est in r.json():
                    st.session_state["ibge_estados"][est["sigla"]] = unidecode(est["nome"]).upper()
        except Exception:
            pass
            
    if not st.session_state["ibge_municipios"]:
        try:
            r = requests.get("https://servicodados.ibge.gov.br/api/v1/localidades/municipios", timeout=6)
            if r.status_code == 200:
                for mun in r.json():
                    nome_norm = unidecode(mun["nome"]).upper().strip()
                    st.session_state["ibge_municipios"][nome_norm] = {
                        "id": mun["id"],
                        "uf": mun["microrregiao"]["mesorregiao"]["UF"]["sigla"].upper()
                    }
        except Exception:
            pass

# Executa a carga síncrona na inicialização do ciclo de vida do Streamlit
inicializar_bases_ibge_cache()

# ==============================================================================
# 🧹 PIPELINE DE ENGENHARIA DE QUALIDADE DE DADOS (CAMADA 1, 2, 21, 22, 23, 24)
# ==============================================================================
def normalizar_endereco_universal(texto):
    """Camada 1 e 24: Remoção de caracteres inválidos, Unicode Normalization e Sinônimos"""
    if not texto or pd.isna(texto):
        return ""
    t = str(texto).strip()
    t = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', t)  # Remove caracteres invisíveis de controle
    t = unidecode(t).upper()
    
    # Tabela de expansão de abreviações e acentuações corrigidas
    abreviacoes = {
        r'\bAV\b': 'AVENIDA', r'\bR\b': 'RUA', r'\bQD\b': 'QUADRA', r'\bLT\b': 'LOTE',
        r'\bCJ\b': 'CONJUNTO', r'\bCONJ\b': 'CONJUNTO', r'\bBL\b': 'BLOCO', r'\bAPT\b': 'APARTAMENTO',
        r'\bST\b': 'SETOR', r'\bCH\b': 'CHACARA', r'\bSHIS\b': 'SETOR DE HABITACOES INDIVIDUAIS SUL'
    }
    for padrao, expansao in abreviacoes.items():
        t = re.sub(padrao, expansao, t)
        
    # Camada 24: Correção semântica e expansão de sinônimos corporativos
    for chave, valor in SINONIMOS.items():
        t = re.sub(r'\b' + chave + r'\b', valor, t)
        
    t = re.sub(r'\s+', ' ', t)
    return t.strip()

def inferir_estado_por_toponimo(texto_normalizado):
    """Camada 21: Analisa as palavras chaves e cruza com a base IBGE para deduzir o estado"""
    palavras = texto_normalizado.split()
    for i in range(len(palavras)):
        for j in range(i + 1, len(palavras) + 1):
            chunk = " ".join(palavras[i:j])
            if chunk in st.session_state["ibge_municipios"]:
                return st.session_state["ibge_municipios"][chunk]["uf"]
    return None

def expandir_contexto_incompleto(texto):
    """Camada 22 e 23: Identifica endereços muito curtos e injeta âncora contextual"""
    texto_norm = normalizar_endereco_universal(texto)
    tokens = texto_norm.split()
    
    # Camada 22: Detecção de endereço incompleto
    if len(tokens) <= 2 or not any(c.isdigit() for c in texto_norm):
        uf_inferida = inferir_estado_por_toponimo(texto_norm)
        if uf_inferida:
            return f"{texto_norm}, {uf_inferida}, BRASIL"
            
    if "BRASIL" not in texto_norm:
        return f"{texto_norm}, BRASIL"
    return texto_norm

def parece_poi(texto_normalizado):
    """Camada 11: Detector de intenção semântica de Pontos de Interesse"""
    return any(keyword in texto_normalizado for keyword in POI_KEYWORDS)

# ==============================================================================
# 🗺️ RESOLUÇÃO MULTI-FONTE, OVERPASS E CONSENSO (CAMADA 3, 4, 5, 6, 7, 8, 9, 12, 13, 14, 17)
# ==============================================================================
def extrair_dados_reais_google(origem_raw, destino_raw, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=True):
    """
    CAMADA BRUTA PRESERVADA - Intercepta a API interna de direções do Google Maps.
    """
    if usar_coordenadas and lat_o and lon_o and lat_d and lon_d and lat_o != 0.0 and lat_d != 0.0:
        origem_param = f"{lat_o},{lon_o}"
        destino_param = f"{lat_d},{lon_d}"
        url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
    else:
        origem_param = requests.utils.quote(f"{origem_raw}".strip())
        destino_param = requests.utils.quote(f"{destino_raw}".strip())
        url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
    
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(str(origem_raw).strip())}&destination={requests.utils.quote(str(destino_raw).strip())}&travelmode=driving"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.google.com/maps",
        "Accept": "*/*"
    }
    
    try:
        resposta = requests.get(url_api, headers=headers, timeout=12)
        texto_resposta = resposta.text
        
        regex_km = r'\"(\d+[\.,]?\d*)\s*km\"'
        match_km = re.findall(regex_km, texto_resposta)
        
        regex_tempo = r'\"(\d+\s*h\s*\d+\s*min|\d+\s*h|\d+\s*min)\"'
        match_tempo = re.findall(regex_tempo, texto_resposta)
        
        km_txt = match_km[0] if match_km else ""
        tempo_txt = match_tempo[0] if match_tempo else ""
        
        if km_txt and tempo_txt:
            km_puro = float(km_txt.replace('.', '').replace(',', '.'))
            envolve_balsa = "Não"
            padroes_balsa = [
                r'\"utilizar\s+balsa\b', r'\"pegar\s+balsa\b', r'\"travessia\s+de\s+balsa\b',
                r'\"balsa\s+de\s+veículos\b', r'\"ferry\b', r'\"travessia\s+por\s+balsa\b'
            ]
            
            if any(re.search(padrao, texto_resposta.lower()) for padrao in padroes_balsa):
                envolve_balsa = "Sim"
                
            return km_puro, tempo_txt, link_maps, envolve_balsa
    except Exception:
        pass
    return None

def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    """CÁLCULO GEODÉSICO PRESERVADO - Vincenty (1975)"""
    if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0:
        return 0.0
    try:
        a = 6378137.0
        b = 6356752.314245
        f = 1 / 298.257223563
        L = math.radians(lon2 - lon1)
        U1 = math.atan((1 - f) * math.tan(math.radians(lat1)))
        U2 = math.atan((1 - f) * math.tan(math.radians(lat2)))
        sinU1, cosU1 = math.sin(U1), math.cos(U1)
        sinU2, cosU2 = math.sin(U2), math.cos(U2)
        lambda_lon = L
        
        for _ in range(200):
            sinLambda, cosLambda = math.sin(lambda_lon), math.cos(lambda_lon)
            sinSigma = math.sqrt((cosU2 * sinLambda) ** 2 + (cosU1 * sinU2 - sinU1 * cosU2 * cosLambda) ** 2)
            if sinSigma == 0: return 0.0
            cosSigma = sinU1 * sinU2 + cosU1 * cosU2 * cosLambda
            sigma = math.atan2(sinSigma, cosSigma)
            sinAlpha = cosU1 * cosU2 * sinLambda / sinSigma
            cosSqAlpha = 1 - sinAlpha ** 2
            cos2SigmaM = cosSigma - 2 * sinU1 * sinU2 / cosSqAlpha if cosSqAlpha != 0 else 0
            C = f / 16 * cosSqAlpha * (4 + f * (4 - 3 * cosSqAlpha))
            lambdaPrev = lambda_lon
            lambda_lon = L + (1 - f) * C * sinAlpha * (sigma + f * sinAlpha * (cos2SigmaM + C * cosSigma * (-1 + 2 * cos2SigmaM ** 2)))
            if abs(lambda_lon - lambdaPrev) < 1e-12: break
            
        uSq = cosSqAlpha * (a ** 2 - b ** 2) / (b ** 2)
        A = 1 + uSq / 16384 * (4096 + uSq * (-768 + uSq * (320 - 175 * uSq)))
        B = uSq / 1024 * (256 + uSq * (-128 + uSq * (74 - 47 * uSq)))
        deltaSigma = B * sinSigma * (cos2SigmaM + B / 4 * (cosSigma * (-1 + 2 * cos2SigmaM ** 2) - B / 6 * cos2SigmaM * (-3 + 4 * sinSigma ** 2) * (-3 + 4 * cos2SigmaM ** 2)))
        s = b * A * (sigma - deltaSigma)
        return round(s / 1000, 2)
    except Exception:
        return 0.0

def buscar_poi_overpass(texto_normalizado):
    """Camada 5 e 12: Consulta geoespacial na infraestrutura Overpass API (OSM)"""
    try:
        query_osm = f"""
        [out:json][timeout:15];
        (
          node["name"~"{texto_normalizado}",i];
          way["name"~"{texto_normalizado}",i];
          relation["name"~"{texto_normalizado}",i];
        );
        out center;
        """
        r = requests.post("https://overpass-api.de/api/interpreter", data={"data": query_osm}, timeout=15)
        if r.status_code == 200:
            elements = r.json().get("elements", [])
            if elements:
                el = elements[0]
                lat = el.get("lat", el.get("center", {}).get("lat", 0.0))
                lon = el.get("lon", el.get("center", {}).get("lon", 0.0))
                name = el.get("tags", {}).get("name", texto_normalizado)
                return {"lat": lat, "lon": lon, "endereco": f"{name}, BRASIL", "fonte": "OVERPASS", "score": 95}
    except Exception:
        pass
    return None

def executar_reverse_geocoding_enrichment(lat, lon):
    """Camada 6, 7 e 17: Reconstrução Reversa Máxima via Malha Cadastral Nominatim"""
    resultado = {"logradouro": "", "bairro": "", "cidade": "", "municipio": "", "distrito": "", "estado": "", "cep": "", "pais": "BRASIL"}
    try:
        url_rev = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&addressdetails=1"
        headers = {"User-Agent": "GerenciadorRotasUniversais/6.0 (suporte@logistica.com)"}
        r = requests.get(url_rev, headers=headers, timeout=5)
        if r.status_code == 200:
            addr = r.json().get("address", {})
            resultado["logradouro"] = addr.get("road", addr.get("pedestrian", ""))
            resultado["bairro"] = addr.get("neighbourhood", addr.get("suburb", addr.get("city_district", "")))
            resultado["cidade"] = addr.get("city", addr.get("town", addr.get("municipality", "")))
            resultado["municipio"] = addr.get("municipality", resultado["cidade"])
            resultado["distrito"] = addr.get("city_district", addr.get("suburb", ""))
            resultado["estado"] = addr.get("state", "").upper()
            resultado["cep"] = addr.get("postcode", "")
    except Exception:
        pass
    return resultado

def escolher_melhor_resultado_consenso(resultados):
    """Camada 8, 9, 25, 26, 27, 28 e 29: Matriz de Votação e Score de Confiança Unificado"""
    if not resultados:
        return None
    if len(resultados) == 1:
        return resultados[0]
        
    for i, c1 in enumerate(resultados):
        votos = 0
        for j, c2 in enumerate(resultados):
            if i != j:
                dist = calcular_distancia_vincenty(c1["lat"], c1["lon"], c2["lat"], c2["lon"])
                if dist <= 3.0:  # Janela de tolerância geodésica de 3 km
                    votos += 1
        c1["consenso"] = votos

    resultados.sort(key=lambda x: (x.get("consenso", 0), x.get("score", 0)), reverse=True)
    return resultados[0]

def obter_coordenadas_e_endereco_oficial(localidade):
    """
    CAMADA GEOGRÁFICA INTEROPERÁVEL REESTRUTURADA (10 Camadas de Resolução Universal)
    """
    texto_cru = str(localidade).strip()
    if not texto_cru or texto_cru.lower() == 'nan':
        return 0.0, 0.0, "", "BAIXA", "", ""
        
    if texto_cru in st.session_state["cache_geocodificacao"]:
        c = st.session_state["cache_geocodificacao"][texto_cru]
        return c["lat"], c["lon"], c["endereco"], c["confianca"], c["municipio"], c["distrito"]

    texto_norm = normalizar_endereco_universal(texto_cru)
    texto_expandido = expandir_contexto_incompleto(texto_cru)
    
    resultados_concorrentes = []

    # Camada 3: Verificação Postal Redundante (ViaCEP + BrasilAPI)
    digits_cep = re.sub(r'\D', '', texto_cru)
    if len(digits_cep) == 8:
        logr, bair, loca, uf = camada_postal_redundante(digits_cep)
        if loca:
            addr_correios = ", ".join([c for c in [logr, bair, loca, uf] if c.strip()]) + f", CEP {digits_cep}, BRASIL"
            url_arc = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(addr_correios)}&maxLocations=1&sourceCountry=BRA"
            try:
                res_arc = requests.get(url_arc, timeout=4).json()
                if res_arc.get('candidates'):
                    loc = res_arc['candidates'][0]['location']
                    retorno = (float(loc['y']), float(loc['x']), addr_correios, "ALTISSIMA", loca, bair)
                    st.session_state["cache_geocodificacao"][texto_cru] = {"lat": retorno[0], "lon": retorno[1], "endereco": retorno[2], "confianca": retorno[3], "municipio": retorno[4], "distrito": retorno[5]}
                    return retorno
            except Exception:
                pass
            return 0.0, 0.0, addr_correios, "ALTA", loca, bair

    # Camada 5 e 12: Ativação Preventiva de POI Search via Overpass API
    if parece_poi(texto_norm):
        poi_res = buscar_poi_overpass(texto_norm)
        if poi_res:
            resultados_concorrentes.append(poi_res)

    # Camada 4: Geocodificação Multi-Fonte Corporativa (ArcGIS)
    url_arc = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(texto_expandido)}&maxLocations=2&sourceCountry=BRA&outFields=StAddr,Neighborhood,City,RegionAbbr"
    try:
        res_arc = requests.get(url_arc, timeout=5).json()
        if res_arc.get('candidates'):
            cand = res_arc['candidates'][0]
            attr = cand.get('attributes', {})
            resultados_concorrentes.append({
                "lat": float(cand['location']['y']), "lon": float(cand['location']['x']),
                "endereco": cand.get('address', texto_expandido), "fonte": "ARCGIS", "score": cand.get('score', 80)
            })
    except Exception:
        pass

    # Camada 4: Geocodificação Multi-Fonte Aberta (Nominatim)
    url_osm = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(texto_expandido)}&limit=1&addressdetails=1&countrycodes=br"
    headers = {"User-Agent": "GerenciadorRotasUniversais/6.0 (suporte@logistica.com)"}
    try:
        res_osm = requests.get(url_osm, headers=headers, timeout=5).json()
        if res_osm:
            alvo = res_osm[0]
            resultados_concorrentes.append({
                "lat": float(alvo['lat']), "lon": float(alvo['lon']),
                "endereco": alvo.get('display_name', texto_expandido), "fonte": "NOMINATIM", "score": 85
            })
    except Exception:
        pass

    # Processamento de Consenso e Enriquecimento Máximo
    vencedor = escolher_melhor_resultado_consenso(resultados_concorrentes)
    if vencedor:
        metadados_reversos = executar_reverse_geocoding_enrichment(vencedor["lat"], vencedor["lon"])
        
        score_final = vencedor["score"]
        if metadados_reversos["cep"]: score_final += 10
        if metadados_reversos["bairro"]: score_final += 5
        if len(texto_cru.split()) >= 4: score_final += 10
        
        confianca = "BAIXA"
        if score_final >= 95: confianca = "ALTISSIMA"
        elif score_final >= 85: confianca = "ALTA"
        elif score_final >= 70: confianca = "MEDIA"
        
        rua = metadados_reversos["logradouro"] if metadados_reversos["logradouro"] else texto_cru
        componentes_montagem = [rua, metadados_reversos["bairro"], metadados_reversos["cidade"], metadados_reversos["estado"]]
        endereco_final = ", ".join([c for c in componentes_montagem if c.strip()]) + ", BRASIL"
        
        retorno = (vencedor["lat"], vencedor["lon"], endereco_final, confianca, metadados_reversos["municipio"], metadados_reversos["distrito"])
        st.session_state["cache_geocodificacao"][texto_cru] = {"lat": retorno[0], "lon": retorno[1], "endereco": retorno[2], "confianca": retorno[3], "municipio": retorno[4], "distrito": retorno[5]}
        return retorno

    return 0.0, 0.0, texto_expandido, "BAIXA", "", ""

def camada_postal_redundante(cep_alvo):
    """Resolução postal redundante interna"""
    cep_limpo = re.sub(r'\D', '', str(cep_alvo))
    if len(cep_limpo) == 8:
        try:
            res = requests.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=4).json()
            if "erro" not in res:
                return res.get('logradouro', ''), res.get('bairro', ''), res.get('localidade', ''), res.get('uf', '')
        except Exception:
            pass
        try:
            res = requests.get(f"https://brasilapi.com.br/api/cep/v1/{cep_limpo}", timeout=4).json()
            if "name" not in res:
                return res.get('street', ''), res.get('neighborhood', ''), res.get('city', ''), res.get('state', '')
        except Exception:
            pass
    return "", "", "", ""

# ==============================================================================
# 🚀 BLINDAGEM DO MOTOR DE ROTAS MULTI-CAMADAS (PARTE 6 — CAMADAS DE 1 A 4)
# ==============================================================================
def rota_osrm(lat_o, lon_o, lat_d, lon_d):
    """Camada 2 de Roteamento: Infraestrutura de Dados Abertos OSRM Engine (Gratuito)"""
    try:
        url_osrm = f"https://router.project-osrm.org/route/v1/driving/{lon_o},{lat_o};{lon_d},{lat_d}?overview=false"
        r = requests.get(url_osrm, timeout=10).json()
        if r.get("routes"):
            rota = r["routes"][0]
            km = round(rota["distance"] / 1000, 2)
            minutos = round(rota["duration"] / 60)
            tempo_str = f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min" if minutos % 60 > 0 else f"{minutos // 60} h"
            return {"distancia": km, "tempo": tempo_str, "fonte": "OSRM", "score": 95}
    except Exception:
        pass
    return None

def obter_fator_desvio_rodoviario(linha_reta):
    """Camada 4 de Roteamento: Modelo Geodésico Adaptativo Baseado na Escala Espacial"""
    if linha_reta < 5.0: return 1.45
    if linha_reta < 20.0: return 1.35
    if linha_reta < 100.0: return 1.25
    if linha_reta < 500.0: return 1.18
    return 1.12

def calcular_pipeline_logistico(origem, destino):
    """Pipeline Central Agnóstico com Roteamento Universal de Contingência Síncrona"""
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    # Executa a desambiguação espacial e resolve as propriedades de completude geográficas
    lat_o, lon_o, o_oficial, conf_o, mun_o, dist_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    lat_d, lon_d, d_oficial, conf_d, mun_d, dist_d = obter_coordenadas_e_endereco_oficial(destino_clean)
    
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)
    
    # --------------------------------------------------------------------------
    # 🔏 MECANISMO DE TRAVA GEODÉSICA DE EXCLUSÃO INTERESTADUAL DE COORDENADAS
    # --------------------------------------------------------------------------
    usar_coords = True if (lat_o != 0.0 and lat_d != 0.0) else False
    if usar_coords and dist_linha_reta > 150.0:
        siglas_originais = re.findall(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM|PA|MT)\b', origem_clean.upper() + " " + destino_clean.upper())
        if len(set(siglas_originais)) <= 1:
            usar_coords = False

    # ORQUESTRADOR CENTRAL DE ROTAS DE CONTINGÊNCIA (FALLBACK EM CASCATA)
    google_res = extrair_dados_reais_google(o_oficial, d_oficial, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=usar_coords)
    if google_res and google_res[0] < (dist_linha_reta * 4.0):
        return google_res[0], google_res[1], google_res[2], google_res[3], dist_linha_reta, "Google Preview", 100, conf_o, dist_o, mun_o, conf_d, dist_d, mun_d

    if lat_o != 0.0 and lat_d != 0.0:
        osrm_res = rota_osrm(lat_o, lon_o, lat_d, lon_d)
        if osrm_res:
            link_fallback = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(o_oficial)}&destination={requests.utils.quote(d_oficial)}&travelmode=driving"
            return osrm_res["distancia"], osrm_res["tempo"], link_fallback, "Não", dist_linha_reta, osrm_res["fonte"], osrm_res["score"], conf_o, dist_o, mun_o, conf_d, dist_d, mun_d

    # Provedor Terceiro: Modelo Geodésico Adaptativo Matemático
    link_fallback = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(o_oficial)}&destination={requests.utils.quote(d_oficial)}&travelmode=driving"
    km_geodesico = round(dist_linha_reta * obter_fator_desvio_rodoviario(dist_linha_reta), 2)
    v_comercial = 45.0 if km_geodesico < 50.0 else 65.0
    minutos_est = round((km_geodesico / v_comercial) * 60) if km_geodesico > 0 else 0
    tempo_geodesico = f"{minutos_est} min" if minutos_est < 60 else f"{minutos_est // 60} h {minutos_est % 60} min"
    
    # LINHA 260 CORRIGIDA: Inserção da vírgula regulamentar ausente antes de "Geodésico Adaptativo"
    return km_geodesico, tempo_geodesico, link_fallback, "Não", dist_linha_reta, "Geodésico Adaptativo", 70, conf_o, dist_o, mun_o, conf_d, dist_d, mun_d

# --- INTERFACE VISUAL NO STREAMLIT ---
st.write("Envie sua planilha Excel com as colunas **Origem** e **Destino** para processar as distâncias automaticamente.")

arquivo_carregado = st.file_uploader("Arraste ou selecione seu arquivo Excel (.xlsx)", type=["xlsx"])

if arquivo_carregado is not None:
    df = pd.read_excel(arquivo_carregado)
    
    if 'Origem' not in df.columns or 'Destino' not in df.columns:
        st.error("Erro: A planilha enviada precisa ter as colunas com os nomes exatos: 'Origem' e 'Destino'.")
    else:
        st.success("Planilha carregada com sucesso!")
        
        if st.button("Iniciar Processamento das Rotas"):
            novas_colunas = [
                'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta', 
                'Fonte da Rota', 'Score da Rota', 'Confianca Origem', 
                'Distrito Origem', 'Municipio Origem', 'Confianca Destino', 
                'Distrito Destino', 'Municipio Destino'
            ]
            for col in novas_colunas:
                df[col] = None
                
            total_linhas = len(df)
            barra_progresso = st.progress(0)
            texto_status = st.empty()
            
            for index, linha in df.iterrows():
                origem = str(linha['Origem']).strip()
                destino = str(linha['Destino']).strip()
                
                if origem and destino and origem != 'nan' and destino != 'nan':
                    texto_status.text(f"🔢 Processando linha {index+1} de {total_linhas}: {origem} ➔ {destino}")
                    
                    res_pipeline = calcular_pipeline_logistico(origem, destino)
                    
                    df.at[index, 'Distancia'] = res_pipeline[0]
                    df.at[index, 'Tempo'] = res_pipeline[1]
                    df.at[index, 'Link da Rota'] = res_pipeline[2]
                    df.at[index, 'Balsas'] = res_pipeline[3]
                    df.at[index, 'Linha Reta'] = res_pipeline[4]
                    df.at[index, 'Fonte da Rota'] = res_pipeline[5]
                    df.at[index, 'Score da Rota'] = res_pipeline[6]
                    df.at[index, 'Confianca Origem'] = res_pipeline[7]
                    df.at[index, 'Distrito Origem'] = res_pipeline[8]
                    df.at[index, 'Municipio Origem'] = res_pipeline[9]
                    df.at[index, 'Confianca Destino'] = res_pipeline[10]
                    df.at[index, 'Distrito Destino'] = res_pipeline[11]
                    df.at[index, 'Municipio Destino'] = res_pipeline[12]
                    
                    time.sleep(0.6)
                    
                barra_progresso.progress((index + 1) / total_linhas)
                
            texto_status.text("✨ Processamento concluído com sucesso!")
            
            ordem_colunas = [
                'Origem', 'Destino', 'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta',
                'Fonte da Rota', 'Score da Rota', 'Confianca Origem', 'Distrito Origem', 'Municipio Origem',
                'Confianca Destino', 'Distrito Destino', 'Municipio Destino'
            ]
            df = df.reindex(columns=ordem_colunas)
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False)
            dados_excel = output.getvalue()
            
            st.write("---")
            st.balloons()
            
            st.download_button(
                label="📥 Baixar Planilha Pronta",
                data=dados_excel,
                file_name="planilha_rotas_calculada.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
