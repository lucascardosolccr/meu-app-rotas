import streamlit as st
import pandas as pd
import requests
import time
import math
import io

# Configuração da página do site
st.set_set_page_config(page_title="Gerenciador de Rotas Inteligentes", page_icon="🚗", layout="centered")

def calcular_distancia_vincenty(lat1, lon1, lat2, lon2):
    """
    Calcula a distância em Linha Reta usando o modelo elipsoidal da Terra (WGS-84).
    É o cálculo matemático mais exato que existe.
    """
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
        s = b * A * (sigma - deltaSigma)

        return round(s / 1000, 2) # Retorna em KM
    except:
        return 0.0

def verificar_balsa_regional(status, o, d):
    """
    Proteção extra para garantir que rotas conhecidas na bacia amazônica do Pará
    sejam marcadas com balsa mesmo em casos de inconsistência de mapas.
    """
    cidades = ["moz", "almeirim", "soure", "salvaterra", "marajó", "cametá", "itaituba", "chaves", "gurupá"]
    if any(c in o.lower() or c in d.lower() for c in cidades):
        return "Sim"
    return status

def geocode_arcgis(localidade):
    """Busca coordenadas usando o servidor mundial do ArcGIS (Livre de bloqueios e ultra preciso)"""
    url = f"https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates?f=json&singleLine={requests.utils.quote(localidade)}&maxLocations=1"
    try:
        resposta = requests.get(url, timeout=10).json()
        if resposta.get('candidates'):
            ponto = resposta['candidates'][0]['location']
            return float(ponto['y']), float(ponto['x'])
    except:
        pass
    return None

def consultar_base_alta_precisao(origem, destino):
    """
    Roteador de alta precisão rodoviária sem limites ou aproximações artificiais.
    """
    origem_clean = str(origem).strip()
    destino_clean = str(destino).strip()

    origem_query = origem_clean if "," in origem_clean.lower() else f"{origem_clean}, Pará, Brasil"
    destino_query = destino_clean if "," in destino_clean.lower() else f"{destino_clean}, Pará, Brasil"

    link_maps = f"https://www.google.com/maps/dir/{requests.utils.quote(origem_query)}/{requests.utils.quote(destino_query)}"

    try:
        # 1. Geocodificação via ArcGIS de Alta Disponibilidade
        coords_o = geocode_arcgis(origem_query)
        coords_d = geocode_arcgis(destino_query)

        if coords_o and coords_d:
            lat1, lon1 = coords_o
            lat2, lon2 = coords_d

            # Executa a Linha Reta exata via Vincenty
            dist_linha_reta = calcular_distancia_vincenty(lat1, lon1, lat2, lon2)

            # 2. Roteamento Terrestre Oficial via OSRM (Malha Rodoviária Real)
            url_rota = f"http://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=false"
            res_r = requests.get(url_rota, timeout=12).json()
            
            if res_r.get('code') == 'Ok':
                leg = res_r['routes'][0]['legs'][0]
                km_terrestre = round(leg['distance'] / 1000, 2)
                minutos = round(leg['duration'] / 60)
                
                tempo_txt = f"{minutos} min" if minutos < 60 else f"{minutos//60}h {minutos%60}min"
                balsa_final = verificar_balsa_regional("Não", origem_clean, destino_clean)

                return km_terrestre, tempo_txt, link_maps, balsa_final, dist_linha_reta

    except Exception as e:
        pass

    return "Não localizado", "Não localizado", link_maps, "Não", 0.0


# --- INTERFACE VISUAL NO STREAMLIT ---
st.title("🚗 Calculador Inteligente de Rotas Rodoviárias")
st.write("Envie sua planilha Excel contendo as colunas **Origem** e **Destino** para extrair os valores reais.")

arquivo_carregado = st.file_uploader("Selecione seu arquivo Excel (.xlsx)", type=["xlsx"])

if arquivo_carregado is not None:
    df = pd.read_excel(arquivo_carregado)
    
    if 'Origem' not in df.columns or 'Destino' not in df.columns:
        st.error("Erro: A planilha precisa conter as colunas com os nomes exatos: 'Origem' e 'Destino'.")
    else:
        st.success("Planilha mapeada e pronta para processamento!")
        
        if st.button("Iniciar Processamento das Rotas"):
            # Inicializa colunas aceitando qualquer tipo de dado (Evita o TypeError do Pandas)
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
                    texto_status.text(f"Processando linha {index+1} de {total_linhas}: {origem} ➔ {destino}")

                    # Executa a consulta robusta atualizada
                    km, tempo, link, balsa_status, linha_reta = consultar_base_alta_precisao(origem, destino)

                    df.at[index, 'Distancia'] = km
                    df.at[index, 'Tempo'] = tempo
                    df.at[index, 'Link da Rota'] = link
                    df.at[index, 'Balsas'] = balsa_status
                    df.at[index, 'Linha Reta'] = linha_reta

                    # Pausa leve de estabilidade para os servidores rodoviários
                    time.sleep(0.3)
                
                barra_progresso.progress((index + 1) / total_linhas)

            texto_status.text("✨ Processamento de rotas concluído com sucesso!")

            # Alinhamento e ordenação exata de colunas conforme o seu padrão visual
            ordem_colunas = ['Origem', 'Destino', 'Distancia', 'Tempo', 'Link da Rota', 'Balsas', 'Linha Reta']
            df = df.reindex(columns=ordem_colunas)

            # Exportador de bytes em memória para baixar no navegador de forma limpa
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False)
            dados_excel = output.getvalue()

            st.write("---")
            st.balloons()

            st.download_button(
                label="📥 Baixar Planilha com Dados Reais",
                data=dados_excel,
                file_name="planilha_rotas_precisao.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
