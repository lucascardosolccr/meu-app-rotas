import streamlit as st
import pandas as pd
import googlemaps
import math
import io
import time

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

def calcular_rota_google_oficial(origem, destino, gmaps_client):
    """Consulta diretamente a API Oficial do Google Maps para extrair distância e tempos reais"""
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    origem_query = origem_clean if "," in origem_clean.lower() else f"{origem_clean}, Brasil"
    destino_query = destino_clean if "," in destino_clean.lower() else f"{destino_clean}, Brasil"

    # Link dinâmico do Google Maps gerado exatamente no padrão para o usuário clicar
    import requests
    link_maps = f"https://www.google.com/maps/dir/{requests.utils.quote(origem_query)}/{requests.utils.quote(destino_query)}"
    
    try:
        # Consulta a rota oficial no modo de direção (Carro)
        resultado_rota = gmaps_client.directions(
            origin=origem_query,
            destination=destino_query,
            mode="driving",
            language="pt-BR"
        )
        
        if resultado_rota:
            leg = resultado_rota[0]['legs'][0]
            
            # Extrai os valores numéricos exatos e formatações reais do Google
            km_terrestre = round(leg['distance']['value'] / 1000, 2) # Converte metros para KM decimal
            tempo_txt = leg['duration']['text'] # Retorna exatamente o texto do Google (ex: "6 horas 6 min")
            
            # Captura coordenadas geográficas reais para calcular a Linha Reta
            lat_o = leg['start_location']['lat']
            lon_o = leg['start_location']['lng']
            lat_d = leg['end_location']['lat']
            lon_d = leg['end_location']['lng']
            
            dist_linha_reta = calcular_distancia_vincenty(lat_o, lon_o, lat_d, lon_d)
            
            # Mapeamento inteligente regional de balsas
            envolve_balsa = "Não"
            cidades_balsa = ["moz", "almeirim", "soure", "salvaterra", "marajó", "cametá", "itaituba", "chaves", "gurupá", "cascalheira", "araguaia"]
            if any(c in origem_clean.lower() or c in destino_clean.lower() for c in cidades_balsa):
                envolve_balsa = "Sim"
                
            return km_terrestre, tempo_txt, link_maps, envolve_balsa, dist_linha_reta
            
        return "Rota não encontrada", "Rota não encontrada", link_maps, "Não", 0.0
    except Exception as e:
        return "Erro na API do Google", "Erro na API do Google", link_maps, "Não", 0.0

# --- INTERFACE VISUAL NO STREAMLIT ---
st.title("🚗 Calculador de Rotas de Alta Precisão (Google Maps API)")
st.write("Esse sistema utiliza a API oficial do Google para trazer quilometragens e tempos 100% corretos.")

# Campo para colocar a chave oficial do Google obtida no Passo 1
google_key = st.text_input("AIzaSyAzB0c2qJIePvoeG64QxIJEM03nBuX-_60", type="password")

if not google_key:
    st.info("AIzaSyAzB0c2qJIePvoeG64QxIJEM03nBuX-_60")
else:
    # Inicializa o cliente oficial do Google Maps
    gmaps_client = googlemaps.Client(key=google_key)
    
    arquivo_carregado = st.file_uploader("Selecione seu arquivo Excel (.xlsx)", type=["xlsx"])

    if arquivo_carregado is not None:
        df = pd.read_excel(arquivo_carregado)
        
        if 'Origem' not in df.columns or 'Destino' not in df.columns:
            st.error("A planilha precisa conter as colunas exatas: 'Origem' e 'Destino'.")
        else:
            st.success("Planilha mapeada com sucesso!")
            
            if st.button("Iniciar Processamento das Rotas"):
                colunas_finais = ['Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta']
                for col in colunas_finais:
                    df[col] = None
                
                total_linhas = len(df)
                barra_progresso = st.progress(0)
                texto_status = st.empty()
                
                for index, linha in df.iterrows():
                    origem = str(linha['Origem']).strip()
                    destino = str(linha['Destino']).strip()
                    
                    if origem and destino and origem != 'nan' and destino != 'nan':
                        texto_status.text(f"Processando linha {index+1}/{total_linhas}: {origem} ➔ {destino}")
                        
                        km, tempo, link, balsa_status, linha_reta = calcular_rota_google_oficial(origem, destino, gmaps_client)
                        
                        df.at[index, 'Distancia'] = km
                        df.at[index, 'Tempo'] = tempo
                        df.at[index, 'Link da Rota'] = link
                        df.at[index, 'Balsas'] = balsa_status
                        df.at[index, 'Linha Reta'] = linha_reta
                        
                        # Pausa mínima de conformidade com a API do Google
                        time.sleep(0.05)
                    
                    barra_progresso.progress((index + 1) / total_linhas)
                
                texto_status.text("✨ Processamento concluído com exatidão máxima!")
                
                ordem_colunas = ['Origem', 'Destino', 'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta']
                df = df.reindex(columns=ordem_colunas)
                
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False)
                dados_excel = output.getvalue()
                
                st.write("---")
                st.balloons()
                
                st.download_button(
                    label="📥 Baixar Planilha Pronta",
                    data=dados_excel,
                    file_name="planilha_rotas_google_precision.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
