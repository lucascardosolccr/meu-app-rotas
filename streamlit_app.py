import streamlit as st
import pandas as pd
import requests
import time
import math
import io

# Configuração da página do site
st.set_page_config(page_title="Gerenciador de Rotas Inteligentes", page_icon="🚗", layout="centered")

# SEU TOKEN DO OPENROUTESERVICE INSERIDO DE FORMA FIXA E SEGURA
TOKEN_ORS = "eyJvcmciOiI1YjNjZTM1OTc4NTExMTAwMDFjZjYyNDgiLCJpZCI6ImJmN2ZlOWY0Yzk1NTQzZDFhZTVmNmM2NGQ2ZjY4ZmM5IiwiaCI6Im11cm11cjY0In0="

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

def geocode_arcgis(localidade):
    """Busca as coordenadas exatas dos municípios através do ArcGIS global"""
    url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(localidade)}&maxLocations=1"
    try:
        resposta = requests.get(url, timeout=10).json()
        if resposta.get('candidates'):
            ponto = resposta['candidates'][0]['location']
            return float(ponto['y']), float(ponto['x'])
    except:
        pass
    return None

def calcular_rota_menor_trajeto(origem, destino):
    """Calcula a rota terrestre rodoviária mais curta em KM e tempo exato usando o ORS oficial"""
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    origem_query = origem_clean if "," in origem_clean.lower() else f"{origem_clean}, Brasil"
    destino_query = destino_clean if "," in destino_clean.lower() else f"{destino_clean}, Brasil"

    # Link dinâmico do Google Maps gerado exatamente no padrão para abrir o trajeto certo
    link_maps = f"https://www.google.com/maps/dir/{requests.utils.quote(origem_query)}/{requests.utils.quote(destino_query)}"
    
    try:
        coords_o = geocode_arcgis(origem_query)
        coords_d = geocode_arcgis(destino_query)

        if coords_o and coords_d:
            lat1, lon1 = coords_o
            lat2, lon2 = coords_d
            
            # Linha Reta via Vincenty em KM
            dist_linha_reta = calcular_distancia_vincenty(lat1, lon1, lat2, lon2)

            # API Oficial do OpenRouteService configurada estritamente para o menor trajeto terrestre
            url = "https://api.openrouteservice.org/v2/directions/driving-car"
            payload = {
                "coordinates": [[lon1, lat1], [lon2, lat2]],
                "preference": "shortest",
                "units": "km"
            }
            headers = {
                'Accept': 'application/json',
                'Authorization': TOKEN_ORS,
                'Content-Type': 'application/json'
            }
            
            response = requests.post(url, json=payload, headers=headers, timeout=12)
            
            if response.status_code == 200:
                summary = response.json()['routes'][0]['summary']
                
                # Menor distância terrestre real em KM
                km_terrestre = round(summary['distance'], 2)
                
                # Menor tempo real convertido de segundos para h/min
                segundos = summary['duration']
                minutos_totais = round(segundos / 60)
                
                if minutos_totais < 60:
                    tempo_txt = f"{minutos_totais} min"
                else:
                    tempo_txt = f"{minutos_totais // 60}h {minutos_totais % 60}min"
                
                # Verificação simplificada de balsa baseada na sua lista de municípios do Pará
                envolve_balsa = "Não"
                cidades_balsa = ["moz", "almeirim", "soure", "salvaterra", "marajó", "cametá", "itaituba", "chaves", "gurupá"]
                if any(c in origem_clean.lower() or c in destino_clean.lower() for c in cidades_balsa):
                    envolve_balsa = "Sim"
                    
                return km_terrestre, tempo_txt, link_maps, envolve_balsa, dist_linha_reta

        return "Local não encontrado", "Local não encontrado", link_maps, "Não", 0.0
    except:
        return "Erro técnico", "Erro técnico", link_maps, "Não", 0.0

# --- INTERFACE VISUAL NO APP ---
st.title("🚗 Calculador de Rotas de Alta Precisão")
st.write("Envie sua planilha Excel contendo as colunas **Origem** e **Destino** para extrair os valores reais.")

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
            
            total_linhas = len(df)
            barra_progresso = st.progress(0)
            texto_status = st.empty()
            
            for index, linha in df.iterrows():
                origem = str(linha['Origem']).strip()
                destino = str(linha['Destino']).strip()
                
                if origem and destino and origem != 'nan' and destino != 'nan':
                    texto_status.text(f"Processando {index+1}/{total_linhas}: {origem} ➔ {destino}")
                    
                    km, tempo, link, balsa_status, linha_reta = calcular_rota_menor_trajeto(origem, destino)
                    
                    df.at[index, 'Distancia'] = km
                    df.at[index, 'Tempo'] = tempo
                    df.at[index, 'Link da Rota'] = link
                    df.at[index, 'Balsas'] = balsa_status
                    df.at[index, 'Linha Reta'] = linha_reta
                    
                    # Pausa de estabilização padrão
                    time.sleep(0.2)
                
                barra_progresso.progress((index + 1) / total_linhas)
            
            texto_status.text("✨ Processamento concluído com sucesso!")
            
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
                file_name="planilha_rotas_calculada.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
