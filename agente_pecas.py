#!/usr/bin/env python3
"""
Assistente de peças OEM - versão conversacional com menu de detalhes
- Busca por veículo, descrição, código, OEM
- Mostra ano do veículo nas opções
- Menu após detalhes: 1 aplicação, 2 similares, 3 nova busca
"""

import os
import re
import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from collections import deque
from dotenv import load_dotenv
from rapidfuzz import fuzz

# ============================================================
# CONFIGURAÇÕES
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

EXCEL_PATH = BASE_DIR / "data" / "CATALOGO_AUTOFLEX_BD_v1-3.xlsx"
DB_PATH = BASE_DIR / "autoflex_catalog.db"

# ============================================================
# CARREGAMENTO DO EXCEL (para fuzzy)
# ============================================================
try:
    df = pd.read_excel(EXCEL_PATH, sheet_name="Catálogo Mestre")
    print(f"✅ Base carregada: {EXCEL_PATH}")
    print(f"📦 Total peças: {len(df)}")
except Exception as e:
    print(f"❌ Erro ao carregar Excel: {e}")
    exit(1)

df["sku_str"] = df["SKU Autoflex"].fillna("").astype(str).str.strip()
df["codigo_oem_str"] = df["Código OEM"].fillna("").astype(str).str.strip()
df["descricao_lower"] = df["Descrição"].fillna("").astype(str).str.lower()
df["montadora_str"] = df["Montadora"].fillna("").astype(str).str.strip()
df["grupo_str"] = df["Grupo"].fillna("").astype(str).str.strip()
df["veiculo_str"] = df["Veículo"].fillna("").astype(str).str.strip()

df["busca_texto"] = df.apply(
    lambda row: " ".join([
        str(row.get("SKU Autoflex", "")),
        str(row.get("Código OEM", "")),
        str(row.get("Descrição", "")),
        str(row.get("Montadora", "")),
        str(row.get("Grupo", "")),
        str(row.get("Veículo", "")),
    ]).lower(),
    axis=1
)

# ============================================================
# BANCO SQLITE
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            sku_autoflex TEXT PRIMARY KEY,
            codigo_oem TEXT,
            descricao TEXT,
            veiculo TEXT,
            montadora TEXT,
            linha TEXT,
            grupo TEXT,
            ncm TEXT,
            ipi_percent TEXT,
            codigo_barras TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fitment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku_autoflex TEXT,
            montadora TEXT,
            modelo TEXT,
            ano_inicio INTEGER,
            ano_fim INTEGER,
            motor_versao TEXT,
            confidence REAL,
            FOREIGN KEY(sku_autoflex) REFERENCES products(sku_autoflex)
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_prod_sku ON products(sku_autoflex)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_prod_oem ON products(codigo_oem)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_prod_desc ON products(descricao)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fit_modelo ON fitment(modelo)")
    conn.commit()
    return conn

def import_excel_to_sqlite():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM products")
    if cursor.fetchone()[0] > 0:
        print("✅ Banco SQLite já populado. Pulando importação.")
        conn.close()
        return

    print("📥 Importando Catálogo Mestre...")
    df_prod = pd.read_excel(EXCEL_PATH, sheet_name="Catálogo Mestre")
    for _, row in df_prod.iterrows():
        cursor.execute("""
            INSERT OR REPLACE INTO products
            (sku_autoflex, codigo_oem, descricao, veiculo, montadora, linha, grupo, ncm, ipi_percent, codigo_barras)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(row.get("SKU Autoflex", "")).strip(),
            str(row.get("Código OEM", "")).strip(),
            str(row.get("Descrição", "")).strip(),
            str(row.get("Veículo", "")).strip(),
            str(row.get("Montadora", "")).strip(),
            str(row.get("Linha", "")).strip(),
            str(row.get("Grupo", "")).strip(),
            str(row.get("NCM", "")).strip(),
            str(row.get("IPI %", "")).strip(),
            str(row.get("Cód. Barras", "")).strip(),
        ))
    conn.commit()
    print(f"✅ {len(df_prod)} produtos importados.")

    print("📥 Importando Fitment...")
    try:
        df_fit = pd.read_excel(EXCEL_PATH, sheet_name="Fitment Completo")
        total = 0
        for _, row in df_fit.iterrows():
            sku = str(row.get("SKU", "")).strip()
            cursor.execute("SELECT 1 FROM products WHERE sku_autoflex = ?", (sku,))
            if not cursor.fetchone():
                continue
            ano_inicio = row.get("Ano Início")
            ano_fim = row.get("Ano Fim")
            cursor.execute("""
                INSERT INTO fitment
                (sku_autoflex, montadora, modelo, ano_inicio, ano_fim, motor_versao, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                sku,
                str(row.get("Montadora", "")).strip(),
                str(row.get("Modelo", "")).strip(),
                int(ano_inicio) if pd.notna(ano_inicio) else None,
                int(ano_fim) if pd.notna(ano_fim) else None,
                str(row.get("Motor/Versão", "")).strip(),
                float(row.get("Confidence", 0.6)) if pd.notna(row.get("Confidence", None)) else 0.6
            ))
            total += 1
        conn.commit()
        print(f"✅ {total} aplicações importadas.")
    except Exception as e:
        print(f"⚠️ Erro ao importar fitment: {e}")
    conn.close()

