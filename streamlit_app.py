import concurrent.futures
import diskcache as dc
from rapidfuzz import process, fuzz
from unidecode import unidecode
import re
import requests

# ==============================================================================
# ENGINE DE RESOLUÇÃO UNIVERSAL DE ENDEREÇOS (TOTALMENTE AGNÓSTICO)
# ==============================================================================

# Inicializa o Cache Local (Camada 10)
geocache = dc.Cache('./geocache_db')

class ResolutorUniversal:
    def __init__(self):
        self.headers = {'User-Agent': 'RotasInteligentesApp/2.0 (seu_email@dominio.com)'}
        
        # Dicionário Estrutural Universal (Focado em tipologia, não em localidade)
        self.dicionario_estrutural = [
            "AVENIDA", "RUA", "QUADRA", "CONJUNTO", "LOTE", "APARTAMENTO", 
            "BLOCO", "SETOR", "RODOVIA", "TRAVESSA", "PRACA", "CONDOMINIO", 
            "EDIFICIO", "FAZENDA", "CHACARA", "ESTRADA", "VILA", "DISTRITO",
            "RESIDENCIAL", "PARQUE", "ALAMEDA", "MARGINAL"
        ]
        
    def camada_1_limpeza(self, texto):
        """Limpeza, normalização Unicode e expansão de abreviações universais."""
        if not isinstance(texto, str): return ""
        t = unidecode(texto).upper().strip()
        t = re.sub(r'\s+', ' ', t) # Remove espaços duplicados
        
        abreviacoes_universais = {
            r'\bAV\b': 'AVENIDA', r'\bR\b': 'RUA', r'\bQD\b': 'QUADRA', 
            r'\bCJ\b': 'CONJUNTO', r'\bLT\b': 'LOTE', r'\bAPT\b': 'APARTAMENTO', 
            r'\bBL\b': 'BLOCO', r'\bST\b': 'SETOR', r'\bROD\b': 'RODOVIA',
            r'\bTRV\b': 'TRAVESSA', r'\bPRC\b': 'PRACA', r'\bCOND\b': 'CONDOMINIO',
            r'\bED\b': 'EDIFICIO', r'\bRES\b': 'RESIDENCIAL'
        }
        for padrao, substituto in abreviacoes_universais.items():
            t = re.sub(padrao, substituto, t)
        return t

    def camada_2_fuzzy_estrutural(self, texto):
        """
        Correção Inteligente (RapidFuzz) aplicada EXCLUSIVAMENTE a estruturas de endereço.
        Nomes de cidades e ruas com erro tipográfico (ex: 'tagutnga') são ignorados 
        aqui e corrigidos nativamente pelo Photon/Nominatim.
        """
        tokens = texto.split()
        corrigido = []
        for token in tokens:
            if len(token) > 4: # Só aplica fuzzy em palavras minimamente complexas
                # Compara o token apenas contra nosso dicionário de estruturas universais
                melhor_match = process.extractOne(token, self.dicionario_estrutural, scorer=fuzz.WRatio)
                
                # Exige um grau altíssimo de confiança (90%) para não corromper nomes de locais
                # Exemplo: Se o token for "Avenda", corrige para "AVENIDA"
                # Se o token for "Samabaia", o score será baixo e ele manterá "Samabaia" intacto.
                if melhor_match and melhor_match[1] > 90: 
                    corrigido.append(melhor_match[0])
                else:
                    corrigido.append(token)
            else:
                corrigido.append(token)
        return " ".join(corrigido)

    def camada_3_cep(self, cep):
        """Redundância de CEP (ViaCEP -> BrasilAPI)."""
        try:
            res = requests.get(f"https://viacep.com.br/ws/{cep}/json/", timeout=4).json()
            if "erro" not in res: return res
        except:
            pass
        try: # Fallback BrasilAPI
            res = requests.get(f"https://brasilapi.com.br/api/cep/v2/{cep}", timeout=4).json()
            if "street" in res:
                return {'logradouro': res['street'], 'bairro': res['neighborhood'], 'localidade': res['city'], 'uf': res['state'], 'cep': cep}
        except:
            pass
        return None

    def _consultar_photon(self, query):
        """Photon tem ElasticSearch nativo, é excelente para tolerância a erros (typos)."""
        try:
            res = requests.get(f"https://photon.komoot.io/api/?q={requests.utils.quote(query)}&limit=1", timeout=5).json()
            if res.get('features'):
                coords = res['features'][0]['geometry']['coordinates']
                props = res['features'][0]['properties']
                endereco = f"{props.get('street', props.get('name', ''))}, {props.get('city', '')}, {props.get('state', '')}".strip(" ,")
                return {"source": "Photon", "lat": coords[1], "lon": coords[0], "address": endereco}
        except: return None

    def _consultar_nominatim(self, query):
        """Nominatim tem a base mais completa de trilhas e fazendas via OSM."""
        try:
            res = requests.get(f"https://nominatim.openstreetmap.org/search?q={requests.utils.quote(query)}&format=json&limit=1", headers=self.headers, timeout=5).json()
            if res:
                return {"source": "Nominatim", "lat": float(res[0]['lat']), "lon": float(res[0]['lon']), "address": res[0]['display_name']}
        except: return None

    def camada_4_multi_fonte(self, query):
        """Executa consultas PARALELAS aos provedores globais."""
        candidatos = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futuro_photon = executor.submit(self._consultar_photon, query)
            futuro_nom = executor.submit(self._consultar_nominatim, query)
            
            for f in concurrent.futures.as_completed([futuro_photon, futuro_nom]):
                resultado = f.result()
                if resultado: candidatos.append(resultado)
        return candidatos

    def camada_5_poi_overpass(self, query):
        """
        Busca estruturada de POIs via Overpass API com Bounding Box Global.
        O Nominatim geralmente resolve, isso é uma redundância extrema.
        """
        # Identificadores universais de POI
        if any(k in query for k in ["HOSPITAL", "SHOPPING", "AEROPORTO", "UNIVERSIDADE", "ESCOLA", "FACULDADE"]):
            # Overpass query buscando o nome em qualquer lugar do Brasil
            overpass_query = f"""
            [out:json][timeout:10];
            area["ISO3166-1"="BR"][admin_level=2]->.searchArea;
            node["name"~"{query}",i](area.searchArea);
            out body 1;
            """
            try:
                res = requests.post("http://overpass-api.de/api/interpreter", data=overpass_query, timeout=8).json()
                if res.get('elements'):
                    el = res['elements'][0]
                    return {"source": "Overpass", "lat": el['lat'], "lon": el['lon'], "address": el.get('tags', {}).get('name', query)}
            except: pass
        return None

    def camada_8_9_consenso_score(self, candidatos, texto_original):
        """Algoritmo de Votação Espacial e Score de Confiança."""
        if not candidatos: return None, 0
        if len(candidatos) == 1: return candidatos[0], 60
        
        melhor_candidato = candidatos[0]
        maior_score = 0
        
        for i, c1 in enumerate(candidatos):
            score = 50 # Base
            for j, c2 in enumerate(candidatos):
                if i != j:
                    # Se implementou o math local (Vincenty), o consenso mede a proximidade global
                    dist = calcular_distancia_vincenty(c1['lat'], c1['lon'], c2['lat'], c2['lon'])
                    if dist <= 2.0:  # Consenso espacial (menor que 2km)
                        score += 30
            
            if len(c1['address'].split(',')) > 2: score += 20
            
            if score > maior_score:
                maior_score = score
                melhor_candidato = c1
                
        return melhor_candidato, min(maior_score, 100)

    def resolver(self, texto_bruto):
        """Orquestrador do Pipeline Agnóstico."""
        if texto_bruto in geocache:
            return geocache[texto_bruto]

        # Camadas 1 e 2
        texto_limpo = self.camada_1_limpeza(texto_bruto)
        texto_fuzzy = self.camada_2_fuzzy_estrutural(texto_limpo)
        
        # Camada 3 (CEP) - Expressão regular universal para CEPs brasileiros
        cep_match = re.search(r'\b\d{5}-?\d{3}\b', texto_bruto)
        if cep_match:
            cep_limpo = cep_match.group().replace("-", "")
            dados_cep = self.camada_3_cep(cep_limpo)
            if dados_cep:
                # O CEP ancora o dado em uma localidade correta antes das APIs
                texto_fuzzy = f"{dados_cep.get('logradouro','')} {dados_cep.get('bairro','')}, {dados_cep.get('localidade','')}, {dados_cep.get('uf','')}, Brasil".strip(" ,")

        # Camada 4 e 5 (Multi-fonte Paralela Universal)
        candidatos = self.camada_4_multi_fonte(texto_fuzzy)
        if not candidatos:
            poi = self.camada_5_poi_overpass(texto_fuzzy)
            if poi: candidatos.append(poi)

        # Camada 8 e 9 (Consenso e Confiança)
        resultado, score = self.camada_8_9_consenso_score(candidatos, texto_fuzzy)
        
        if resultado:
            tupla_final = (resultado['lat'], resultado['lon'], resultado['address'])
            geocache.set(texto_bruto, tupla_final, expire=2592000)
            return tupla_final
            
        return None

resolutor = ResolutorUniversal()
