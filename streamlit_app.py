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
                
            return km_puro, tempo_txt, link_maps, Black := envolve_balsa if 'Black' in locals() else envolve_balsa
            
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

def extrair_metadados_via_cep(cep_alvo):
    """Varre a API estruturada dos Correios para extrair cidade e estado de suporte"""
    cep_limpo = re.sub(r'\D', '', str(cep_alvo))
    if len(cep_limpo) == 8:
        try:
            res = requests.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=5).json()
            if "erro" not in res:
                return res.get('logradouro', ''), res.get('bairro', ''), res.get('localidade', ''), res.get('uf', '')
        except Exception:
            pass
    return "", "", "", ""

def obter_coordenadas_e_endereco_oficial(localidade, uf_limite_obrigatorio=""):
    """
    CAMADA GEOGRÁFICA INTEROPERÁVEL UNIVERSAL - Redundância Híbrida Inteligente.
    Filtra candidatos com base na coerência de estado para erradicar desvios interestaduais.
    """
    texto_str = str(localidade).strip()
    texto_upper = texto_str.upper()
    
    # 1. PROCESSAMENTO LOGÍSTICO DE CEP DIRECT (ViaCEP)
    cep_limpo = re.sub(r'\D', '', texto_str)
    if len(cep_limpo) == 8 and (texto_str.isdigit() or "-" in texto_str or "CEP" in texto_upper):
        logr, bair, loca, uf = extrair_metadados_via_cep(cep_limpo)
        if loca:
            if uf.upper() == "DF" and "ZONA INDUSTRIAL" in bair.upper():
                bair = "SIG"
            componentes = [logr, bair, loca, uf]
            endereco_oficial_cep = ", ".join([c for c in componentes if c]) + f", {cep_limpo}"
            
            url_arc = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(endereco_oficial_cep + ', Brasil')}&maxLocations=1&sourceCountry=BRA"
            try:
                res_arc = requests.get(url_arc, timeout=5).json()
                if res_arc.get('candidates'):
                    pt = res_arc['candidates'][0]['location']
                    return float(pt['y']), float(pt['x']), endereco_oficial_cep, uf.upper()
            except Exception:
                pass
            return 0.0, 0.0, endereco_oficial_cep, uf.upper()

    # 2. CONSTRUÇÃO DA QUERY COM AMARRAÇÃO DE UF SE DISPONÍVEL
    query = f"{texto_str}, Brasil"
    if uf_limite_obrigatorio and uf_limite_obrigatorio not in texto_upper:
        query = f"{texto_str}, {uf_limite_obrigatorio}, Brasil"
    elif "BRASIL" not in texto_upper:
        query = f"{texto_str}, Brasil"
    
    # --- PROVEDOR PROPRIETÁRIO (ArcGIS Server REST) ---
    url_arc = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=8&sourceCountry=BRA&outFields=*"
    try:
        res_arc = requests.get(url_arc, timeout=8).json()
        if res_arc.get('candidates'):
            # Varre os candidatos e força a validação cruzada do Estado encontrado
            for candidato in res_arc['candidates']:
                attrs = candidato.get('attributes', {})
                esta_a = attrs.get('RegionAbbr', '').strip() or attrs.get('Region', '').strip()
                
                # Se houver uma trava de UF vinda da mesma linha, rejeita candidatos de outros estados (Ex: Rejeita AM se a origem for DF)
                if uf_limite_obrigatorio and esta_a.upper() != uf_limite_obrigatorio.upper():
                    continue
                    
                lat = float(candidato['location']['y'])
                lon = float(candidato['location']['x'])
                logr_a = attrs.get('StAddr', '').strip()
                bair_a = attrs.get('Neighborhood', '').strip()
                cide_a = attrs.get('City', '').strip()
                
                if logr_a and len(logr_a.split()) > 1:
                    componentes_arc = [logr_a, bair_a, cide_a, esta_a]
                    return lat, lon, ", ".join([c for c in componentes_arc if c]), esta_a.upper()
                return lat, lon, candidato['address'], esta_a.upper()
    except Exception:
        pass

    # --- PROVEDOR COLABORATIVO (OpenStreetMap Nominatim) ---
    url_osm = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query)}&limit=3&addressdetails=1"
    headers_osm = {"User-Agent": "GerenciadorRotasUniversais/4.0 (lucasccruz@gmail.com)"}
    try:
        res_osm = requests.get(url_osm, headers=headers_osm, timeout=6).json()
        if res_osm:
            for opcao in res_osm:
                details = opcao.get('address', {})
                esta_o = details.get('state', '').strip()
                
                # Tradução de estados longos para siglas comuns se necessário
                if uf_limite_obrigatorio and uf_limite_obrigatorio.upper() == "DF" and "DISTRITO FEDERAL" not in esta_o.upper():
                    continue
                
                lat = float(opcao['lat'])
                lon = float(opcao['lon'])
                rua = details.get('road', details.get('pedestrian', ''))
                bair_o = details.get('neighbourhood', details.get('suburb', ''))
                cide_o = details.get('city', details.get('town', ''))
                
                componentes_osm = [rua, bair_o, cide_o, esta_o]
                endereco_osm = ", ".join([c for c in componentes_osm if c])
                return lat, lon, endereco_osm, uf_limite_obrigatorio if uf_limite_obrigatorio else "OSM"
    except Exception:
        pass
        
    return 0.0, 0.0, query, uf_limite_obrigatorio

