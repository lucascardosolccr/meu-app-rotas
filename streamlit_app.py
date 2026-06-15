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
    if usar_coordenadas and lat_o and lon_o and lat_d and lon_d:
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
        texto_resposta = response_txt = resposta.text
        
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
                r'\"utilizar\s+balsa\b', 
                r'\"pegar\s+balsa\b', 
                r'\"travessia\s+de\s+balsa\b', 
                r'\"balsa\s+de\s+veículos\b',
                r'\"ferry\b',
                r'\"travessia\s+por\s+balsa\b'
            ]
            
            if any(re.search(padrao, texto_resposta.lower()) for padrao in padroes_balsa):
                envolve_balsa = "Sim"
                
            return km_puro, tempo_txt, link_maps, envolve_balsa
            
    except Exception:
        pass
        
    return None

def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    """Cálculo local da Linha Reta Geodésica (Vincenty, 1975)"""
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

def buscar_via_cep(cep):
    """Busca estruturada e imutável na API nacional aberta dos Correios (ViaCEP)"""
    cep_limpo = re.sub(r'\D', '', str(cep))
    if len(cep_limpo) == 8:
        try:
            res = requests.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=5).json()
            if "erro" not in res:
                logradouro = res.get('logradouro', '').strip()
                bairro = res.get('bairro', '').strip()
                localidade = res.get('localidade', '').strip()
                uf = res.get('uf', '').strip()
                
                if uf.upper() == "DF" and "ZONA INDUSTRIAL" in bairro.upper():
                    bairro = "SIG"
                
                componentes = [logradouro, bairro, localidade, uf]
                endereco_formatado = ", ".join([c for c in componentes if c])
                return endereco_formatado + f", {res.get('cep')}"
        except Exception:
            pass
    return None

def obter_coordenadas_e_endereco_oficial(localidade):
    """
    CAMADA GEOGRÁFICA DE ALTA PRECISÃO - Executa o Pipeline de Desambiguação Agnóstico Nacional
    com proteção estrita contra desvios de coordenadas inter-regionais.
    """
    texto_str = str(localidade).strip()
    texto_upper = texto_str.upper()
    
    # Captura numeração predial ou codificação urbana latente
    numero_predial = ""
    match_num = re.search(r'(?:NÚMERO|Nº|N|NUMERO|LOTE|QD|Q|CONJUNTO|CJ)\s*([A-Za-z0-9\/]+)\b', texto_upper)
    if match_num and not re.match(r'^\d{5}-?\d{3}$', texto_str):
        numero_predial = match_num.group(0)

    # 1. RESOLUÇÃO POSTAL PURA (Se for CEP de largada)
    cep_limpo = re.sub(r'\D', '', texto_str)
    if len(cep_limpo) == 8 and (texto_str.isdigit() or "-" in texto_str or "CEP" in texto_upper):
        endereco_via_cep = buscar_via_cep(cep_limpo)
        if endereco_via_cep:
            url_arc = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(endereco_via_cep)}&maxLocations=1&sourceCountry=BRA"
            try:
                res_arc = requests.get(url_arc, timeout=5).json()
                if res_arc.get('candidates'):
                    loc = res_arc['candidates'][0]['location']
                    return float(loc['y']), float(loc['x']), endereco_via_cep
            except Exception:
                pass
            return 0.0, 0.0, endereco_via_cep

    # 2. INJEÇÃO CONTEXTUAL ADAPTATIVA (Proteção estrita contra alucinações interestaduais no DF)
    tokens_df = ["QR ", "QN ", "QS ", "QNL ", "QNJ ", "QNM ", "QNO ", "SAMAMBAIA", "CEILANDIA", "CEILÂNDIA", "TAGUATINGA", "UCB", "CATOLICA", "UNB"]
    sufixo_df = ""
    if any(t in texto_upper for t in tokens_df) and "DF" not in texto_upper and "BRAS" not in texto_upper:
        sufixo_df = ", Distrito Federal, Brasil"

    query = f"{texto_str}{sufixo_df}" if "BRASIL" not in texto_upper else texto_str
    url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=10&sourceCountry=BRA&outFields=*"
    
    try:
        resposta = requests.get(url, timeout=10).json()
        if resposta.get('candidates'):
            # Varredura inteligente de múltiplos candidatos para validação cruzada postal
            for candidato in resposta['candidates']:
                lat = float(candidato['location']['y'])
                lon = float(candidato['location']['x'])
                
                atributos = candidato.get('attributes', {})
                cep_identificado = atributos.get('Postal', '').strip() or atributos.get('PostalExt', '').strip()
                cep_identificado_limpo = re.sub(r'\D', '', cep_identificado)
                
                if len(cep_identificado_limpo) == 8:
                    endereco_oficial_correios = buscar_via_cep(cep_identificado_limpo)
                    if endereco_oficial_correios:
                        # Re-acopla a identificação de quadra/conjunto se o usuário tiver preenchido
                        if numero_predial and numero_predial.upper() not in endereco_oficial_correios.upper():
                            partes = endereco_oficial_correios.split(', ', 1)
                            if len(partes) > 1:
                                endereco_oficial_correios = f"{partes[0]} {numero_predial}, {partes[1]}"
                        return lat, lon, endereco_oficial_correios

            # FALLBACK DE SEGURANÇA ESTRUTURADO (Se falhar o cruzamento reverso por CEP)
            primeiro = resposta['candidates'][0]
            lat = float(primeiro['location']['y'])
            lon = float(primeiro['location']['x'])
            attrs = primeiro.get('attributes', {})
            logradouro_arc = attrs.get('StAddr', '').strip()
            bairro_arc = attrs.get('Neighborhood', '').strip()
            cidade_arc = attrs.get('City', '').strip()
            estado_arc = attrs.get('RegionAbbr', '').strip() or attrs.get('Region', '').strip()
            
            if logradouro_arc and len(logradouro_arc.split()) > 1:
                componentes = [logradouro_arc, bairro_arc, city_arc := cidade_arc if cidade_arc else "Brasília", estado_arc if estado_arc else "DF"]
                return lat, lon, ", ".join([c for c in componentes if c])
            
            return lat, lon, primeiro['address']
    except Exception:
        pass
        
    return 0.0, 0.0, texto_str

