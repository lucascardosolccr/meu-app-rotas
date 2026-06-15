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
                
            return km_puro, tempo_txt, link_maps, envolve_balsa
            
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

def obter_coordenadas_e_endereco_oficial(localidade):
    """
    CAMADA GEOGRÁFICA INTEROPERÁVEL - Resolução direta e qualificação estruturada de strings.
    Se for CEP, usa a base postal puramente. Se for texto comum, normaliza gramaticalmente.
    """
    texto_str = str(localidade).strip()
    texto_upper = texto_str.upper()
    
    # 1. TRATAMENTO SOBERANO SE A ENTRADA JÁ FOR UM CEP PURO (ViaCEP)
    cep_limpo = re.sub(r'\D', '', texto_str)
    if len(cep_limpo) == 8 and (texto_str.isdigit() or "-" in texto_str or "CEP" in texto_upper):
        try:
            res_cep = requests.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=5).json()
            if "erro" not in res_cep:
                logradouro = res_cep.get('logradouro', '').strip()
                bairro = res_cep.get('bairro', '').strip()
                localidade_nome = res_cep.get('localidade', '').strip()
                uf = res_cep.get('uf', '').strip()
                
                if uf.upper() == "DF" and "ZONA INDUSTRIAL" in bairro.upper():
                    bairro = "SIG"
                
                componentes_cep = [logradouro, bairro, localidade_nome, uf]
                endereco_oficial_cep = ", ".join([c for c in componentes_cep if c]) + f", {cep_limpo}, Brasil"
                
                # Obtém coordenadas de suporte via OpenStreetMap para cálculo geodésico
                url_osm = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(endereco_oficial_cep)}&limit=1&countrycodes=br"
                headers_osm = {"User-Agent": "GerenciadorRotasInteligentes/4.0 (lucasccruz@gmail.com)"}
                res_osm = requests.get(url_osm, headers=headers_osm, timeout=5).json()
                lat, lon = (float(res_osm[0]['lat']), float(res_osm[0]['lon'])) if res_osm else (0.0, 0.0)
                
                return lat, lon, endereco_oficial_cep
        except Exception:
            pass

    # 2. SE FOR ENDEREÇO TEXTUAL COMUM: ENRIQUECIMENTO E STRIP SEMÂNTICO (OpenStreetMap Nominatim)
    # Proteção contextual: se identificar siglas típicas do DF mas faltar escopo, complementa programaticamente
    tokens_df = ["QR ", "QN ", "QS ", "QNL ", "QNJ ", "QNM ", "QNO ", "SAMAMBAIA", "CEILANDIA", "CEILÂNDIA", "TAGUATINGA", "UCB", "CATOLICA", "UNB", "UNICEUB", "CEUB"]
    sufixo_regional = ""
    if any(t in texto_upper for t in tokens_df) and "DF" not in texto_upper and "BRAS" not in texto_upper:
        sufixo_regional = ", Brasília, DF"

    query = f"{texto_str}{sufixo_regional}, Brasil" if "BRASIL" not in texto_upper else texto_str
    
    # Consulta direta no OpenStreetMap travando o país (countrycodes=br) para colher lat/lon estáveis
    url_osm = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query)}&limit=1&addressdetails=1&countrycodes=br"
    headers_osm = {"User-Agent": "GerenciadorRotasInteligentes/4.0 (lucasccruz@gmail.com)"}
    
    try:
        resposta = requests.get(url_osm, headers=headers_osm, timeout=6).json()
        if resposta:
            opcao = resposta[0]
            lat = float(opcao['lat'])
            lon = float(opcao['lon'])
            
            # Reconstrói a string de busca enriquecida com o bairro, cidade e estado reais retornados pela malha geográfica
            details = opcao.get('address', {})
            rua = details.get('road', details.get('pedestrian', '')).strip()
            bairro = details.get('neighbourhood', details.get('suburb', details.get('city_district', ''))).strip()
            cidade = details.get('city', details.get('town', details.get('municipality', ''))).strip()
            estado = details.get('state', '').strip()
            
            # Se a rua mapeada for condizente, monta a assinatura rica de texto que o Google Maps adora receber
            if rua and len(rua.split()) > 1:
                componentes_osm = [texto_str if len(texto_str) > len(rua) else rua, bairro, cidade, estado]
                endereco_estruturado = ", ".join([c for c in componentes_osm if c]) + ", Brasil"
                return lat, lon, endereco_estruturado
                
            return lat, lon, f"{opcao['display_name']}"
    except Exception:
        pass
        
    return 0.0, 0.0, query

def calcular_pipeline_logistico(origem, destino):
    """Pipeline focado em estabilidade de strings tratadas de alta prioridade"""
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    # Resolve as strings qualificadas nas APIs geográficas
    lat_o, lon_o, origem_oficial = obter_coordenadas_e_endereco_oficial(origem_clean)
    lat_d, lon_d, destino_oficial = obter_coordenadas_e_endereco_oficial(destino_clean)
    
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)

    # Executa a chamada forçando TEXTO (usar_coordenadas=False) para pontos institucionais complexos ou
    # usando COORDENADAS se os dois pontos forem mapeados com segurança matemática perfeita em linha reta curta
    usar_coords = True if (lat_o != 0.0 and lat_d != 0.0 and dist_linha_reta < 100.0) else False
    
    dados_reais = extrair_dados_reais_google(origem_oficial, destino_oficial, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=usar_coords)
    
    if dados_reais:
        km_google, tempo_google, link_google, balsa_google = dados_reais
        
        # --- FILTRO CONTRA DEFEITO DE COORDENADAS INTERESTADUAIS ---
        # Se a rota rodoviária explodir de forma absurda em relação à linha reta, roda o plano de evacuação textual pura
        if km_google > 120.0 and dist_linha_reta < 40.0:
            dados_reais_seguros = extrair_dados_reais_google(origem_oficial, destino_oficial, 0.0, 0.0, 0.0, 0.0, usar_coordenadas=False)
            if dados_reais_seguros:
                return dados_reais_seguros[0], dados_reais_seguros[1], dados_reais_seguros[2], dados_reais_seguros[3], dist_linha_reta
                
        return km_google, tempo_google, link_google, balsa_google, dist_linha_reta

    # CONTINGÊNCIA EM CASO DE TIMEOUT DA API INTERNA
    link_maps_fallback = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(origem_oficial)}&destination={requests.utils.quote(destino_oficial)}&travelmode=driving"
    km_terrestre = round(dist_linha_reta * 1.27, 2) if dist_linha_reta > 0.0 else 0.0
    minutos = round((km_terrestre / 45.0) * 60) if km_terrestre > 0.0 else 0
    tempo_txt = f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min"
    return km_terrestre, tempo_txt, link_maps_fallback, "Não", dist_linha_reta

# --- INTERFACE VISUAL NO STREAMLIT ---
st.title("🚗 Gerenciador de Rotas Inteligentes")
st.subheader("Engine de Alta Fidelidade Textual e Logística — Operação Gratuita")
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
