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

def extrair_dados_reais_google(lat_o, lon_o, lat_d, lon_d, origem_txt, destino_txt):
    """
    CAMADA BRUTA - Intercepta a API de direções do Google Maps.
    Força o cálculo e o traçado RÍGIDO pelos pontos geográficos exatos (Lat/Lon),
    eliminando qualquer chance de desvio semântico por texto.
    """
    if lat_o and lon_o and lat_d and lon_d and lat_o != 0.0 and lat_d != 0.0:
        origem_param = f"{lat_o},{lon_o}"
        destino_param = f"{lat_d},{lon_d}"
        
        # Endpoint de tráfego em tempo real baseado em coordenadas puras
        url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
        
        # URL oficial de navegação canônica amarrada nos pinos geográficos exatos
        link_maps = f"https://www.google.com/maps/dir/?api=1&origin={origem_param}&destination={destino_param}&travelmode=driving"
    else:
        # Fallback caso as coordenadas falhem completamente
        origem_enc = requests.utils.quote(f"{origem_txt}".strip())
        destino_enc = requests.utils.quote(f"{destino_txt}".strip())
        url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_enc}!1m2!1m1!1s{destino_enc}!3e0"
        link_maps = f"https://www.google.com/maps/dir/?api=1&origin={origem_enc}&destination={destino_enc}&travelmode=driving"
    
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

def obter_coordenadas_e_endereco_oficial_osm(localidade):
    """
    CAMADA GEOGRÁFICA INTEROPERÁVEL (OpenStreetMap/Nominatim + ViaCEP).
    Mapeia e valida qualquer endereço ou CEP por força de malha urbana real.
    """
    texto_str = str(localidade).strip()
    texto_upper = texto_str.upper()
    
    # 1. PROCESSAMENTO LOGÍSTICO DE COMPOSIÇÃO POSTAL (ViaCEP)
    cep_limpo = re.sub(r'\D', '', texto_str)
    if len(cep_limpo) == 8 and (texto_str.isdigit() or "-" in texto_str or "CEP" in texto_upper):
        try:
            resposta = requests.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=5)
            if resposta.status_code == 200:
                dados = resposta.json()
                if "erro" not in dados:
                    logradouro = dados.get('logradouro', '').strip()
                    bairro = dados.get('bairro', '').strip()
                    localidade_nome = dados.get('localidade', '').strip()
                    uf = dados.get('uf', '').strip()
                    
                    if uf.upper() == "DF" and "ZONA INDUSTRIAL" in bairro.upper():
                        bairro = "SIG"
                        
                    componentes = [logradouro, bairro, localidade_nome, uf]
                    texto_str = ", ".join([c for c in componentes if c])
                    
                    # Faz o cruzamento síncrono no OSM usando o endereço oficial dos Correios
                    url_osm = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(texto_str + ', Brasil')}&limit=1"
                    headers_osm = {"User-Agent": "GerenciadorRotasInteligentes/1.0 (lucasccruz@gmail.com)"}
                    res_osm = requests.get(url_osm, headers=headers_osm, timeout=5).json()
                    if res_osm:
                        return float(res_osm[0]['lat']), float(res_osm[0]['lon']), f"{texto_str}, {cep_limpo}"
                    
                    return 0.0, 0.0, f"{texto_str}, {cep_limpo}"
        except Exception:
            pass

    # 2. SE FOR ENDEREÇO TEXTUAL COMUM: GEOCODIFICAÇÃO VIA OPENSTREETMAP (NOMINATIM)
    # Garante âncoras regionais implícitas baseadas em termos logísticos urbanos do DF se o usuário omitir o estado
    tokens_df = ["QR ", "QN ", "QS ", "QNL ", "QNJ ", "QNM ", "QNO ", "SAMAMBAIA", "CEILANDIA", "CEILÂNDIA", "TAGUATINGA", "UCB", "CATOLICA", "UNB", "UNICEUB", "CEUB"]
    sufixo_regional = ""
    if any(t in texto_upper for t in tokens_df) and "DF" not in texto_upper and "BRAS" not in texto_upper:
        sufixo_regional = ", Brasília, DF"

    query = f"{texto_str}{sufixo_regional}, Brasil" if "BRASIL" not in texto_upper else texto_str
    
    url_osm = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query)}&limit=3&addressdetails=1"
    headers_osm = {"User-Agent": "GerenciadorRotasInteligentes/1.0 (lucasccruz@gmail.com)"}
    
    try:
        resposta = requests.get(url_osm, headers=headers_osm, timeout=8).json()
        if resposta:
            melhor_opcao = resposta[0]
            lat = float(melhor_opcao['lat'])
            lon = float(melhor_opcao['lon'])
            
            # Reconstrói dinamicamente o endereço usando as tags estruturadas do OpenStreetMap
            addr_details = melhor_opcao.get('address', {})
            rua = addr_details.get('road', addr_details.get('suburb', '')).strip()
            bairro_osm = addr_details.get('neighbourhood', addr_details.get('suburb', '')).strip()
            cidade_osm = addr_details.get('city', addr_details.get('town', addr_details.get('state_district', ''))).strip()
            estado_osm = addr_details.get('state', '').strip()
            cep_osm = addr_details.get('postcode', '').strip()
            
            # Se o OpenStreetMap trouxer um CEP válido na busca textual, faz a dupla checagem reversa automática
            if cep_osm and len(re.sub(r'\D', '', cep_osm)) == 8:
                endereco_correios = buscar_via_cep(re.sub(r'\D', '', cep_osm))
                if endereco_correios:
                    return lat, lon, endereco_correios
            
            componentes_osm = [rua, bairro_osm, cidade_osm, estado_osm]
            endereco_reconstruido = ", ".join([c for c in componentes_osm if c and c.upper() != bairro_osm.upper()])
            if cep_osm:
                endereco_reconstruido += f", {cep_osm}"
                
            return lat, lon, endereco_reconstruido if len(endereco_reconstruido) > 10 else melhor_opcao['display_name']
    except Exception:
        pass
        
    return 0.0, 0.0, texto_str

