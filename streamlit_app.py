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

# ==========================================
# CAMADA 1: HIGIENIZAÇÃO UNIVERSAL DE TEXTO
# ==========================================
def higienizar_string_universal(texto):
    """
    Remove ruídos gramaticais e padroniza abreviações comuns de logradouros
    brasileiros sem prender o código a nenhuma cidade específica.
    """
    if not texto or texto == 'nan':
        return ""
    
    txt = str(texto).strip()
    # Remove espaços duplos e caracteres invisíveis
    txt = re.sub(r'\s+', ' ', txt)
    
    # Dicionário de abreviações universais (Válido para todo o território nacional)
    abreviacoes = {
        r"\bav\b\.?": "Avenida",
        r"\br\b\.?": "Rua",
        r"\bqd\b\.?": "Quadra",
        r"\blt\b\.?": "Lote",
        r"\bcj\b\.?": "Conjunto",
        r"\bbl\b\.?": "Bloco",
        r"\bapt\b\.?": "Apartamento",
        r"\bap\b\.?": "Apartamento",
        r"\brod\b\.?": "Rodovia",
        r"\bestr\b\.?": "Estrada"
    }
    
    for padrao, substituicao in abreviacoes.items():
        txt = re.sub(padrao, substituicao, txt, flags=re.IGNORECASE)
        
    return txt

# ==========================================
# CAMADA 2: RESOLUÇÃO UNIVERSAL DE ENDEREÇOS
# ==========================================
def obter_coordenadas_e_endereco_oficial(localidade):
    """
    Pipeline dinâmico e agnóstico. Classifica o tipo de endereço por metadados (ArcGIS)
    e decide se a geocodificação é confiável ou se necessita de enriquecimento por string.
    """
    texto_puro = str(localidade).strip()
    texto_limpo = higienizar_string_universal(texto_puro)
    
    # Se a entrada contiver um padrão de CEP, limpa e valida via ViaCEP
    cep_localizado = re.sub(r'\D', '', texto_limpo)
    if len(cep_localizado) == 8 and (texto_puro.isdigit() or "-" in texto_puro):
        try:
            res_cep = requests.get(f"https://viacep.com.br/ws/{cep_localizado}/json/", timeout=5).json()
            if "erro" not in res_cep:
                componentes = [res_cep.get('logradouro'), res_cep.get('bairro'), res_cep.get('localidade'), res_cep.get('uf')]
                texto_limpo = ", ".join([c for c in componentes if c]) + f", {res_cep.get('cep')}, Brasil"
        except Exception:
            pass

    # Garante o escopo nacional se o usuário omitir o país
    query = texto_limpo if "brasil" in texto_limpo.lower() else f"{texto_limpo}, Brasil"
    
    # Consultando o ArcGIS com outFields=* para obter o 'Addr_type' (Tipo de correspondência)
    url_arcgis = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query)}&maxLocations=3&sourceCountry=BRA&outFields=*"
    
    try:
        resposta = requests.get(url_arcgis, timeout=10).json()
        if resposta.get('candidates'):
            # Seleciona o candidato com maior score de aderência textual
            candidato = max(resposta['candidates'], key=lambda x: x.get('score', 0))
            
            lat = float(candidato['location']['y'])
            lon = float(candidato['location']['x'])
            endereco_reconstruido = candidato['address']
            
            # Captura metadados de qualidade do próprio geocodificador
            atributos = candidato.get('attributes', {})
            tipo_endereco = atributos.get('Addr_type', '') # Ex: 'PointAddress', 'StreetName', 'POI', 'Locality'
            
            # Se o geocodificador reconstruiu as chaves estruturadas, nós as usamos
            logradouro_arc = atributos.get('StAddr', '').strip()
            bairro_arc = atributos.get('Neighborhood', '').strip()
            cidade_arc = atributos.get('City', '').strip()
            estado_arc = atributos.get('RegionAbbr', '').strip() or atributos.get('Region', '').strip()
            
            if logradouro_arc and cidade_arc:
                componentes_reconstruidos = [logradouro_arc, bairro_arc, cidade_arc, estado_arc]
                endereco_reconstruido = ", ".join([c for c in componentes_reconstruidos if c])
            
            # CRÍTICO: Passamos o tipo_endereco adiante para o pipeline logístico tomar decisões de injeção
            return lat, lon, endereco_reconstruido, tipo_endereco
            
    except Exception:
        pass
        
    # Fallback agnóstico caso o ArcGIS falhe por completo
    return 0.0, 0.0, query, "Unknown"