# ============================================================
# NORMALIZAÇÃO E UTILITÁRIOS
# ============================================================
def limpar_texto(texto):
    return str(texto).lower().strip()

def normalizar_codigo(codigo):
    return re.sub(r"[^a-zA-Z0-9]", "", str(codigo)).lower()

def eh_codigo_puro(texto):
    texto_limpo = texto.strip()
    return bool(re.fullmatch(r"[a-zA-Z0-9\-.]{4,}", texto_limpo)) and len(texto_limpo.split()) == 1

def detectar_intencao(texto):
    texto = texto.lower().strip()
    if eh_codigo_puro(texto):
        return "codigo"
    if "ref" in texto or "oem" in texto:
        return "referencia"
    modelos = obter_modelos_conhecidos()
    if any(modelo in texto for modelo in modelos):
        return "veiculo"
    return "descricao"

def obter_modelos_conhecidos():
    return [
        "palio", "uno", "strada", "siena", "punto", "linea", "idea",
        "doblo", "doblô", "argo", "cronos", "mobi", "fiorino",
        "gol", "fox", "voyage", "saveiro", "virtus",
        "onix", "prisma", "cobalt", "spin", "corsa", "celta",
        "agile", "montana", "toro", "renegade", "compass",
        "hilux", "corolla", "hb20", "civic", "fit"
    ]

def extrair_termos_modelo_ano(texto):
    texto = texto.lower().strip()
    ignorar = {
        "quero", "preciso", "procuro", "tem", "vc", "voce", "você",
        "me", "manda", "ver", "uma", "um", "o", "a", "os", "as",
        "de", "do", "da", "dos", "das", "para", "pra", "com",
        "peça", "peca", "produto"
    }
    modelos = obter_modelos_conhecidos()
    modelo = None
    for item in modelos:
        if re.search(rf"\b{re.escape(item)}\b", texto):
            modelo = item
            break
    ano = None
    ano_match = re.search(r"\b(19|20)\d{2}\b", texto)
    if ano_match:
        ano = ano_match.group()
    palavras = re.findall(r"[a-zA-ZÀ-ÿ0-9]+", texto)
    termos = []
    for palavra in palavras:
        if palavra in ignorar:
            continue
        if modelo and palavra == modelo:
            continue
        if ano and palavra == ano:
            continue
        if len(palavra) <= 2:
            continue
        termos.append(palavra)
    return termos, modelo, ano

# ============================================================
# BUSCAS
# ============================================================
def row_para_peca(row):
    return {
        "sku": str(row[0]).strip(),
        "codigo_oem": str(row[1]).strip() if len(row) > 1 else "",
        "descricao": str(row[2]).strip() if len(row) > 2 else "",
        "veiculo": str(row[3]).strip() if len(row) > 3 else "",
        "montadora": str(row[4]).strip() if len(row) > 4 else "",
        "grupo": str(row[5]).strip() if len(row) > 5 else "",
        "ano_inicio": None,
        "ano_fim": None,
    }

def enriquecer_com_fitment(pecas):
    if not pecas:
        return pecas
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    for p in pecas:
        sku = p["sku"]
        cursor.execute("""
            SELECT ano_inicio, ano_fim FROM fitment
            WHERE sku_autoflex = ?
            ORDER BY confidence DESC, ano_inicio
            LIMIT 1
        """, (sku,))
        row = cursor.fetchone()
        if row:
            p["ano_inicio"] = row[0]
            p["ano_fim"] = row[1]
    conn.close()
    return pecas