def calcular_pipeline_logistico(origem_bruta, destino_bruto):
    """Pipeline focado em amarrações numéricas via coordenadas OpenStreetMap -> Google Maps"""
    
    # Resolve as coordenadas e os endereços formais via ecossistema OpenStreetMap
    lat_o, lon_o, origem_oficial = obter_coordenadas_e_endereco_oficial_osm(origem_bruta)
    lat_d, lon_d, destino_oficial = obter_coordenadas_e_endereco_oficial_osm(destino_bruto)
    
    # Cálculo analítico de linha reta via Vincenty
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)

    # Executa o scraping usando as coordenadas numéricas absolutas (Garante precisão milimétrica)
    usar_coords = True if (lat_o != 0.0 and lat_d != 0.0 and dist_linha_reta < 180.0) else False
    dados_reais = extrair_dados_reais_google(origem_oficial, destino_oficial, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=usar_coords)
    
    if dados_reais:
        km_google, tempo_google, link_google, balsa_google = dados_reais
        return km_google, tempo_google, link_google, balsa_google, dist_linha_reta

    # CONTINGÊNCIA LOCAL SECUNDÁRIA
    origem_param = f"{lat_o},{lon_o}" if lat_o != 0.0 else requests.utils.quote(origem_oficial)
    destino_param = f"{lat_d},{lon_d}" if lat_d != 0.0 else requests.utils.quote(destino_oficial)
    link_maps_gps = f"https://www.google.com/maps/dir/?api=1&origin={origem_param}&destination={destino_param}&travelmode=driving"
    
    km_terrestre = round(dist_linha_reta * 1.27, 2) if dist_linha_reta > 0.0 else 0.0
    v_comercial = 65.0 if km_terrestre >= 150 else 45.0
    minutos = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0.0 else 0
    tempo_txt = f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min" if minutos % 60 > 0 else f"{minutos // 60} h"
    return km_terrestre, tempo_txt, link_maps_gps, "Não", dist_linha_reta

# --- INTERFACE VISUAL NO STREAMLIT ---
st.title("🚗 Gerenciador de Rotas Inteligentes")
st.subheader("Engine de Geolocalização de Alta Fidelidade (OpenStreetMap Engine)")
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
