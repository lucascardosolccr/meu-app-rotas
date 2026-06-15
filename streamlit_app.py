import streamlit as st
import pandas as pd
import requests
import time
import math
import io
import re

# Configuração da página do site seguindo boas práticas de UI/UX
st.set_page_config(
    page_title="Gerenciador de Rotas Inteligentes", 
    page_icon="🚗", 
    layout="centered"
)

def normalizar_texto_endereco(texto):
    """
    CAMADA 1 e 2: LIMPEZA E PADRONIZAÇÃO - Expande abreviações e limpa espaçamentos.
    """
    if not texto or pd.isna(texto):
        return ""
    txt = str(texto).strip()
    
    abreviacoes = {
        r'\bAV\b': 'Avenida',
        r'\bR\b': 'Rua',
        r'\bQD\b': 'Quadra',
        r'\bLT\b': 'Lote',
        r'\bCJ\b': 'Conjunto',
        r'\bBL\b': 'Bloco',
        r'\bAPT\b': 'Apartamento',
        r'\bCH\b': 'Chácara',
        r'\bCHAC\b': 'Chácara'
    }
    for padrao, substituicao in abreviacoes.items():
        txt = re.sub(padrao, substituicao, txt, flags=re.IGNORECASE)
    
    txt = re.sub(r'\s+', ' ', txt)
    return txt

def extrair_dados_reais_google(origem_raw, destino_raw, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=True):
    """
    CAMADA BRUTA - Intercepta a API interna de direções do Google Maps.
    """
    if usar_coordenadas and lat_o and lon_o and lat_d and lon_d and lat_o != 0.0 and lat_d != 0.0:
        origem_param = f"{lat_o},{lon_o}"
        destino_param = f"{lat_d},{lon_d}"
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
            padroes_balsa = [r'\"utilizar\s+balsa\b', r'\"pegar\s+balsa\b', r'\"travessia\s+de\s+balsa\b']
            if any(re.search(padrao, texto_resposta.lower()) for padrao in padroes_balsa):
                envolve_balsa = "Sim"
                
            return km_puro, tempo_txt, link_maps,外 := envolve_balsa if '外' in locals() else envolve_balsa
            
    except Exception:
        pass
        
    return None

def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    """Cálculo local da Linha Reta Geodésica (Vincenty, 1975)"""
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
        return round((b * A * (sigma - deltaSigma)) / 1000, 2)
    except Exception:
        return 0.0

def camada_postal_redundante(cep_alvo):
    """CAMADA 3: RESOLUÇÃO POSTAL REDUNDANTE (ViaCEP + BrasilAPI)"""
    cep_limpo = re.sub(r'\D', '', str(cep_alvo))
    if len(cep_limpo) == 8:
        # Tentativa 1: ViaCEP
        try:
            res = requests.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=4).json()
            if "erro" not in res:
                return res.get('logradouro', ''), res.get('bairro', ''), res.get('localidade', ''), res.get('uf', '')
        except Exception:
            pass
        # Tentativa 2: BrasilAPI
        try:
            res = requests.get(f"https://brasilapi.com.br/api/cep/v1/{cep_limpo}", timeout=4).json()
            if "name" not in res:
                return res.get('street', ''), res.get('neighborhood', ''), res.get('city', ''), res.get('state', '')
        except Exception:
            pass
    return "", "", "", ""

