#!/usr/bin/env python3
"""
build_db.py — Cria o banco SQLite unificando as duas planilhas:
  1. CATALOGO_AUTOFLEX_BD_v1-3.xlsx  (fitment, cross-refs, preços)
  2. OEM_PARTS_-_TABELA_DE_PRODUTOS_-_29-04-26.xlsx  (estoque, aplicação, montadora OEM)

Uso:
    python build_db.py
    python build_db.py /caminho/catalogo.xlsx /caminho/oem_parts.xlsx
"""

import sqlite3, sys, re
import pandas as pd
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH  = BASE_DIR / "data" / "autoflex_catalog.db"

XLSX1_CANDIDATES = [
    BASE_DIR / "CATALOGO_AUTOFLEX_BD_v1-3.xlsx",
    BASE_DIR / "data" / "CATALOGO_AUTOFLEX_BD_v1-3.xlsx",
]
XLSX2_CANDIDATES = [
    BASE_DIR / "OEM_PARTS_-_TABELA_DE_PRODUTOS_-_29-04-26.xlsx",
    BASE_DIR / "data" / "OEM_PARTS_-_TABELA_DE_PRODUTOS_-_29-04-26.xlsx",
]

def find_xlsx(candidates, override=None):
    if override:
        p = Path(override)
        if p.exists(): return p
        raise FileNotFoundError(f"Não encontrado: {override}")
    for c in candidates:
        if c.exists(): return c
    return None