def buscar_por_codigo(codigo):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    codigo_norm = normalizar_codigo(codigo)
    cursor.execute("""
        SELECT sku_autoflex, codigo_oem, descricao, veiculo, montadora, grupo
        FROM products
        WHERE LOWER(REPLACE(REPLACE(sku_autoflex, '.', ''), '-', '')) = ?
           OR LOWER(REPLACE(REPLACE(codigo_oem, '.', ''), '-', '')) = ?
           OR codigo_oem LIKE ? OR sku_autoflex LIKE ?
        LIMIT 10
    """, (codigo_norm, codigo_norm, f"%{codigo}%", f"%{codigo}%"))
    rows = cursor.fetchall()
    conn.close()
    pecas = [row_para_peca(row) for row in rows]
    return enriquecer_com_fitment(pecas)

def buscar_por_referencia(texto):
    match = re.search(r"(ref|rf|oem)\s*[:\-]?\s*([a-zA-Z0-9\-.]+)", texto.lower())
    if match:
        return buscar_por_codigo(match.group(2))
    return []

def buscar_por_veiculo(modelo=None, ano=None, termos=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    query = """
        SELECT
            p.sku_autoflex, p.codigo_oem, p.descricao, p.veiculo, p.montadora, p.grupo,
            f.modelo, f.ano_inicio, f.ano_fim, f.motor_versao, f.confidence
        FROM fitment f
        JOIN products p ON p.sku_autoflex = f.sku_autoflex
        WHERE 1=1
    """
    params = []
    if modelo:
        query += " AND LOWER(f.modelo) LIKE ?"
        params.append(f"%{modelo.lower()}%")
    if ano:
        query += " AND (f.ano_inicio <= ? AND (f.ano_fim >= ? OR f.ano_fim IS NULL))"
        params.extend([int(ano), int(ano)])
    query += " ORDER BY f.confidence DESC LIMIT 30"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    pecas = []
    for r in rows:
        peca = {
            "sku": str(r[0]).strip(),
            "codigo_oem": str(r[1]).strip(),
            "descricao": str(r[2]).strip(),
            "veiculo": str(r[3]).strip(),
            "montadora": str(r[4]).strip(),
            "grupo": str(r[5]).strip(),
            "modelo": str(r[6]).strip(),
            "ano_inicio": r[7],
            "ano_fim": r[8],
            "motor_versao": str(r[9]).strip(),
            "confidence": r[10],
        }
        if termos:
            desc = peca["descricao"].lower()
            if not any(t in desc for t in termos):
                continue
        pecas.append(peca)
    return pecas[:10]

def buscar_por_descricao(termos, modelo=None, ano=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    query = """
        SELECT sku_autoflex, codigo_oem, descricao, veiculo, montadora, grupo
        FROM products
        WHERE 1=1
    """
    params = []
    for t in termos:
        query += " AND LOWER(descricao) LIKE ?"
        params.append(f"%{t.lower()}%")
    if modelo:
        query += " AND LOWER(veiculo) LIKE ?"
        params.append(f"%{modelo.lower()}%")
    query += " LIMIT 20"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    pecas = [row_para_peca(row) for row in rows]
    return enriquecer_com_fitment(pecas)

def buscar_fuzzy(consulta, limite=10):
    consulta = consulta.lower().strip()
    resultados = []
    for _, row in df.iterrows():
        texto = str(row["busca_texto"]).lower()
        score = max(
            fuzz.partial_ratio(consulta, texto),
            fuzz.token_sort_ratio(consulta, texto),
            fuzz.token_set_ratio(consulta, texto)
        )
        if score >= 65:
            resultados.append({
                "score": score,
                "sku": str(row.get("SKU Autoflex", "")).strip(),
                "codigo_oem": str(row.get("Código OEM", "")).strip(),
                "descricao": str(row.get("Descrição", "")).strip(),
                "veiculo": str(row.get("Veículo", "")).strip(),
                "montadora": str(row.get("Montadora", "")).strip(),
                "grupo": str(row.get("Grupo", "")).strip(),
                "ano_inicio": None,
                "ano_fim": None,
            })
    resultados.sort(key=lambda x: x["score"], reverse=True)
    pecas = resultados[:limite]
    return enriquecer_com_fitment(pecas)

# ============================================================
# FORMATAÇÃO
# ============================================================
def valor_limpo(valor):
    if valor is None:
        return ""
    valor = str(valor).strip()
    if valor.lower() in ["nan", "none", "null"]:
        return ""
    return valor

def formatar_resumo_peca(peca, numero=None):
    sku = valor_limpo(peca.get("sku"))
    descricao = valor_limpo(peca.get("descricao"))
    oem = valor_limpo(peca.get("codigo_oem"))
    ano_inicio = peca.get("ano_inicio")
    ano_fim = peca.get("ano_fim")
    prefixo = f"{numero}️⃣ " if numero else ""
    texto = f"{prefixo}{descricao}\n"
    texto += f"SKU: {sku}"
    if oem:
        texto += f" | OEM: {oem}"
    if ano_inicio:
        texto += f" | Ano: {ano_inicio} - {ano_fim or 'atual'}"
    return texto

def formatar_detalhe_peca(peca):
    sku = valor_limpo(peca.get("sku"))
    oem = valor_limpo(peca.get("codigo_oem"))
    descricao = valor_limpo(peca.get("descricao"))
    veiculo = valor_limpo(peca.get("veiculo"))
    montadora = valor_limpo(peca.get("montadora"))
    grupo = valor_limpo(peca.get("grupo"))
    modelo = valor_limpo(peca.get("modelo"))
    motor = valor_limpo(peca.get("motor_versao"))
    ano_inicio = peca.get("ano_inicio")
    ano_fim = peca.get("ano_fim")
    texto = "✅ Peça encontrada\n\n"
    texto += f"🔧 {descricao}\n\n"
    texto += f"SKU Autoflex: {sku}\n"
    if oem:
        texto += f"OEM / Referência: {oem}\n"
    if montadora:
        texto += f"Montadora: {montadora}\n"
    if grupo:
        texto += f"Grupo: {grupo}\n"
    if veiculo:
        texto += f"Aplicação: {veiculo}\n"
    if modelo:
        texto += f"Modelo: {modelo}\n"
    if motor:
        texto += f"Motor/versão: {motor}\n"
    if ano_inicio:
        texto += f"Ano: {ano_inicio} até {ano_fim or 'atual'}\n"
    texto += "\nVocê pode pedir:\n1️⃣ aplicação completa\n2️⃣ peças similares\n3️⃣ nova busca"
    return texto

def formatar_lista_pecas(pecas):
    texto = f"Encontrei {len(pecas)} opção(ões):\n\n"
    for i, peca in enumerate(pecas[:9], 1):
        texto += formatar_resumo_peca(peca, i)
        texto += "\n\n"
    texto += "Responda com o número, SKU, OEM ou mande uma nova busca."
    return texto

def formatar_sem_resultado(consulta):
    return (f"Não encontrei uma peça com essa busca.\n\nBusca feita: {consulta}\n\n"
            "Tente assim:\n• cabo embreagem palio 2006\n• 4002\n• oem 55204912\n• amortecedor uno")

# ============================================================
# MEMÓRIA
# ============================================================
class MemoriaConversa:
    def __init__(self, limite=20):
        self.historico = deque(maxlen=limite)
        self.contexto = {"ultimo_modelo": None, "ultimo_ano": None, "ultimos_termos": [],
                         "ultima_peca": None, "ultima_intencao": None, "categoria": None,
                         "montadora": None, "timestamp": None}
    def salvar_busca(self, consulta, resposta, pecas=None):
        self.historico.append({"hora": datetime.now().strftime("%H:%M:%S"),
                               "consulta": consulta, "resposta": resposta[:200], "pecas": pecas or []})
    def atualizar(self, modelo=None, ano=None, termos=None, peca=None, intencao=None, categoria=None, montadora=None):
        if modelo: self.contexto["ultimo_modelo"] = modelo
        if ano: self.contexto["ultimo_ano"] = ano
        if termos: self.contexto["ultimos_termos"] = termos
        if peca: self.contexto["ultima_peca"] = peca
        if intencao: self.contexto["ultima_intencao"] = intencao
        if categoria: self.contexto["categoria"] = categoria
        if montadora: self.contexto["montadora"] = montadora
        self.contexto["timestamp"] = datetime.now()
    def obter(self): return self.contexto.copy()
    def limpar(self):
        self.contexto = {"ultimo_modelo": None, "ultimo_ano": None, "ultimos_termos": [],
                         "ultima_peca": None, "ultima_intencao": None, "categoria": None,
                         "montadora": None, "timestamp": None}
    def historico_texto(self):
        if not self.historico: return "Nenhum histórico ainda."
        texto = "Últimas buscas:\n\n"
        for item in list(self.historico)[-5:]:
            texto += f"- {item['hora']} | {item['consulta']}\n"
        return texto

# ============================================================
# AGENTE PRINCIPAL (com menu de detalhes corrigido)
# ============================================================
class AgentePecas:
    def __init__(self):
        self.memoria = MemoriaConversa()
        self.aguardando_escolha = False   # esperando número da lista
        self.aguardando_acao_detalhe = False  # esperando 1,2,3 no menu de detalhes
        self.opcoes_atuais = []
        self.peca_atual = None

    def processar(self, consulta):
        consulta = consulta.strip()
        if not consulta:
            return "Digite uma peça, código, OEM ou veículo."

        comando = consulta.lower()
        if comando == "/ajuda": return self.ajuda()
        if comando == "/historico": return self.memoria.historico_texto()
        if comando == "/limpar":
            self._resetar_estados()
            self.memoria.limpar()
            return "Contexto limpo."
        if comando == "sair":
            self._resetar_estados()
            return "Busca cancelada."

        # Estado: menu de detalhes (1,2,3)
        if self.aguardando_acao_detalhe:
            if comando == "1":
                self.aguardando_acao_detalhe = False
                return self.responder_aplicacao(self.peca_atual)
            elif comando == "2":
                self.aguardando_acao_detalhe = False
                return self.buscar_similares(self.peca_atual)
            elif comando == "3":
                self._resetar_estados()
                return "🔍 Nova busca. Digite o que deseja."
            else:
                # Se digitar algo que não é 1,2,3, pode ser nova busca
                self._resetar_estados()
                # re-processa como nova consulta
                return self.processar(consulta)

        # Estado: esperando escolha da lista
        if self.aguardando_escolha:
            resposta_escolha = self.tentar_processar_escolha(consulta)
            if resposta_escolha:
                return resposta_escolha
            # Se não foi uma escolha válida, cancela e trata como nova busca
            self._resetar_estados()
            return self.processar(consulta)

        # --- Nova busca normal ---
        intencao = detectar_intencao(consulta)
        termos, modelo, ano = extrair_termos_modelo_ano(consulta)
        contexto = self.memoria.obter()
        # contexto automático
        if not modelo and contexto["ultimo_modelo"] and len(consulta.split()) <= 3:
            modelo = contexto["ultimo_modelo"]
        if not ano and contexto["ultimo_ano"] and modelo:
            ano = contexto["ultimo_ano"]
        if not termos and contexto["ultimos_termos"] and modelo:
            termos = contexto["ultimos_termos"]
        self.memoria.atualizar(modelo=modelo, ano=ano, termos=termos, intencao=intencao)

        pecas = []
        if intencao == "codigo":
            pecas = buscar_por_codigo(consulta)
        elif intencao == "referencia":
            pecas = buscar_por_referencia(consulta)
        elif intencao == "veiculo":
            pecas = buscar_por_veiculo(modelo=modelo, ano=ano, termos=termos)
            if not pecas and termos:
                pecas = buscar_por_descricao(termos, modelo=modelo, ano=ano)
        else:
            if termos:
                pecas = buscar_por_descricao(termos, modelo=modelo, ano=ano)

        if not pecas:
            pecas = buscar_fuzzy(consulta)

        if not pecas:
            resposta = formatar_sem_resultado(consulta)
            self.memoria.salvar_busca(consulta, resposta)
            return resposta

        if len(pecas) == 1:
            peca = pecas[0]
            self.memoria.atualizar(peca=peca)
            self.peca_atual = peca
            self.aguardando_acao_detalhe = True
            resposta = formatar_detalhe_peca(peca)
            self.memoria.salvar_busca(consulta, resposta, [peca])
            return resposta

        self.opcoes_atuais = pecas[:10]
        self.aguardando_escolha = True
        resposta = formatar_lista_pecas(self.opcoes_atuais)
        self.memoria.salvar_busca(consulta, resposta, self.opcoes_atuais)
        return resposta

    def tentar_processar_escolha(self, consulta):
        texto = consulta.lower().strip()
        # Escolha por número
        if texto.isdigit():
            numero = int(texto)
            if 1 <= numero <= len(self.opcoes_atuais):
                peca = self.opcoes_atuais[numero - 1]
                self.memoria.atualizar(peca=peca)
                self.peca_atual = peca
                self.aguardando_escolha = False
                self.aguardando_acao_detalhe = True
                return formatar_detalhe_peca(peca)
            else:
                return f"❌ Número inválido. Digite de 1 a {len(self.opcoes_atuais)} ou mande uma nova busca."
        # Escolha por SKU/OEM
        if eh_codigo_puro(texto):
            for peca in self.opcoes_atuais:
                sku_norm = normalizar_codigo(peca.get("sku", ""))
                oem_norm = normalizar_codigo(peca.get("codigo_oem", ""))
                if normalizar_codigo(texto) in [sku_norm, oem_norm]:
                    self.memoria.atualizar(peca=peca)
                    self.peca_atual = peca
                    self.aguardando_escolha = False
                    self.aguardando_acao_detalhe = True
                    return formatar_detalhe_peca(peca)
            # Se não achou na lista, tenta busca global
            pecas = buscar_por_codigo(texto)
            if pecas:
                if len(pecas) == 1:
                    peca = pecas[0]
                    self.memoria.atualizar(peca=peca)
                    self.peca_atual = peca
                    self.aguardando_escolha = False
                    self.aguardando_acao_detalhe = True
                    return formatar_detalhe_peca(peca)
                else:
                    self.opcoes_atuais = pecas[:10]
                    self.aguardando_escolha = True
                    return formatar_lista_pecas(self.opcoes_atuais)
        # Se não foi escolha, retorna None (será tratado como nova busca)
        return None

    def _resetar_estados(self):
        self.aguardando_escolha = False
        self.aguardando_acao_detalhe = False
        self.opcoes_atuais = []
        self.peca_atual = None

    def responder_aplicacao(self, peca):
        sku = peca.get("sku")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT modelo, ano_inicio, ano_fim, motor_versao
            FROM fitment
            WHERE sku_autoflex = ?
            ORDER BY modelo, ano_inicio
            LIMIT 20
        """, (sku,))
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return "Não encontrei aplicação detalhada para essa peça."
        texto = "Aplicação encontrada:\n\n"
        for row in rows:
            modelo, ano_inicio, ano_fim, motor = row
            texto += f"• {modelo}"
            if ano_inicio:
                texto += f" {ano_inicio}-{ano_fim or 'atual'}"
            if motor:
                texto += f" | {motor}"
            texto += "\n"
        # Volta para o menu de detalhes
        self.aguardando_acao_detalhe = True
        return texto + "\n\nDigite 1, 2 ou 3 para continuar."

    def buscar_similares(self, peca):
        descricao = peca.get("descricao", "")
        termos, modelo, ano = extrair_termos_modelo_ano(descricao)
        if not termos:
            return "Não consegui identificar similares para essa peça."
        similares = buscar_por_descricao(termos[:2])
        similares = [p for p in similares if p.get("sku") != peca.get("sku")]
        if not similares:
            return "Não encontrei peças similares."
        self.opcoes_atuais = similares[:10]
        self.aguardando_escolha = True
        self.aguardando_acao_detalhe = False
        return formatar_lista_pecas(self.opcoes_atuais)

    def ajuda(self):
        return ("Você pode buscar assim:\n\n"
                "• cabo embreagem palio 2006\n"
                "• palio 2008\n"
                "• 4002\n"
                "• oem 55204912\n"
                "• ref 55204912\n\n"
                "Depois de uma lista, responda com:\n"
                "• número da opção\n"
                "• SKU\n"
                "• OEM\n\n"
                "Após ver os detalhes, digite:\n"
                "1 para ver aplicação completa\n"
                "2 para peças similares\n"
                "3 para nova busca")

# ============================================================
# MAIN
# ============================================================
def main():
    print("="*60)
    print("🧠 AGENTE DE PEÇAS OEM - Conversacional (com anos)")
    print("="*60)
    print("Digite uma peça, veículo, SKU ou OEM.")
    print("Exemplos:")
    print("• cabo embreagem palio 2006")
    print("• 4002")
    print("• oem 55204912")
    print("• /ajuda")
    print("="*60)

    init_db()
    import_excel_to_sqlite()

    agente = AgentePecas()
    while True:
        consulta = input("\nVocê: ").strip()
        if not consulta:
            continue
        if consulta.lower() in ["fechar", "exit", "quit"]:
            print("\nAté logo!")
            break
        resposta = agente.processar(consulta)
        print(f"\n{resposta}")

if __name__ == "__main__":
    main()