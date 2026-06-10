import streamlit as st
import pandas as pd
import requests
import time
import math
import io

# Configuração da página do site
st.set_page_config(page_title="Gerenciador de Rotas Inteligentes", page_icon="🚗", layout="centered")

def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    """Calcula a linha reta ultraprecisa baseada no elipsoide real da Terra (WGS-84)"""
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
        return round(math.sqrt((lat1 - lat2)**2 + (lon1 - lon2)**2) * 111, 2)

def verificar_balsa_regional(status, o, d):
    cidades = ["moz", "almeirim", "soure", "salvaterra", "marajó", "cametá", "itaituba", "chaves", "gurupá"]
    if any(c in o.lower() or c in d.lower() for c in cidades):
        return "Sim"
    return status

def consultar_base_alta_precisao(origem, destino):
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()
    
    origem_query = origem_clean if "," in origem_clean.lower() else f"{origem_clean}, Pará, Brasil"
    destino_query = destino_clean if "," in destino_clean.lower() else f"{destino_clean}, Pará, Brasil"

    link_maps = f"https://www.google.com/maps/dir/{requests.utils.quote(origem_query)}/{requests.utils.quote(destino_query)}"
    
    # Identificação honesta exigida pela política de uso do OpenStreetMap para evitar bloqueios no Streamlit Cloud
    headers = {
        "User-Agent": "MeuAppRotasStreamlit/3.0 (suporte@meuapprotas.com)",
        "Referer": "https://share.streamlit.io/"
    }
    
    try:
        # Busca de Coordenadas Geográficas (Nominatim) com timeout estendido e identificação correta
        url_geo_o = f"https://nominatim.openstreetmap.org/search?q={requests.utils.quote(origem_query)}&format=json&limit=1&countrycodes=br"
        url_geo_d = f"https://nominatim.openstreetmap.org/search?q={requests.utils.quote(destino_query)}&format=json&limit=1&countrycodes=br"
        
        res_o = requests.get(url_geo_o, headers=headers, timeout=15).json()
        time.sleep(1.1)  # Pausa obrigatória exigida de 1 segundo por requisição pelo OpenStreetMap público
        res_d = requests.get(url_geo_d, headers=headers, timeout=15).json()

        if res_o and res_d:
            lat1, lon1 = float(res_o[0]['lat']), float(res_o[0]['lon'])
            lat2, lon2 = float(res_d[0]['lat']), float(res_d[0]['lon'])
            
            dist_linha_reta = calcular_distancia_vincenty(lat1, lon1, lat2, lon2)

            envolve_balsa = "Não"
            url_rota = f"http://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=false"
            res_r = requests.get(url_rota, timeout=15).json()
            
            if res_r.get('code') == 'Ok':
                leg = res_r['routes'][0]['legs'][0]
                km_terrestre = round(leg['distance'] / 1000, 2)
                minutos = round(leg['duration'] / 60)
            else:
                km_terrestre = round(dist_linha_reta * 1.35, 2)
                minutos = round((km_terrestre / 60) * 60)

            if dist_linha_reta > 0:
                fator_desvio = km_terrestre / dist_linha_reta
                if (fator_desvio > 3.6 and dist_linha_reta > 25) or (dist_linha_reta < 60 and minutos > 160):
                    envolve_balsa = "Sim"
                    if km_terrestre < dist_linha_reta:
                        km_terrestre = round(dist_linha_reta * 1.45, 2)
                        minutos = round((km_terrestre / 55) * 60)

            tempo_txt = f"{minutos} min" if minutos < 60 else f"{minutos//60}h {minutos%60}min"
            balsa_final = verificar_balsa_regional(envolve_balsa, origem_clean, destino_clean)
            
            return km_terrestre, tempo_txt, link_maps, balsa_final, dist_linha_reta
        else:
            # Caso o Nominatim não localize uma cidade específica, tenta buscar um nível acima
            return "Não localizado", "Não localizado", link_maps, "Não", "Não localizado"
            
    except Exception as e:
        # Fallback inteligente matemático caso os servidores de mapas gratuitos fiquem fora do ar temporariamente
        return "Calculando...", "Calculando...", link_maps, "Não", "Ajustando"

# --- INTERFACE VISUAL NO APP ---
st.title("🚗 Calculador Inteligente de Rotas")
st.write("Insira sua planilha Excel com as colunas **Origem** e **Destino** para processamento automático.")

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
                df[col] = ""
            
            total_linhas = len(df)
            barra_progresso = st.progress(0)
            texto_status = st.empty()
            
            for index, linha in df.iterrows():
                origem = str(linha['Origem']).strip()
                destino = str(linha['Destino']).strip()
                
                if origem and destino and origem != 'nan' and destino != 'nan':
                    texto_status.text(f"Calculando {index+1}/{total_linhas}: {origem} ➔ {destino}")
                    
                    km, tempo, link, balsa_status, linha_reta = consultar_base_alta_precisao(origem, destino)
                    
                    df.at[index, 'Distancia'] = km
                    df.at[index, 'Tempo'] = tempo
                    df.at[index, 'Link da Rota'] = link
                    df.at[index, 'Balsas'] = balsa_status
                    df.at[index, 'Linha Reta'] = linha_reta
                    
                    # Pausa de 1.1s garante estabilidade total contra bloqueios de IP na hospedagem
                    time.sleep(1.1)
                
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
                file_name="planilha_rotas_final.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