def obter_coordenadas_e_endereco_oficial(localidade, uf_limite_obrigatorio=""):
    """
    CAMADA GEOGRÁFICA INTEROPERÁVEL - Geocodificação Dinâmica Multi-Provedor Sem Filtros Fixos.
    """
    texto_str = normalizar_texto_endereco(localidade)
    texto_upper = texto_str.upper()
    foi_resolvido_por_cep = False
    
    # Isola frações urbanas para reanexação lógica final (Camada 5)
    fracao_predial = ""
    match_fracao = re.search(r'\b(CONJUNTO|CJ|QUADRA|QD|BLOCO|BL|LOTE|LT|CHÁCARA|CHACARA|NÚMERO|Nº|NUMERO|N|CASA|AP|KM)\s*([A-Za-z0-9\/.]+)\b', texto_upper)
    if match_fracao and not re.match(r'^\d{5}-?\d{3}$', texto_str):
        fracao_predial = match_fracao.group(0)

    # 1. RESOLUÇÃO POSTAL COM REDUNDÂNCIA (Camada 3)
    match_cep = re.search(r'\b\d{5}-?\d{3}\b', texto_str)
    if match_cep or (len(re.sub(r'\D', '', texto_str)) == 8 and texto_str.isdigit()):
        cep_alvo = match_cep.group(0).replace("-", "") if match_cep else re.sub(r'\D', '', texto_str)
        logr, bair, loca, uf = camada_postal_redundante(cep_alvo)
        if loca:
            endereco_oficial_cep = ", ".join([c for c in [logr, fracao_predial if fracao_predial else "", bair, loca, uf] if c.strip()]) + ", Brasil"
            
            url_osm = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(endereco_oficial_cep)}&limit=1&countrycodes=br"
            headers_osm = {"User-Agent": "GerenciadorRotasAgnostico/6.0 (suporte_dados@logistica.com)"}
            try:
                res_osm = requests.get(url_osm, headers=headers_osm, timeout=4).json()
                if res_osm:
                    return float(res_osm[0]['lat']), float(res_osm[0]['lon']), endereco_oficial_cep, uf.upper()
            except Exception:
                pass
            return 0.0, 0.0, endereco_oficial_cep, uf.upper()

    # 2. GEOCODIFICAÇÃO MULTI-FONTE E ENRIQUECIMENTO (Camada 4, 6 e 8)
    query = f"{texto_str}, {uf_limite_obrigatorio}, Brasil" if uf_limite_obrigatorio and uf_limite_obrigatorio not in texto_upper else f"{texto_str}, Brasil" if "BRASIL" not in texto_upper else texto_str
    
    # Candidato Provedor A: ArcGIS Server REST
    url_arc = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=3&sourceCountry=BRA&outFields=*"
    try:
        resposta = requests.get(url_arc, timeout=6).json()
        if resposta.get('candidates'):
            candidato = resposta['candidates'][0]
            atributos = candidato.get('attributes', {})
            estado_arc = (atributos.get('RegionAbbr', '').strip() or atributos.get('Region', '').strip()).upper()
            
            if not uf_limite_obrigatorio or estado_arc == uf_limite_obrigatorio.upper():
                lat = float(candidato['location']['y'])
                lon = float(candidato['location']['x'])
                logradouro_arc = atributos.get('StAddr', '').strip()
                bairro_arc = atributos.get('Neighborhood', '').strip()
                cidade_arc = atributos.get('City', '').strip()
                
                if logradouro_arc and len(logradouro_arc.split()) > 1:
                    componentes_arc = [logradouro_arc, fracao_predial if fracao_predial else "", bairro_arc, cidade_arc, estado_arc]
                    endereco_completo = ", ".join([c for c in componentes_arc if c.strip()])
                    return lat, lon, f"{endereco_completo}, Brasil", estado_arc
                return lat, lon, f"{candidato['address']}, Brasil", estado_arc
    except Exception:
        pass

    # Candidato Provedor B: OpenStreetMap Nominatim Engine
    url_osm = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query)}&limit=2&addressdetails=1&countrycodes=br"
    headers_osm = {"User-Agent": "GerenciadorRotasAgnostico/6.0 (suporte_dados@logistica.com)"}
    try:
        res_osm = requests.get(url_osm, headers=headers_osm, timeout=6).json()
        if res_osm:
            opcao = res_osm[0]
            details = opcao.get('address', {})
            estado_osm = details.get('state', '').strip().upper()
            
            lat = float(opcao['lat'])
            lon = float(opcao['lon'])
            rua = details.get('road', details.get('pedestrian', ''))
            bair_o = details.get('neighbourhood', details.get('suburb', ''))
            cide_o = details.get('city', details.get('town', ''))
            
            componentes_osm = [texto_str if len(texto_str) > len(rua) else rua, fracao_predial if fracao_predial else "", bair_o, cide_o, details.get('state', '')]
            endereco_osm = ", ".join([c for c in componentes_osm if c.strip()]) + ", Brasil"
            return lat, lon, endereco_osm, estado_osm[:2]
    except Exception:
        pass
        
    return 0.0, 0.0, query, uf_limite_obrigatorio if uf_limite_obrigatorio else "ND"

