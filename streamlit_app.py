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

def extrair_dados_reais_google(origem_oficial, destino_oficial):
    """
    CAMADA BRUTA - Intercepta a API interna de direções do Google Maps.
    Utiliza as strings ricas e hierarquizadas para extrair KMs e tempos reais de tráfego.
    """
    origem_enc = requests.utils.quote(f"{origem_oficial}".strip())
    destino_enc = requests.utils.quote(f"{destino_oficial}".strip())
    
    # URL de Direções Canônicas em modo de navegação direta rodoviária (driving) - Padrão Universal
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={origem_enc}&destination={destino_enc}&travelmode=driving"
    
    # Endpoint da API interna estruturada de tráfego do Google Maps
    url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_enc}!1m2!1m1!1s{destino_enc}!3e0"
    
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
            
            # --- DETECÇÃO REFINADA DE BALSAS ---
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

def geocodificar_coordenadas_fallback(query_texto):
    """Consulta auxiliar gratuita no ArcGIS Server apenas para gerar Lat/Lon do Vincenty"""
    url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query_texto)}&maxLocations=1&sourceCountry=BRA"
    try:
        res = requests.get(url, timeout=10).json()
        if res.get('candidates'):
            pt = res['candidates'][0]['location']
            return float(pt['y']), float(pt['x'])
    except Exception:
        pass
    return 0.0, 0.0

def tratar_e_normalizar_localidade_universal(localidade_raw):
    """
    CAMADA INTELIGENTE DE DESAMBIGUÇÃO - Resolve de forma agnóstica CEPs e textos comuns.
    Desestrutura o retorno postal e remonta a string forçando indexação hierárquica e unificada.
    """
    texto_str = str(localidade_raw).strip()
    
    # 1. PROCESSAMENTO LOGÍSTICO DE COMPOSIÇÃO POSTAL
    cep_limpo = re.sub(r'\D', '', texto_str)
    if len(cep_limpo) == 8 and (texto_str.isdigit() or "-" in texto_str):
        try:
            resposta = requests.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=5)
            if resposta.status_code == 200:
                dados = resposta.json()
                if "erro" not in dados:
                    logradouro = dados.get('logradouro', '').strip()
                    bairro = dados.get('bairro', '').strip()
                    localidade = dados.get('localidade', '').strip()
                    uf = dados.get('uf', '').strip()
                    
                    # Normalização semântica automática para siglas industriais do Distrito Federal
                    if uf.upper() == "DF":
                        if "ZONA INDUSTRIAL" in bairro.upper() or "ZONA INDUSTRIAL" in logradouro.upper():
                            bairro = "SIG"
                    
                    # Concatenação Hierárquica Rígida: Garante que o Google valide o pacote de dados inteiro
                    componentes = [logradouro, bairro, localidade, uf]
                    endereco_unificado = ", ".join([c for c in componentes if c])
                    
                    # Retorna o endereço amarrando o CEP no final da string de busca
                    return f"{endereco_unificado}, {cep_limpo}, Brasil"
        except Exception:
            pass

    # 2. SE FOR ENDEREÇO TEXTUAL OU PONTO DE INTERESSE (POI) COMMON
    # Injeta automaticamente a âncora regional base para evitar conflitos homônimos interestaduais
    if "BRASIL" not in texto_str.upper():
        if "DF" not in texto_str.upper() and "BRASILIA" not in texto_str.upper() and "BRASÍLIA" not in texto_str.upper():
            # Estratégia adaptativa: Se for uma palavra conhecida do DF, anexa Brasília, senão trata nacionalmente
            if any(t in texto_str.upper() for t in ["TAGUATINGA", "SAMAMBAIA", "PONTE ALTA", "CEILANDIA", "GUARA", "UNB", "UNICEUB"]):
                return f"{texto_str}, Brasília, DF, Brasil"
        return f"{texto_str}, Brasil"
        
    return texto_str

def calcular_pipeline_logistico(origem_bruta, destino_bruto):
    """Pipeline central de roteamento universal baseado em metadados estáveis"""
    
    # Executa o motor de normalização textual em lote
    origem_oficial = tratar_e_normalizar_localidade_universal(origem_bruta)
    destino_oficial = tratar_e_normalizar_localidade_universal(destino_bruto)
    
    # Obtém as coordenadas apenas para manter o cálculo analítico do Vincenty (Linha Reta)
    lat_o, lon_o = geocodificar_coordenadas_fallback(origem_oficial)
    lat_d, lon_d = geocodificar_coordenadas_fallback(destino_oficial)
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)

    # Dispara a busca rodoviária real no motor do Google Maps usando a frase blindada
    dados_reais = extrair_dados_reais_google(origem_oficial, destino_oficial)
    
    if dados_reais and isinstance(dados_reais, tuple) and len(dados_reais) == 4:
        km_google, tempo_google, link_google, balsa_google = dados_reais
        return km_google, tempo_google, link_google, balsa_google, dist_linha_reta

    # CONTINGÊNCIA EM CASO DE FALHA DE CONEXÃO
    link_fallback = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(origem_oficial)}&destination={requests.utils.quote(destino_oficial)}&travelmode=driving"
    km_terrestre = round(dist_linha_reta * 1.27, 2) if dist_linha_reta > 0.0 else 0.0
    v_comercial = 65.0 if km_terrestre >= 150 else 45.0
    minutos = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0.0 else 0
    tempo_txt = f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min" if minutos % 60 > 0 else f"{minutos // 60} h"
    
    return km_terrestre, tempo_txt, link_fallback, "Não", dist_linha_reta

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
            
            for index, presidential_line in df.iterrows():
                origem = str(presidential_line['Origem']).strip()
                destino = str(presidential_line['Destino']).strip()
                
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
                Este software implementa um ecossistema de **Engenharia Reversa de Redes** operando em três camadas puras:
                1. **Vetorização de Lote:** Extrai os eixos de texto das células da planilha carregada.
                2. **Tratamento Postal Dinâmico (Feedback Loop Geocoding):** Intercepta códigos postais e reconstrói a string fundindo os metadados oficiais sob concatenação rígida. Adiciona o CEP no final do bloco textual para travar a varredura semântica.
                3. **Direções Canônicas Estritas:** Monta URLs sob o protocolo oficial e autenticado do Google (`https://www.google.com/maps/dir/?api=1`). Esse barramento força o navegador a respeitar os limites de endereçamento enviados, elidindo palpites do algoritmo do mapa.
                """)
