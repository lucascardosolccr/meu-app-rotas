import streamlit as st
import pandas as pd
import requests
import time
import math
import io

# 🔑 INSIRA A SUA CHAVE DE API DO GOOGLE MAPS ENTRE AS ASPAS ABAIXO:
CHAVE_GOOGLE_FIXA = "AIzaSyAzB0c2qJIePvoeG64QxIJEM03nBuX-_60"

# Configuração da página do site
st.set_page_config(page_title="Gerenciador de Rotas Inteligentes", page_icon="🚗", layout="centered")

def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    """Calcula a linha reta exata baseada no elipsoide real da Terra (WGS-84)"""
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
        for _ in range(100):
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
    except:
        return 0.0

def calcular_rota_definitiva_google(origem, destino, uf_origem="", uf_destino=""):
    """Consulta diretamente os servidores do Google Maps via API HTTP para precisão absoluta do link"""
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    # Adiciona contexto inteligente de Estado para evitar homônimos (Ex: Taguatinga, TO)
    origem_query = origem_clean
    if uf_origem and str(uf_origem).strip().lower() != 'nan':
        origem_query += f", {str(uf_origem).strip()}"
    elif "brasil" not in origem_clean.lower() and "," not in origem_clean:
        origem_query += ", Tocantins, Brasil"
        
    destino_query = destino_clean
    if uf_destino and str(uf_destino).strip().lower() != 'nan':
        destino_query += f", {str(uf_destino).strip()}"
    elif "brasil" not in destino_clean.lower() and "," not in destino_clean:
        destino_query += ", Tocantins, Brasil"

    # Link idêntico ao gerado na interface web do Google Maps
    link_maps = f"https://www.google.com/maps/dir/?api=1&origin={requests.utils.quote(origem_query)}&destination={requests.utils.quote(destino_query)}&travelmode=driving"
    
    # Inicializa variáveis padrão de detecção de balsa
    envolve_balsa = "Não"
    palavras_chave_balsa = ["cascalheira", "araguaia", "balsa", "travessia", "moz", "almeirim", "soure", "salvaterra", "marajó", "cametá", "itaituba", "chaves", "gurupá"]
    if any(p in origem_clean.lower() or p in destino_clean.lower() for p in palavras_chave_balsa):
        envolve_balsa = "Sim"

    if not CHAVE_GOOGLE_FIXA or CHAVE_GOOGLE_FIXA == "AIzaSyAzB0c2qJIePvoeG64QxIJEM03nBuX-_60":
        return "Configure a chave na linha 12", "Chave ausente", link_maps, envolve_balsa, 0.0

    try:
        # Chamada direta HTTP à API do Google Maps configurada para evitar balsas (avoid=ferries)
        # Isso garante que a Distância e o Tempo venham estritamente pelo melhor trajeto terrestre/rodoviário
        url = f"https://maps.googleapis.com/maps/api/directions/json?origin={requests.utils.quote(origem_query)}&destination={requests.utils.quote(destino_query)}&mode=driving&avoid=ferries&language=pt-BR&key={CHAVE_GOOGLE_FIXA}"
        
        resposta = requests.get(url, timeout=12).json()
        
        if resposta.get("status") == "OK" and resposta.get("routes"):
            leg = resposta["routes"][0]["legs"][0]
            
            # Extração exata e em tempo real dos dados fornecidos pelo Google Maps
            km_terrestre = round(leg["distance"]["value"] / 1000, 2)
            tempo_txt = leg["duration"]["text"]
            
            # Captura de coordenadas para cálculo de Linha Reta auxiliar
            lat_o = leg["start_location"]["lat"]
            lon_o = leg["start_location"]["lng"]
            lat_d = leg["end_location"]["lat"]
            lon_d = leg["end_location"]["lng"]
            dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)
            
            return km_terrestre, tempo_txt, link_maps, envolve_balsa, dist_linha_reta
        else:
            # Caso o status retorne algum erro de cota ou endereço inválido
            status_erro = resposta.get("status", "Erro desconhecido")
            return f"Erro Google ({status_erro})", "Verificar", link_maps, envolve_balsa, 0.0
            
    except Exception as e:
        return "Erro de conexão", "Erro técnico", link_maps, envolve_balsa, 0.0

# --- INTERFACE VISUAL NO APP ---
st.title("🚗 Calculador de Rotas de Alta Precisão (Google Oficial)")
st.write("Processamento em lote conectado diretamente aos servidores do Google Maps com exclusão de rotas fluviais.")

if CHAVE_GOOGLE_FIXA == "AIzaSyAzB0c2qJIePvoeG64QxIJEM03nBuX-_60" or not CHAVE_GOOGLE_FIXA:
    st.error("❌ A chave de API do Google não foi configurada! Abra o código e coloque sua chave na linha 12 para que o sistema funcione.")

arquivo_carregado = st.file_uploader("Selecione seu arquivo Excel (.xlsx)", type=["xlsx"])

if arquivo_carregado is not None:
    df = pd.read_excel(arquivo_carregado)
    
    if 'Origem' not in df.columns or 'Destino' not in df.columns:
        st.error("A planilha precisa conter as colunas exatas: 'Origem' e 'Destino'.")
    else:
        st.success("Planilha validada com sucesso!")
        
        if st.button("Iniciar Processamento das Rotas"):
            colunas_finais = ['Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta']
            for col in colunas_finais:
                df[col] = None
            
            # Mapeia colunas auxiliares de UF/Estado caso existam na planilha original do usuário
            col_uf_o = next((c for c in df.columns if c.lower() in ['uf_origem', 'uf origem', 'estado origem', 'origem_uf']), None)
            col_uf_d = next((c for c in df.columns if c.lower() in ['uf_destino', 'uf destino', 'estado destino', 'destino_uf']), None)

            total_linhas = len(df)
            barra_progresso = st.progress(0)
            texto_status = st.empty()
            
            for index, linha in df.iterrows():
                origem = str(linha['Origem']).strip()
                destino = str(linha['Destino']).strip()
                
                uf_o = str(linha[col_uf_o]).strip() if col_uf_o else ""
                uf_d = str(linha[col_uf_d]).strip() if col_uf_d else ""
                
                if origem and destino and origem != 'nan' and destino != 'nan':
                    texto_status.text(f"Processando linha {index+1}/{total_linhas}: {origem} ➔ {destino}")
                    
                    km, tempo, link, balsa_status, linha_reta = calcular_rota_definitiva_google(origem, destino, uf_o, uf_d)
                    
                    df.at[index, 'Distancia'] = km
                    df.at[index, 'Tempo'] = tempo
                    df.at[index, 'Link da Rota'] = link
                    df.at[index, 'Balsas'] = balsa_status
                    df.at[index, 'Linha Reta'] = linha_reta
                    
                    # Intervalo mínimo padrão de requisições por segundo
                    time.sleep(0.04)
                
                barra_progresso.progress((index + 1) / total_linhas)
            
            texto_status.text("✨ Processamento concluído com exatidão máxima do Google!")
            
            ordem_colunas = ['Origem', 'Destino', 'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta']
            for c in df.columns:
                if c not in ordem_colunas:
                    ordem_colunas.insert(0, c)
                    
            df = df.reindex(columns=ordem_colunas)
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False)
            dados_excel = output.getvalue()
            
            st.write("---")
            st.balloons()
            
            st.download_button(
                label="📥 Baixar Planilha Oficial Google Precision",
                data=dados_excel,
                file_name="planilha_rotas_google_precision.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