def calcular_pipeline_logistico(origem, destino):
    """Pipeline central avançado 100% agnóstico com Filtro de Inconsistência Geodésica"""
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    # Pré-Varredura de Metadados Postais para extração de UF âncora explícita
    uf_ancora = ""
    cep_o_match = re.search(r'\b\d{5}-?\d{3}\b', origem_clean)
    cep_d_match = re.search(r'\b\d{5}-?\d{3}\b', destino_clean)
    
    if cep_o_match:
        _, _, _, uf_o = camada_postal_redundante(cep_o_match.group(0))
        if uf_o: uf_ancora = uf_o.upper()
    elif cep_d_match:
        _, _, _, uf_d = camada_postal_redundante(cep_d_match.group(0))
        if uf_d: uf_ancora = uf_d.upper()
        
    if not uf_ancora:
        match_sigla = re.search(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM|PA|MT|MS)\b', origem_clean.upper() + " " + destino_clean.upper())
        if match_sigla:
            uf_ancora = match_sigla.group(1)

    # Resolve os dados estruturados nas APIs geográficas
    lat_o, lon_o, origem_oficial, uf_final_o = obter_coordenadas_e_endereco_oficial(origem_clean, uf_ancora)
    lat_d, lon_d, destino_oficial, uf_final_d = obter_coordenadas_e_endereco_oficial(destino_clean, uf_ancora if uf_ancora else uf_final_o)
    
    # Camada 9: Computa o vetor analítico de linha reta teórica perfeito
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)
    
    query_o = origem_oficial
    query_d = destino_oficial
    
    usar_coords = True
    
    # --- CAMADA DE INTELIGÊNCIA GEOMÉTRICA CONTRA ALUCINAÇÕES (A Trava de Escala) ---
    # Se a linha reta calculada pelas APIs der maior que 150 km, mas o usuário NÃO especificou 
    # siglas de estados diferentes na planilha (indicando que deveria ser uma viagem curta local), 
    # o sistema detecta uma falha cadastral de homônimo. Coordenadas ruins são abortadas na hora.
    if dist_linha_reta > 150.0:
        siglas_encontradas = re.findall(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM|PA|MT|MS)\b', origem_clean.upper() + " " + destino_clean.upper())
        if len(set(siglas_encontradas)) <= 1:
            usar_coords = False # Desativa as coordenadas e força o roteamento por texto limpo no Google

    dados_reais = extrair_dados_reais_google(query_o, query_d, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=usar_coords)
    
    if dados_reais:
        km_google, tempo_google, link_google, balsa_google = dados_reais
        
        # Filtro de Contingência Viária pós-Google Maps
        if km_google > 150.0 and dist_linha_reta < 60.0 and usar_coords:
            dados_reais_seguros = extrair_dados_reais_google(query_o, query_d, 0.0, 0.0, 0.0, 0.0, usar_coordenadas=False)
            if dados_reais_seguros:
                return dados_reais_seguros[0], dados_reais_seguros[1], dados_reais_seguros[2], dados_reais_seguros[3], dist_linha_reta
                
        return km_google, tempo_google, link_google, balsa_google, dist_linha_reta

    # FALLBACK OPERACIONAL EM CASO DE ADVERSIDADE DE INTERNET
    link_maps_fallback = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(query_o)}&destination={requests.utils.quote(query_d)}&travelmode=driving"
    km_terrestre = round(dist_linha_reta * 1.27, 2) if dist_linha_reta > 0.0 else 0.0
    v_comercial = 65.0 if km_terrestre >= 150 else 45.0
    minutos = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0.0 else 0
    
    tempo_txt = f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min" if minutos % 60 > 0 else f"{minutos // 60} h"
    return km_terrestre, tempo_txt, link_maps_fallback, "Não", dist_linha_reta

# --- INTERFACE VISUAL NO STREAMLIT ---
st.title("🚗 Gerenciador de Rotas Inteligentes")
st.subheader("Engine de Resolução Espacial Agnóstica — Operação Gratuita")
st.write("Insira uma planilha Excel (.xlsx) contendo as colunas **Origem** e **Destino**.")

arquivo_carregado = st.file_uploader("Selecionar Arquivo Excel", type=["xlsx"])

if arquivo_carregado is not None:
    df = pd.read_excel(arquivo_carregado)
    
    if 'Origem' not in df.columns or 'Destino' not in df.columns:
        st.error("Erro de Validação: Certifique-se de que a planilha possui as colunas obrigatórias 'Origem' e 'Destino'.")
    else:
        st.success("Tabela de dados detectada com sucesso! Pronto para processar.")
        
        if st.button("Iniciar Processamento em Lote"):
            for col in ['Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta']:
                df[col] = None

            total_linhas = len(df)
            barra_progresso = st.progress(0)
            container_status = st.empty()
            
            for index, linha in df.iterrows():
                origem = str(linha['Origem']).strip()
                destino = str(linha['Destino']).strip()
                
                if origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan':
                    container_status.text(f"🔢 Processando linha {index + 1} de {total_linhas}: {origem} ➔ {destino}")
                    
                    km, tempo, link, balsa_status, linha_reta = calcular_pipeline_logistico(origem, destino)
                    
                    df.at[index, 'Distancia'] = km
                    df.at[index, 'Tempo'] = tempo
                    df.at[index, 'Link da Rota'] = link
                    df.at[index, 'Balsas'] = balsa_status
                    df.at[index, 'Linha Reta'] = linha_reta
                    
                    time.sleep(1.0)
                
                barra_progresso.progress((index + 1) / total_linhas)
            
            container_status.empty()
            barra_progresso.empty()
            st.success("✨ Processamento em lote concluído com sucesso!")
            
            ordem_finais = ['Origem', 'Destino', 'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta']
            for col_orig in df.columns:
                if col_orig not in ordem_finais:
                    ordem_finais.insert(0, col_orig)
            df = df.reindex(columns=ordem_colunas if 'ordem_colunas' in locals() else ordem_finais)
            
            output_buffer = io.BytesIO()
            with pd.ExcelWriter(output_buffer, engine='openpyxl') as writer:
                df.to_excel(writer, index=False)
            dados_excel = output_buffer.getvalue()
            
            st.write("---")
            st.balloons()
            
            st.download_button(
                label="📥 Baixar Planilha Logística Processada",
                data=dados_excel,
                file_name="planilha_rotas_calculada.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
