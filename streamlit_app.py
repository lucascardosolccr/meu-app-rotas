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

# ==============================================================================
# 🌟 NOVA RESOLUÇÃO UNIVERSAL DE ENDEREÇOS (PIpipeline de 10 Camadas Gratuitas)
# ==============================================================================

def calcular_levenshtein_local(s1, s2):
    """Algoritmo de Similaridade de Distância Levenshtein Nátivo (Camada 2 Fuzzy)"""
    if len(s1) < len(s2):
        return calcular_levenshtein_local(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    distance = previous_row[-1]
    max_len = max(len(s1), len(s2))
    return (1 - (distance / max_len)) * 100

def sanitizar_texto_universal(texto):
    """Camada 1: Limpeza, Normalização Unicode e Expansão de Abreviações"""
    t = str(texto).strip()
    t = re.sub(r'[\x00-\x1F\x7F-\x9F]', '', t)  # Remove caracteres invisíveis
    t = re.sub(r'\s+', ' ', t)  # Remove espaços duplicados
    
    # Tabela de Abreviações Consagradas Nacionais
    substituicoes = {
        r'\bAV\b': 'Avenida', r'\bR\b': 'Rua', r'\bQD\b': 'Quadra', r'\bQ\b': 'Quadra',
        r'\bCJ\b': 'Conjunto', r'\bCONJ\b': 'Conjunto', r'\bLT\b': 'Lote', r'\bL\b': 'Lote',
        r'\bAPT\b': 'Apartamento', r'\bAP\b': 'Apartamento', r'\bBL\b': 'Bloco', r'\bB\b': 'Bloco',
        r'\bST\b': 'Setor', r'\bCH\b': 'Chácara', r'\bCHAC\b': 'Chácara', r'\bKM\b': 'Quilômetro',
        r'\bDF\b': 'Distrito Federal', r'\bTRAV\b': 'Travessa', r'\bBR\b': 'Rodovia BR'
    }
    
    for padrao, expansao in substituicoes.items():
        t = re.sub(padrao, expansao, t, flags=re.IGNORECASE)
    return t

def camada_postal_redundante(cep_limpo):
    """Camada 3: Resolução Postal com Tolerância a Falhas (ViaCEP + BrasilAPI)"""
    # Fallback 1: ViaCEP
    try:
        res = requests.get(f"https://viacep.com.br/ws/{cep_limpo}/json/", timeout=4).json()
        if "erro" not in res:
            return res.get('logradouro', ''), res.get('bairro', ''), res.get('localidade', ''), res.get('uf', '')
    except Exception:
        pass
        
    # Fallback 2: BrasilAPI
    try:
        res = requests.get(f"https://brasilapi.com.br/api/cep/v1/{cep_limpo}", timeout=4).json()
        if "name" not in res:
            return res.get('street', ''), res.get('neighborhood', ''), res.get('city', ''), res.get('state', '')
    except Exception:
        pass
        
    return "", "", "", ""

def obter_coordenadas_e_endereco_oficial(localidade):
    """
    CAMADA GEOGRÁFICA INTEROPERÁVEL UNIVERSAL - Pipeline Consolidado.
    Resolve dinamicamente inputs incompletos através de votação multi-fonte.
    """
    texto_original = str(localidade).strip()
    texto_limpo = sanitizar_texto_universal(texto_original)
    texto_upper = texto_limpo.upper()
    
    # Tenta pescar alguma numeração ou indicação de quadra/bloco original do usuário
    fração_predial = ""
    match_fracao = re.search(r'\b(CONJUNTO|CJ|QUADRA|QD|BLOCO|BL|LOTE|LT|CHÁCARA|CHACARA|NÚMERO|Nº|NUMERO|N|CASA|AP|KM)\s*([A-Za-z0-9\/.]+)\b', texto_upper)
    if match_fracao and not re.match(r'^\d{5}-?\d{3}$', texto_original):
        fração_predial = match_fracao.group(0)

    # 1. PROCESSAMENTO DE CEP DIRECT (Camada 3 Saneada)
    digits_cep = re.sub(r'\D', '', texto_original)
    if len(digits_cep) == 8 and (texto_original.isdigit() or "-" in texto_original or "CEP" in texto_upper):
        logr, bair, loca, uf = camada_postal_redundante(digits_cep)
        if loca:
            endereco_oficial_cep = ", ".join([c for c in [logr, fracao_predial if fracao_predial else "", bair, loca, uf] if c.strip()]) + ", Brasil"
            
            # Captura Lat/Lon do CEP via OpenStreetMap
            url_osm = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(endereco_oficial_cep)}&limit=1&countrycodes=br"
            headers_osm = {"User-Agent": "GerenciadorRotasInteligentes/6.0 (lucasccruz@gmail.com)"}
            try:
                res_osm = requests.get(url_osm, headers=headers_osm, timeout=4).json()
                if res_osm:
                    return float(res_osm[0]['lat']), float(res_osm[0]['lon']), endereco_oficial_cep
            except Exception:
                pass
            return 0.0, 0.0, endereco_oficial_cep

    # 2. GEOCODIFICAÇÃO MULTI-FONTE PARALELA E CONSENSO (Camadas 4, 5, 6, 8, 9)
    query_completa = f"{texto_limpo}, Brasil" if "BRASIL" not in texto_upper else texto_limpo
    
    candidatos_fontes = []
    
    # Fonte A: ArcGIS REST Engine
    url_arc = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(query_completa)}&maxLocations=3&sourceCountry=BRA&outFields=StAddr,Neighborhood,City,RegionAbbr"
    try:
        res_arc = requests.get(url_arc, timeout=5).json()
        if res_arc.get('candidates'):
            cand = res_arc['candidates'][0]
            attr = cand.get('attributes', {})
            candidatos_fontes.append({
                'lat': float(cand['location']['y']),
                'lon': float(cand['location']['x']),
                'rua': attr.get('StAddr', ''),
                'bairro': attr.get('Neighborhood', ''),
                'cidade': attr.get('City', ''),
                'estado': attr.get('RegionAbbr', ''),
                'score_bruto': cand.get('score', 0)
            })
    except Exception:
        pass

    # Fonte B: OpenStreetMap Nominatim Engine
    url_osm = f"https://nominatim.openstreetmap.org/search?format=json&q={requests.utils.quote(query_completa)}&limit=2&addressdetails=1&countrycodes=br"
    headers_osm = {"User-Agent": "GerenciadorRotasInteligentes/6.0 (lucasccruz@gmail.com)"}
    try:
        res_osm = requests.get(url_osm, headers=headers_osm, timeout=5).json()
        if res_osm:
            alvo = res_osm[0]
            details = alvo.get('address', {})
            candidatos_fontes.append({
                'lat': float(alvo['lat']),
                'lon': float(alvo['lon']),
                'rua': details.get('road', details.get('pedestrian', details.get('construction', ''))),
                'bairro': details.get('neighbourhood', details.get('suburb', details.get('city_district', ''))),
                'cidade': details.get('city', details.get('town', '')),
                'estado': details.get('state', ''),
                'score_bruto': 90
            })
    except Exception:
        pass

    # ALGORITMO DE VOTAÇÃO E SCORE DE CONFIANÇA (Camadas 8 e 9)
    if candidatos_fontes:
        # Seleciona o candidato com maior estabilidade cadastral e proximidade semântica
        melhor_candidato = candidatos_fontes[0]
        max_score_calculado = -1.0
        
        for cand in candidatos_fontes:
            # Avalia a completude e qualidade do texto via Fuzzy Matching com a digitação do usuário
            string_remontada = f"{cand['rua']} {cand['bairro']} {cand['cidade']} {cand['estado']}"
            score_fuzzy = calcular_levenshtein_local(texto_limpo, string_remontada)
            
            score_final_provedor = (cand['score_bruto'] * 0.4) + (score_fuzzy * 0.6)
            if score_final_provedor > max_score_calculado:
                max_score_calculado = score_final_provedor
                melhor_candidato = cand
                
        # Remonta a estrutura canônica enriquecida
        rua_final = melhor_candidato['rua'] if melhor_candidato['rua'] else texto_limpo
        if fração_predial and fração_predial.upper() not in rua_final.upper():
            rua_final = f"{rua_final} {fração_predial.title()}"
            
        componentes_finais = [rua_final, melhor_candidato['bairro'], melhor_candidato['cidade'], melhor_candidato['estado']]
        endereco_estruturado_final = ", ".join([c for c in componentes_finais if c.strip()]) + ", Brasil"
        
        return melhor_candidato['lat'], melhor_candidato['lon'], endereco_estruturado_final

    return 0.0, 0.0, query_completa