def calcular_pipeline_logistico(origem, destino):
    """Pipeline Universal de Roteamento Sem Amarras Fixas com Fusão de Contexto de Linha"""
    origem_clean = str(origem).strip()
    num_destino = str(destino).strip()
    
    # ETAPA 1: Pré-Varredura de Metadados Postais da Linha para extrair a UF âncora da operação
    uf_ancora = ""
    cep_o_match = re.search(r'\b\d{5}-?\d{3}\b', origem_clean)
    cep_d_match = re.search(r'\b\d{5}-?\d{3}\b', num_destino)
    
    if cep_o_match:
        _, _, _, uf_o = extrair_metadados_via_cep(cep_o_match.group(0))
        if uf_o: uf_ancora = uf_o.upper()
    elif cep_d_match:
        _, _, _, uf_d = extrair_metadados_via_cep(cep_d_match.group(0))
        if uf_d: uf_ancora = uf_d.upper()
        
    # Se não houver CEP na linha, procura siglas de estados explícitas escritas pelo usuário (Ex: "DF", "SP", "GO")
    if not uf_ancora:
        match_sigla = re.search(r'\b(DF|GO|SP|RJ|MG|BA|PR|SC|RS|CE|PE|AM)\b', origem_clean.upper() + " " + num_destino.upper())
        if match_sigla:
            uf_ancora = match_sigla.group(1)

    # ETAPA 2: Resolve a localização aplicando a trava da UF âncora encontrada na mesma linha
    lat_o, lon_o, origem_oficial, uf_final_o = obter_coordenadas_e_endereco_oficial(origem_clean, uf_ancora)
    lat_d, lon_d, destino_oficial, uf_final_d = obter_coordenadas_e_endereco_oficial(num_destino, uf_ancora if uf_ancora else uf_final_o)
    
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)

    # Se a linha reta der coerente (< 120km), confia na amarração das coordenadas exatas de pino
    usar_coords = True if (lat_o != 0.0 and lat_d != 0.0 and dist_linha_reta < 120.0) else False
    
    dados_reais = extrair_dados_reais_google(origem_oficial, destino_oficial, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=usar_coords)
    
    if dados_reais:
        km_google, tempo_google, link_google, balsa_google = dados_reais
        
        # --- FILTRO DE CONTENÇÃO CONTRA ALUCINAÇÕES INTERESTADUAIS (Auditoria Vincenty) ---
        # Se a distância calculada pelo Google Maps estourar de forma absurda em relação à linha reta urbana curta,
        # o sistema detecta a quebra lógica, desativa as coordenadas ruins e força a busca pura por texto limpo regionalizado.
        if km_google > 120.0 and dist_linha_reta < 45.0:
            dados_reais_seguros = extrair_dados_reais_google(origem_oficial, destino_oficial, 0.0, 0.0, 0.0, 0.0, usar_coordenadas=False)
            if dados_reais_seguros:
                return dados_reais_seguros[0], dados_reais_seguros[1], dados_reais_seguros[2], dados_reais_seguros[3], dist_linha_reta
                
        return km_google, tempo_google, link_google, balsa_google, dist_linha_reta

    link_maps_fallback = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(origem_oficial)}&destination={requests.utils.quote(destino_oficial)}&travelmode=driving"
    km_terrestre = round(dist_linha_reta * 1.27, 2) if dist_linha_reta > 0.0 else 0.0
    minutos = round((km_terrestre / 45.0) * 60) if km_terrestre > 0.0 else 0
    tempo_txt = f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min"
    return km_terrestre, tempo_txt, link_maps_fallback, "Não", dist_linha_reta

# --- INTERFACE VISUAL NO STREAMLIT ---
st.title("🚗 Gerenciador de Rotas Inteligentes")
st.subheader("Engine Logística Dinâmica Nacional — Operação Gratuita")
st.write("Insira uma planilha Excel (.xlsx) contendo as colunas **Origem** e **Destino**.")

arquivo_carregado = st.file_uploader("Selecionar Arquivo Excel", type=["xlsx"])

if arquivo_carregado is not None:
    df = pd.read_excel(arquivo_carregado)
    
    if 'Origem' not in df.columns or 'Destino' not in df.columns:
        st.error("Erro de Validação: Planilha precisa conter as colunas 'Origem' e 'Destino'.")
    else:
        st.success("Tabela carregada e validada com sucesso.")
        
        if st.button("Iniciar Processamento Universal"):
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
            df = df.reindex(columns=ordem_finais)
            
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