def calcular_pipeline_logistico(origem, destino):
    """Pipeline central avançado com injeção contextual de strings e coordenadas"""
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    dados_geo_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    dados_geo_d = obter_coordenadas_e_endereco_oficial(destino_clean)
    
    lat_o, lon_o, origem_oficial = dados_geo_o if dados_geo_o else (0.0, 0.0, origem_clean)
    lat_d, lon_d, destino_oficial = dados_geo_d if dados_geo_d else (0.0, 0.0, destino_clean)
    
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d) if (lat_o != 0.0 and lat_d != 0.0) else 0.0

    # MECANISMO DE TRAVA DE SEGURANÇA ANTIALUCINAÇÃO:
    # Se a distância em linha reta der absurdamente maior que 180km para trechos urbanos presumidos,
    # desativa o envio de coordenadas corrompidas e força o Google Maps a achar textualmente na malha local.
    usar_coords = True if (lat_o != 0.0 and lat_d != 0.0 and dist_linha_reta < 180.0) else False
    
    query_o = origem_oficial
    query_d = destino_oficial

    dados_reais = extrair_dados_reais_google(query_o, query_d, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=usar_coords)
    
    if dados_reais:
        km_google, tempo_google, link_google, balsa_google = dados_reais
        return km_google, tempo_google, link_google, balsa_google, dist_linha_reta

    # FALLBACK OPERACIONAL
    link_maps_fallback = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(query_o)}&destination={requests.utils.quote(query_d)}&travelmode=driving"
    km_terrestre = round(dist_linha_reta * 1.27, 2) if dist_linha_reta > 0.0 else 0.0
    v_comercial = 65.0 if km_terrestre >= 150 else 45.0
    minutos = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0.0 else 0
    
    balsa_fallback = "Não"
    tempo_txt = f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min" if minutos % 60 > 0 else f"{minutos // 60} h"
    return km_terrestre, tempo_txt, link_maps_fallback, balsa_fallback, dist_linha_reta

# --- INTERFACE VISUAL NO STREAMLIT ---
st.title("🚗 Gerenciador de Rotas Inteligentes")
st.subheader("Engine de Interceptação de API Viva — Operação Gratuita")
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
                    
                    time.sleep(0.8)
                
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