# ==========================================
# CAMADA 3: INTERCEPTAÇÃO INTELIGENTE GOOGLE
# ==========================================
def extrair_dados_reais_google(origem_raw, destino_raw, lat_o, lon_o, lat_d, lon_d, usar_coordenadas):
    """
    Intercepta as direções baseando-se na inteligência de metadados. 
    Evita Geocoding Drift chaveando dinamicamente entre Coordenadas e Strings Estruturadas.
    """
    if usar_coordenadas and lat_o and lon_o and lat_d and lon_d:
        origem_param = f"{lat_o},{lon_o}"
        destino_param = f"{lat_d},{lon_d}"
        url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
    else:
        # Se for um POI ou local sem número, injeta a string ultra-estruturada limpa pelo resolvedor
        origem_param = requests.utils.quote(str(origem_raw).strip())
        destino_param = requests.utils.quote(str(destino_raw).strip())
        url_api = f"https://www.google.com/maps/preview/directions?authuser=0&hl=pt-BR&gl=br&pb=!1m2!1m1!1s{origem_param}!1m2!1m1!1s{destino_param}!3e0"
    
    # URL pública canônica baseada nas strings limpas para garantir fidelidade no clique do usuário
    link_maps = f"http://googleusercontent.com/maps.google.com/5{requests.utils.quote(str(origem_raw).strip())}&destination={requests.utils.quote(str(destino_raw).strip())}&travelmode=driving"
    
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
            if any(re.search(p, texto_resposta.lower()) for p in [r'\"utilizar\s+balsa\b', r'\"pegar\s+balsa\b', r'\"ferry\b']):
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

# ==========================================
# CAMADA 4: PIPELINE LOGÍSTICO CENTRAL
# ==========================================
def calcular_pipeline_logistico(origem, destino):
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    # Processa as localidades e extrai as coordenadas + o tipo estrutural do endereço
    lat_o, lon_o, origem_oficial, tipo_o = obter_coordenadas_e_endereco_oficial(origem_clean)
    lat_d, lon_d, destino_oficial, tipo_d = obter_coordenadas_e_endereco_oficial(destino_clean)
    
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d) if (lat_o != 0.0 and lat_d != 0.0) else 0.0

    # REGRA DE OURO AUTOMÁTICA: 
    # Se o endereço for classificado como localidade ampla (Locality), bairro (Neighborhood) ou POI, 
    # NUNCA usamos a coordenada crua para a busca do Google. Forçamos a string limpa e estruturada.
    # Coordenadas cruas ficam restritas a precisões cirúrgicas ('PointAddress' ou 'Building').
    usar_coords = True
    if tipo_o in ['POI', 'Locality', 'Neighborhood', 'Unknown'] or tipo_d in ['POI', 'Locality', 'Neighborhood', 'Unknown']:
        usar_coords = False
        
    # Dispara a busca no motor do Google Maps
    dados_reais = extrair_dados_reais_google(origem_oficial, destino_oficial, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=usar_coords)
    
    if dados_reais:
        km_google, tempo_google, link_google, balsa_google = dados_reais
        return km_google, tempo_google, link_google, balsa_google, dist_linha_reta

    # FALLBACK OPERACIONAL EM CASO DE QUIDAM DE REDE
    link_maps_fallback = f"http://googleusercontent.com/maps.google.com/5{requests.utils.quote(origem_oficial)}&destination={requests.utils.quote(destino_oficial)}&travelmode=driving"
    km_terrestre = round(dist_linha_reta * 1.27, 2) if dist_linha_reta > 0.0 else 0.0
    v_comercial = 65.0 if km_terrestre >= 150 else 45.0
    minutos = round((km_terrestre / v_comercial) * 60) if km_terrestre > 0.0 else 0
    tempo_txt = f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min"
    
    return km_terrestre, tempo_txt, link_maps_fallback, "Não", dist_linha_reta

# ==========================================
# INTERFACE GRÁFICA (STREAMLIT)
# ==========================================
st.title("🚗 Gerenciador de Rotas Inteligentes")
st.subheader("Engine Dinâmica de Resolução e Classificação de Endereços")

arquivo_carregado = st.file_uploader("Selecionar Arquivo Excel", type=["xlsx"])

if arquivo_carregado is not None:
    df = pd.read_excel(arquivo_carregado)
    
    if 'Origem' not in df.columns or 'Destino' not in df.columns:
        st.error("Erro: A planilha precisa conter as colunas exatas 'Origem' e 'Destino'.")
    else:
        st.success("Dados carregados com sucesso!")
        
        if st.button("Processar Rotas em Lote"):
            for col in ['Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta']:
                df[col] = None

            total_linhas = len(df)
            barra_progresso = st.progress(0)
            container_status = st.empty()
            
            for index, linha in df.iterrows():
                origem = str(linha['Origem']).strip()
                destino = str(linha['Destino']).strip()
                
                if origin := (origem and destino and origem.lower() != 'nan' and destino.lower() != 'nan'):
                    container_status.text(f"🔢 Linha {index + 1}/{total_linhas}: {origem} ➔ {destino}")
                    
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
            
            st.write("---")
            st.balloons()
            st.download_button(
                label="📥 Baixar Planilha Logística Processada",
                data=output_buffer.getvalue(),
                file_name="rotas_resolvidas_automatico.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