def build(xlsx1: Path = None, xlsx2: Path = None):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))

    # ══════════════════════════════════════════════════════
    # PLANILHA 1 — CATALOGO_AUTOFLEX_BD
    # ══════════════════════════════════════════════════════
    if xlsx1 and xlsx1.exists():
        print(f"📖 Lendo catálogo: {xlsx1.name}")

        # ── Catálogo Mestre → products ─────────────────────
        df = pd.read_excel(xlsx1, sheet_name="Catálogo Mestre", dtype=str).fillna("")
        df.columns = [c.strip() for c in df.columns]
        df = df.rename(columns={
            "SKU Autoflex": "sku_autoflex",
            "Código OEM":   "codigo_oem",
            "Descrição":    "descricao",
            "Veículo":      "veiculo",
            "Montadora":    "montadora",
            "Linha":        "linha",
            "Grupo":        "grupo",
            "NCM":          "ncm",
            "IPI %":        "ipi",
            "Cód. Barras":  "cod_barras",
            "Obs":          "obs",
        })
        df["fonte"] = "catalogo"
        df.to_sql("products", con, if_exists="replace", index=False)
        print(f"  ✅ products: {len(df)} linhas")

        # ── Fitment → fitment ──────────────────────────────
        df = pd.read_excel(xlsx1, sheet_name="Fitment Completo", dtype=str).fillna("")
        df.columns = [c.strip() for c in df.columns]
        df = df.rename(columns={
            "SKU":                  "sku",
            "Descrição do produto": "descricao",
            "Montadora":            "montadora",
            "Modelo":               "modelo",
            "Ano Início":           "ano_ini",
            "Ano Fim":              "ano_fim",
            "Motor/Versão":         "motor",
            "Linha":                "linha",
            "Confidence":           "confidence",
        })
        df.to_sql("fitment", con, if_exists="replace", index=False)
        print(f"  ✅ fitment: {len(df)} linhas")

        # ── Cross-References ───────────────────────────────
        df = pd.read_excel(xlsx1, sheet_name="Cross-References", dtype=str).fillna("")
        df.columns = [c.strip() for c in df.columns]
        df = df.rename(columns={"SKU Autoflex": "sku_autoflex", "Descrição": "descricao"})
        df.to_sql("cross_refs", con, if_exists="replace", index=False)
        print(f"  ✅ cross_refs: {len(df)} linhas")

        # ── Preços ─────────────────────────────────────────
        df = pd.read_excel(xlsx1, sheet_name="Preços por UF", dtype=str).fillna("")
        df.columns = [c.strip() for c in df.columns]
        df = df.rename(columns={"SKU": "sku", "Descrição": "descricao"})
        df.to_sql("precos", con, if_exists="replace", index=False)
        print(f"  ✅ precos: {len(df)} linhas")
    else:
        print("⚠️  CATALOGO_AUTOFLEX_BD não encontrado — pulando.")

    # ══════════════════════════════════════════════════════
    # PLANILHA 2 — OEM_PARTS (estoque + aplicação)
    # ══════════════════════════════════════════════════════
    if xlsx2 and xlsx2.exists():
        print(f"\n📖 Lendo OEM Parts: {xlsx2.name}")

        df2 = pd.read_excel(xlsx2, sheet_name="tabelaProdutosReport", dtype=str).fillna("")
        df2.columns = [c.strip() for c in df2.columns]
        df2 = df2.rename(columns={
            "Código":                "codigo",
            "Ud":                    "unidade",
            "Descrição dos Produtos":"descricao",
            "Aplicação":             "aplicacao",
            "NCM":                   "ncm",
            "Fabricante":            "fabricante",
            "Ref. ":                 "ref_autoflex",
            "Montadora":             "oem_montadora",
            "Local":                 "local",
            "Última Ent.":           "ultima_entrada",
            "Quantidade":            "quantidade",
            "Reservada":             "reservada",
            "Contagem":              "contagem",
            "Seção":                 "secao",
            "Sub Seção":             "subsecao",
        })

        # Salva tabela completa de estoque
        df2.to_sql("oem_parts", con, if_exists="replace", index=False)
        print(f"  ✅ oem_parts: {len(df2)} linhas")

        # Enriquece products com dados do OEM Parts (join por ref_autoflex ↔ sku_autoflex)
        # Adiciona colunas de estoque e aplicação nos products existentes
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS oem_enrich AS
                SELECT
                    o.ref_autoflex AS sku_autoflex,
                    o.descricao    AS descricao_oem,
                    o.aplicacao,
                    o.oem_montadora,
                    o.quantidade,
                    o.fabricante   AS fabricante_oem,
                    o.subsecao,
                    o.local,
                    o.ultima_entrada
                FROM oem_parts o
            """)
        except Exception:
            con.execute("DROP TABLE IF EXISTS oem_enrich")
            con.execute("""
                CREATE TABLE oem_enrich AS
                SELECT
                    o.ref_autoflex AS sku_autoflex,
                    o.descricao    AS descricao_oem,
                    o.aplicacao,
                    o.oem_montadora,
                    o.quantidade,
                    o.fabricante   AS fabricante_oem,
                    o.subsecao,
                    o.local,
                    o.ultima_entrada
                FROM oem_parts o
            """)
        print(f"  ✅ oem_enrich: tabela de enriquecimento criada")

        # Fitment derivado da coluna Aplicação do OEM Parts
        # Extrai modelo + ano da string de aplicação para incrementar o fitment
        fitment_oem = []
        for _, row in df2.iterrows():
            aplic = str(row.get("aplicacao",""))
            ref   = str(row.get("ref_autoflex",""))
            desc  = str(row.get("descricao",""))
            # Divide por / e extrai modelo + ano
            partes = re.split(r'[/\n\r]', aplic)
            modelos_vistos = set()
            for parte in partes:
                parte = parte.strip()
                # Tenta casar "MODELO ANO_INI/ANO_FIM" ou "MODELO ANO/"
                m = re.match(r'^([A-ZÁÀÂÃÉÊÍÓÔÕÚÜÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÜÇ0-9\s\-\.]+?)\s+(\d{2})/(\d{2})', parte)
                if m:
                    modelo = m.group(1).strip().upper()
                    ano_ini = "20" + m.group(2) if int(m.group(2)) < 50 else "19" + m.group(2)
                    ano_fim = "20" + m.group(3) if int(m.group(3)) < 50 else "19" + m.group(3)
                    if modelo not in modelos_vistos and len(modelo) >= 2:
                        modelos_vistos.add(modelo)
                        fitment_oem.append({
                            "sku": ref, "descricao": desc,
                            "montadora": "", "modelo": modelo,
                            "ano_ini": ano_ini, "ano_fim": ano_fim,
                            "motor": "", "linha": row.get("subsecao",""),
                            "confidence": "0.7",
                        })
                else:
                    # Só modelo sem ano
                    m2 = re.match(r'^([A-ZÁÀÂÃÉÊÍÓÔÕÚÜÇ][A-ZÁÀÂÃÉÊÍÓÔÕÚÜÇ0-9\s\-\.]{1,30}?)(?:\s+\d|\s*$)', parte)
                    if m2:
                        modelo = m2.group(1).strip().upper()
                        if modelo not in modelos_vistos and len(modelo) >= 2:
                            LIXO = {"REF","COM","SEM","LADO","TODOS","SISTEMA","DISPLAY","COR",
                                    "EMBALAGEM","PROEMA","PINO","REPARO","LENTE","PARCIAL",
                                    "PREMIUM","REFIL","SUPORTE","ALAVANCA","CABO","BARRA",
                                    "TEVES","KIT","JOGO","ANEL","BOSCH","MANDO","VARGA",
                                    "CALIPER","CONJUNTO","TUBO","TERMINAIS","TERMINAL",
                                    "DENSO","EURO","DIESEL","GASOLINA","FLEX","TURBO",
                                    "ASPIRADO","MPFI","EFI","ROCAM","FIRE","ZETEC","ECOTEC",
                                    "MULTIJET","D4CB","CDI","E-TORQ","ETORQ"}
                            if modelo not in LIXO and not modelo[0].isdigit():
                                modelos_vistos.add(modelo)
                                fitment_oem.append({
                                    "sku": ref, "descricao": desc,
                                    "montadora": "", "modelo": modelo,
                                    "ano_ini": "", "ano_fim": "",
                                    "motor": "", "linha": row.get("subsecao",""),
                                    "confidence": "0.6",
                                })

        if fitment_oem:
            df_fit2 = pd.DataFrame(fitment_oem)
            # Append ao fitment existente
            df_fit2.to_sql("fitment", con, if_exists="append", index=False)
            print(f"  ✅ fitment +{len(df_fit2)} linhas do OEM Parts")

        # Produtos do OEM Parts que não estão no catálogo
        df2_prod = df2[["ref_autoflex","descricao","ncm","fabricante","oem_montadora",
                         "subsecao","aplicacao","quantidade"]].copy()
        df2_prod = df2_prod.rename(columns={
            "ref_autoflex":  "sku_autoflex",
            "fabricante":    "montadora",
            "oem_montadora": "codigo_oem",
            "subsecao":      "grupo",
            "aplicacao":     "veiculo",
        })
        df2_prod["linha"] = "OEM"
        df2_prod["fonte"] = "oem_parts"
        df2_prod.to_sql("products", con, if_exists="append", index=False)
        print(f"  ✅ products +{len(df2_prod)} linhas do OEM Parts")
    else:
        print("⚠️  OEM_PARTS não encontrado — pulando.")

    # ── Índices ────────────────────────────────────────────
    indices = [
        "CREATE INDEX IF NOT EXISTS idx_fit_modelo    ON fitment(modelo)",
        "CREATE INDEX IF NOT EXISTS idx_fit_sku       ON fitment(sku)",
        "CREATE INDEX IF NOT EXISTS idx_prod_sku      ON products(sku_autoflex)",
        "CREATE INDEX IF NOT EXISTS idx_prod_oem      ON products(codigo_oem)",
        "CREATE INDEX IF NOT EXISTS idx_oem_ref       ON oem_parts(ref_autoflex)",
        "CREATE INDEX IF NOT EXISTS idx_oem_descricao ON oem_parts(descricao)",
    ]
    for idx in indices:
        try: con.execute(idx)
        except: pass
    con.commit()

    # ── Sumário ────────────────────────────────────────────
    def count(t):
        try: return con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except: return 0

    print(f"\n{'='*50}")
    print(f"✅ Banco criado: {DB_PATH}")
    print(f"{'='*50}")
    print(f"  products   : {count('products'):>6,}")
    print(f"  fitment    : {count('fitment'):>6,}")
    print(f"  cross_refs : {count('cross_refs'):>6,}")
    print(f"  precos     : {count('precos'):>6,}")
    print(f"  oem_parts  : {count('oem_parts'):>6,}")
    con.close()


if __name__ == "__main__":
    override1 = sys.argv[1] if len(sys.argv) > 1 else None
    override2 = sys.argv[2] if len(sys.argv) > 2 else None
    xlsx1 = find_xlsx(XLSX1_CANDIDATES, override1)
    xlsx2 = find_xlsx(XLSX2_CANDIDATES, override2)
    if not xlsx1 and not xlsx2:
        print("❌ Nenhuma planilha encontrada.")
        print("   Uso: python build_db.py [catalogo.xlsx] [oem_parts.xlsx]")
        sys.exit(1)
    build(xlsx1, xlsx2)