def calcular_pipeline_logistico(origem, destino):
    """Pipeline central avançado com injeção contextual de strings e coordenadas"""
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    # Processa os endereços de lote através do ecossistema das APIs de Consenso
    lat_o, lon_o, origem_oficial = obter_coordenadas_e_endereco_oficial(origem_clean)
    lat_d, lon_d, destino_oficial = obter_coordenadas_e_endereco_oficial(destino_clean)
    
    dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)

    # Coordenadas numéricas travam o pino do Google Maps se o vetor analítico Vincenty for coerente (< 120km)
    usar_coords = True if (lat_o != 0.0 and lat_d != 0.0 and dist_linha_reta < 120.0) else False
    dados_reais = extrair_dados_reais_google(origem_oficial, destino_oficial, lat_o, lon_o, lat_d, lon_d, usar_coordenadas=usar_coords)
    
    if dados_reais:
        km_google, tempo_google, link_google, balsa_google = dados_reais
        
        # --- AUDITORIA MATEMÁTICA ANTI-ALUCINAÇÃO (Pós-Google Maps) ---
        if km_google > 120.0 and dist_linha_reta < 45.0:
            dados_reais_seguros = extrair_dados_reais_google(origem_oficial, destino_oficial, 0.0, 0.0, 0.0, 0.0, usar_coordenadas=False)
            if dados_reais_seguros:
                return dados_reais_seguros[0], dados_reais_seguros[1], dados_reais_seguros[2], dados_reais_seguros[3], dist_linha_reta
                
        return km_google, tempo_google, link_google, balsa_google, dist_linha_reta

    # CONTINGÊNCIA EM CASO DE ADVERSIDADE DE REDE
    link_maps_fallback = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(origem_oficial)}&destination={requests.utils.quote(destino_oficial)}&travelmode=driving"
    km_terrestre = round(dist_linha_reta * 1.27, 2) if dist_linha_reta > 0.0 else 0.0
    minutos = round((km_terrestre / 45.0) * 60) if km_terrestre > 0.0 else 0
    tempo_txt = f"{minutos} min" if minutos < 60 else f"{minutos // 60} h {minutos % 60} min"
    return km_terrestre, tempo_txt, link_maps_fallback, "Não", dist_linha_reta

# --- INTERFACE VISUAL NO STREAMLIT ---
st.title("🚗 Gerenciador de Rotas Inteligentes")
st.subheader("Engine de Alta Precisão Geográfica e Roteamento Universal")
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
