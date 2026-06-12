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

def extrair_dados_reais_google(origem_oficial, destino_oficial, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=True):
    """
    CAMADA BRUTA - Intercepta a API interna de direções do Google Maps.
    Utiliza as strings ricas e oficializadas para garantir indexação estável no mapa.
    """
    if usar_coordenadas and lat_o and lon_o and lat_d and lon_d:
        origem_param = f"{lat_o},{lon_o}"
        destino_param = f"{lat_d},{lon_d}"
        url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
    else:
        origem_param = requests.utils.quote(f"{origem_oficial}".strip())
        destino_param = requests.utils.quote(f"{destino_oficial}".strip())
        url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
    
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
                
            return km_puro, tempo_txt, envolve_balsa
            
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

def geocodificar_e_padronizar_universal(localidade_raw):
    """
    CAMADA GEOGRÁFICA UNIVERSAL - Converte a entrada informal ou postal
    no endereço completo e estruturado da malha cartográfica planetária.
    """
    texto_str = str(localidade_raw).strip()
    
    # 1. VALIDAÇÃO POSTAL PRIORITÁRIA (ViaCEP)
    cep_limpo = re.sub(r'\D', '', texto_str)
    if len(cep_limpo) == 8 and (texto_str.isdigit() or "-" in texto_str):
        try:
            res_cep = requests.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=5).json()
            if "erro" not in res_cep:
                logr = res_cep.get('logradouro', '').strip()
                bair = res_cep.get('bairro', '').strip()
                loca = res_cep.get('localidade', '').strip()
                uf_c = res_cep.get('uf', '').strip()
                cep_c = res_cep.get('cep', '').strip()
                
                # Reconstrói a string com a base postal intocável dos Correios
                componentes_cep = [logr, bair, loca, uf_c]
                texto_str = ", ".join([c for c in componentes_cep if c])
                if cep_c:
                    texto_str += f", {cep_c}"
                
                # Se for CEP, o endereço gerado aqui é soberano e retorna imediatamente com coordenadas
                query_cep = f"{texto_str}, Brasil"
                url_cep = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query_cep)}&maxLocations=1&sourceCountry=BRA"
                resposta_cep = requests.get(url_cep, timeout=10).json()
                if resposta_cep.get('candidates'):
                    pt = resposta_cep['candidates'][0]['location']
                    return float(pt['y']), float(pt['x']), texto_str
        except Exception:
            pass

    # 2. SE NÃO FOR CEP: RESOLUÇÃO POR ATRIBUTOS ESTENDIDOS NO ARCGIS
    query = texto_str if "BRASIL" in texto_str.upper() else f"{texto_str}, Brasil"
    url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=3&sourceCountry=BRA&outFields=*"
    
    try:
        resposta = requests.get(url, timeout=10).json()
        if resposta.get('candidates'):
            melhor_candidato = resposta['candidates'][0]
            lat = float(melhor_candidato['location']['y'])
            lon = float(melhor_candidato['location']['x'])
            
            atributos = melhor_candidato.get('attributes', {})
            logradouro_arc = atributos.get('StAddr', '').strip()
            bairro_arc = atributos.get('Neighborhood', '').strip()
            cidade_arc = atributos.get('City', '').strip()
            estado_arc = atributos.get('RegionAbbr', '').strip() or atributos.get('Region', '').strip()
            cep_postal_arc = atributos.get('Postal', '').strip()
            
            # Se possuir logradouro rico, monta o endereço detalhado completo
            if logradouro_arc and len(logradouro_arc.split()) > 1:
                componentes = [logradouro_arc, bairro_arc, cidade_arc, estado_arc]
                endereco_reconstruido = ", ".join([c for c in componentes if c])
                if cep_postal_arc:
                    endereco_reconstruido += f", {cep_postal_arc}"
                return lat, lon, endereco_reconstruido
            
            return lat, lon, melhor_candidato['address']
    except Exception:
        pass
        
    return 0.0, 0.0, texto_str

def calcular_pipeline_logistico(origem_bruta, destino_bruto):
    """Pipeline logístico agnóstico baseado em normalização de metadados cartográficos"""
    lat_o, lon_o, origem_oficial = geocodificar_e_padronizar_universal(origem_bruta)
    lat_d, lon_d, destino_oficial = geocodificar_e_padronizar_universal(destino_bruto)
    
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d) if (lat_o != 0.0 and lat_d != 0.0) else 0.0

    origem_is_poi = any(k in origem_oficial.upper() for k in ["UNIVERSIDADE", "FACULDADE", "CAMPUS", "HOSPITAL", "SHOPPING", "AEROPORTO"])
    destino_is_poi = any(k in destino_oficial.upper() for k in ["UNIVERSIDADE", "FACULDADE", "CAMPUS", "HOSPITAL", "SHOPPING", "AEROPORTO"])
    usar_coords = not (origem_is_poi or destino_is_poi)

    dados_reais = extrair_dados_reais_google(origem_oficial, destino_oficial, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=usar_coords)
    
    # LINKS DE DIRETRIZES CANÔNICAS (Garante a plotagem exata do trajeto com endereços completos)
    link_maps_canonico = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(origem_oficial)}&destination={requests.utils.quote(destino_oficial)}&travelmode=driving"
    
    if dados_reais and isinstance(dados_reais, tuple):
        km_google, tempo_google, balsa_google = dados_reais[0], dados_reais[1], dados_reais[2]
        return km_google, tempo_google, link_maps_canonico, balsa_google, dist_linha_reta

    km_terrestre = round(dist_linha_reta * 1.27, 2) if dist_linha_reta > 0.0 else 0.0
    v_comercial = 65.0 if km_terrestre >= 150 else 45.0
    minutos = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0.0 else 0
    
    tempo_txt = f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min" if minutos % 60 > 0 else f"{minutos // 60} h"
    return km_terrestre, tempo_txt, link_maps_canonico, "Não", dist_linha_reta

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
            
            # --- SEÇÃO DE AUDITORIA ---
            st.write("---")
            st.subheader("📘 Documentação Técnico-Científica e Auditoria")
            
            with st.expander("1. Engenharia de Funcionamento do Aplicativo"):
                st.markdown("""
                Este software implementa um ecossistema de **Engenharia Reversa de Redes** operando em quatro camadas:
                1. **Vetorização de Lote:** Extrai os eixos de texto das células da planilha carregada.
                2. **Pipeline Agnóstico de Padronização:** Converte de forma soberana CEPs e entradas informais em estruturas de endereços completos validados (Correios/ArcGIS outFields) antes de gerar as rotas.
                3. **Direções Canônicas Públicas:** Constrói URLs estruturadas forçando o navegador a carregar o trajeto rodoviário sem reinterpretações dinâmicas de bairros.
                4. **Vincenty Geodésico:** Computa a linha reta teórica perfeita baseada no elipsoide real da Terra (WGS-84).
                """)
