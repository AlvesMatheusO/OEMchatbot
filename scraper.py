#!/usr/bin/env python3
"""
scraper_mercadopecas.py
========================
Coleta peças do lojamercadopecas.com.br via API JSON pública do Shopify.

Salva em:
  • SQLite: data/mercadopecas.db
  • CSV:    data/mercadopecas.csv  opcional com --csv

Uso:
    python scraper_mercadopecas.py
    python scraper_mercadopecas.py --col amortecedores
    python scraper_mercadopecas.py --col freio --csv
    python scraper_mercadopecas.py --limit 50
    python scraper_mercadopecas.py --delay 1.5
"""

import re
import csv
import time
import json
import sqlite3
import argparse
import logging
from pathlib import Path
from datetime import datetime
from html import unescape

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://lojamercadopecas.com.br"
DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "mercadopecas.db"
CSV_PATH = DATA_DIR / "mercadopecas.csv"

COLECOES = [
    "amortecedores",
    "pastilha-de-freio",
    "freio",
    "suspensao-e-direcao",
    "embreagem",
    "sistema-de-ignicao",
    "bobina-de-ignicao",
    "vela-de-ignicao",
    "motor-e-injecao",
    "filtros",
    "rolamentos",
    "correia",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

FABRICANTES = [
    "MAGNETI MARELLI", "CONTINENTAL", "FLEETGUARD", "FRAS-LE",
    "TRW", "BOSCH", "LUK", "SACHS", "COFAP", "DELPHI",
    "VALEO", "NGK", "DENSO", "FAG", "SKF", "INA", "GATES",
    "DAYCO", "FREMAX", "SYL", "COBREQ", "NAKATA", "MONROE",
    "KYB", "BILSTEIN", "ATE", "BREMBO", "FERODO", "EBC", "PAGID",
    "MAHLE", "MANN", "FRAM", "TECFIL", "WIX", "BENDIX",
    "MOTUL", "CASTROL", "SHELL", "MOBIL", "TOTAL", "PETRONAS",
    "HELLA", "OSRAM", "PHILIPS", "WURTH", "LOCTITE", "3M",
    "ZF", "AISIN", "EXEDY", "HEPU", "BEHR", "VAICO", "MEYLE",
]

GRUPOS = {
    "amortecedor": "AMORTECEDOR",
    "pastilha": "FREIO",
    "freio": "FREIO",
    "disco": "FREIO",
    "tambor": "FREIO",
    "embreagem": "EMBREAGEM",
    "disco embreagem": "EMBREAGEM",
    "platô": "EMBREAGEM",
    "plato": "EMBREAGEM",
    "kit embreagem": "EMBREAGEM",
    "vela": "IGNIÇÃO",
    "bobina": "IGNIÇÃO",
    "cabo ignição": "IGNIÇÃO",
    "correia": "CORREIA",
    "tensor": "CORREIA",
    "rolamento": "ROLAMENTO",
    "cubo": "ROLAMENTO",
    "filtro": "FILTRO",
    "bomba": "BOMBA",
    "radiador": "ARREFECIMENTO",
    "termostato": "ARREFECIMENTO",
    "suspensão": "SUSPENSÃO",
    "bucha": "SUSPENSÃO",
    "barra": "SUSPENSÃO",
    "pivô": "SUSPENSÃO",
    "pivo": "SUSPENSÃO",
    "direção": "DIREÇÃO",
    "direcao": "DIREÇÃO",
}

MODELOS_BR = [
    "PALIO", "SIENA", "UNO", "ARGO", "CRONOS", "MOBI", "FIORINO", "DOBLO",
    "TORO", "STRADA", "PULSE", "FASTBACK", "BRAVO", "LINEA", "IDEA", "PUNTO",
    "GOL", "POLO", "VIRTUS", "VOYAGE", "SAVEIRO", "FOX", "AMAROK", "TIGUAN",
    "JETTA", "GOLF", "CROSSFOX", "UP",
    "ONIX", "TRACKER", "SPIN", "MONTANA", "COBALT", "CELTA", "CORSA",
    "VECTRA", "ASTRA", "S10", "CRUZE", "PRISMA", "AGILE", "ZAFIRA", "SONIC",
    "CIVIC", "HRV", "WRV", "CRV", "CITY", "FIT", "ACCORD",
    "COROLLA", "HILUX", "YARIS", "ETIOS", "SW4", "RAV4", "CAMRY",
    "HB20", "CRETA", "TUCSON", "IX35", "I30",
    "KWID", "SANDERO", "LOGAN", "DUSTER", "CAPTUR", "OROCH", "MASTER", "MEGANE",
    "KA", "FIESTA", "ECOSPORT", "RANGER", "FUSION", "TRANSIT", "FOCUS", "EDGE",
    "KICKS", "VERSA", "MARCH", "FRONTIER", "SENTRA",
    "SPORTAGE", "SORENTO", "CERATO",
    "COMPASS", "RENEGADE", "WRANGLER", "COMMANDER",
    "OUTLANDER", "ASX", "PAJERO", "L200",
    "SPRINTER", "DAILY", "DUCATO", "BOXER", "JUMPER",
]


def configurar_logs():
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s | %(message)s",
    )


def criar_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    return session


def limpar_html(html: str) -> str:
    texto = re.sub(r"<[^>]+>", " ", html or "")
    texto = unescape(texto)
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


def normalizar_tags(tags_raw) -> list[str]:
    if not tags_raw:
        return []

    if isinstance(tags_raw, str):
        return [tag.strip() for tag in tags_raw.split(",") if tag.strip()]

    if isinstance(tags_raw, list):
        return [str(tag).strip() for tag in tags_raw if str(tag).strip()]

    return []


def extrair_fabricante(titulo: str) -> str:
    titulo_up = titulo.upper()

    for fab in sorted(FABRICANTES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(fab) + r"\b", titulo_up):
            return fab

    return ""


def referencia_valida(ref: str) -> bool:
    ref = ref.strip().upper()

    if len(ref) < 4:
        return False

    if ref.isdigit():
        numero = int(ref)

        if 1900 <= numero <= 2035:
            return False

        if len(ref) < 5:
            return False

    if ref in FABRICANTES:
        return False

    return True


def extrair_referencia(titulo: str) -> str:
    titulo_clean = titulo.upper()

    for fab in FABRICANTES:
        titulo_clean = re.sub(r"\b" + re.escape(fab) + r"\b", " ", titulo_clean)

    titulo_clean = re.sub(r"\b(19\d{2}|20[0-3]\d)\b", " ", titulo_clean)

    padroes = [
        r"\b([A-Z]{1,5}\d{3,}[A-Z0-9\-]*)\b",
        r"\b(\d{5,}[A-Z0-9\-]*)\b",
        r"[–—-]\s*([A-Z0-9][A-Z0-9\-\.\/]{3,})",
        r":\s*([A-Z0-9][A-Z0-9\-\.\/]{3,})",
    ]

    for padrao in padroes:
        match = re.search(padrao, titulo_clean)

        if match:
            ref = match.group(1).strip(" -–—:").strip()

            if referencia_valida(ref):
                return ref

    return ""


def extrair_aplicacao(titulo: str, tags: list[str]) -> tuple[list[str], str, str]:
    texto = titulo.upper() + " " + " ".join(tags).upper()

    modelos = []

    for modelo in MODELOS_BR:
        if re.search(r"\b" + re.escape(modelo) + r"\b", texto):
            modelos.append(modelo)

    anos = re.findall(r"\b(19\d{2}|20[0-3]\d)\b", texto)

    if not anos:
        return modelos, "", ""

    anos_ordenados = sorted(set(anos))
    ano_ini = anos_ordenados[0]
    ano_fim = anos_ordenados[-1] if len(anos_ordenados) > 1 else ""

    return modelos, ano_ini, ano_fim


def extrair_grupo(titulo: str, colecao: str) -> str:
    titulo_lower = titulo.lower()

    for termo, grupo in GRUPOS.items():
        if termo in titulo_lower:
            return grupo

    colecao_lower = colecao.lower()

    for termo, grupo in GRUPOS.items():
        if termo in colecao_lower:
            return grupo

    return colecao.upper().replace("-", " ")


def get_colecao_produtos(
    colecao: str,
    limit: int = 500,
    delay: float = 1.0,
) -> list[dict]:
    produtos = []
    page_info = None
    session = criar_session()

    while len(produtos) < limit:
        quantidade = min(250, limit - len(produtos))

        if page_info:
            url = (
                f"{BASE_URL}/collections/{colecao}/products.json"
                f"?limit={quantidade}&page_info={page_info}"
            )
        else:
            url = (
                f"{BASE_URL}/collections/{colecao}/products.json"
                f"?limit={quantidade}"
            )

        try:
            response = session.get(url, timeout=20)
        except requests.RequestException as erro:
            logging.warning("Erro de rede na coleção %s: %s", colecao, erro)
            break

        if response.status_code == 404:
            logging.warning("Coleção não encontrada: %s", colecao)
            break

        if response.status_code != 200:
            logging.warning(
                "HTTP %s na coleção %s",
                response.status_code,
                colecao,
            )
            break

        try:
            data = response.json()
        except json.JSONDecodeError:
            logging.warning("Resposta inválida na coleção %s", colecao)
            break

        lote = data.get("products", [])

        if not lote:
            break

        produtos.extend(lote)

        logging.info(
            "%s: %s produtos coletados",
            colecao,
            len(produtos),
        )

        link_header = response.headers.get("Link", "")
        next_match = re.search(
            r'<[^>]+page_info=([^&>]+)[^>]*>;\s*rel="next"',
            link_header,
        )

        if next_match and len(produtos) < limit:
            page_info = next_match.group(1)
            time.sleep(delay)
        else:
            break

    return produtos[:limit]


def processar_produto(produto: dict, colecao: str) -> dict | None:
    titulo = produto.get("title", "").strip()

    if not titulo:
        return None

    tags = normalizar_tags(produto.get("tags", ""))

    descricao = limpar_html(produto.get("body_html", ""))[:500]

    variants = produto.get("variants", [])
    preco = ""

    if variants:
        preco = variants[0].get("price", "")

    images = produto.get("images", [])
    imagem = ""

    if images:
        imagem = images[0].get("src", "")

    fabricante = extrair_fabricante(titulo)
    referencia = extrair_referencia(titulo)
    grupo = extrair_grupo(titulo, colecao)
    modelos, ano_ini, ano_fim = extrair_aplicacao(titulo, tags)

    handle = produto.get("handle", "")

    return {
        "id": str(produto.get("id", "")),
        "titulo": titulo,
        "descricao": descricao[:300],
        "fabricante": fabricante,
        "referencia": referencia,
        "grupo": grupo,
        "colecao": colecao,
        "modelos": "|".join(modelos),
        "ano_ini": ano_ini,
        "ano_fim": ano_fim,
        "preco_brl": preco,
        "tags": "|".join(tags),
        "imagem": imagem,
        "url": f"{BASE_URL}/products/{handle}" if handle else "",
        "coletado_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def init_db(con: sqlite3.Connection):
    con.execute("""
        CREATE TABLE IF NOT EXISTS pecas (
            id          TEXT PRIMARY KEY,
            titulo      TEXT,
            descricao   TEXT,
            fabricante  TEXT,
            referencia  TEXT,
            grupo       TEXT,
            colecao     TEXT,
            modelos     TEXT,
            ano_ini     TEXT,
            ano_fim     TEXT,
            preco_brl   TEXT,
            tags        TEXT,
            imagem      TEXT,
            url         TEXT,
            coletado_em TEXT
        )
    """)

    con.execute("CREATE INDEX IF NOT EXISTS idx_grupo ON pecas(grupo)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_fabricante ON pecas(fabricante)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_modelos ON pecas(modelos)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_referencia ON pecas(referencia)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_titulo ON pecas(titulo)")

    con.commit()


def salvar_db(con: sqlite3.Connection, registros: list[dict]) -> int:
    if not registros:
        return 0

    sql = """
        INSERT OR REPLACE INTO pecas VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    dados = [
        (
            r["id"],
            r["titulo"],
            r["descricao"],
            r["fabricante"],
            r["referencia"],
            r["grupo"],
            r["colecao"],
            r["modelos"],
            r["ano_ini"],
            r["ano_fim"],
            r["preco_brl"],
            r["tags"],
            r["imagem"],
            r["url"],
            r["coletado_em"],
        )
        for r in registros
    ]

    con.executemany(sql, dados)
    con.commit()

    return len(registros)


def salvar_csv_snapshot(registros: list[dict]):
    if not registros:
        return

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as arquivo:
        writer = csv.DictWriter(arquivo, fieldnames=registros[0].keys())
        writer.writeheader()
        writer.writerows(registros)


def exibir_amostras(registros: list[dict], quantidade: int = 2):
    for item in registros[:quantidade]:
        logging.info("• %s", item["titulo"][:80])
        logging.info(
            "  Fab: %s | Ref: %s | Modelos: %s | R$ %s",
            item["fabricante"] or "-",
            item["referencia"] or "-",
            item["modelos"][:40] or "-",
            item["preco_brl"] or "-",
        )


def main():
    configurar_logs()

    parser = argparse.ArgumentParser(
        description="Scraper Mercado Peças via Shopify JSON"
    )

    parser.add_argument(
        "--col",
        help="Coleção específica. Ex: amortecedores",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Máximo de produtos por coleção",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay entre requests em segundos",
    )

    parser.add_argument(
        "--csv",
        action="store_true",
        help="Exporta CSV também",
    )

    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(str(DB_PATH))
    init_db(con)

    colecoes = [args.col] if args.col else COLECOES

    total_salvo = 0
    todos_registros = []

    logging.info("=" * 60)
    logging.info("Scraper Mercado Peças")
    logging.info("Site: %s", BASE_URL)
    logging.info("Banco: %s", DB_PATH)
    logging.info("Coleções: %s", len(colecoes))
    logging.info("=" * 60)

    for colecao in colecoes:
        logging.info("Coletando coleção: %s", colecao)

        produtos_raw = get_colecao_produtos(
            colecao=colecao,
            limit=args.limit,
            delay=args.delay,
        )

        logging.info("%s produtos brutos encontrados", len(produtos_raw))

        if not produtos_raw:
            continue

        registros = []

        for produto in produtos_raw:
            item = processar_produto(produto, colecao)

            if item:
                registros.append(item)

        salvos = salvar_db(con, registros)
        total_salvo += salvos
        todos_registros.extend(registros)

        logging.info("%s produtos salvos no banco", salvos)
        exibir_amostras(registros)

        time.sleep(args.delay)

    if args.csv:
        salvar_csv_snapshot(todos_registros)
        logging.info("CSV exportado em: %s", CSV_PATH)

    total_db = con.execute("SELECT COUNT(*) FROM pecas").fetchone()[0]

    logging.info("=" * 60)
    logging.info("Concluído")
    logging.info("Salvos nesta execução: %s", total_salvo)
    logging.info("Total no banco: %s", total_db)
    logging.info("Banco: %s", DB_PATH)

    rows = con.execute("""
        SELECT grupo, COUNT(*) AS total, COUNT(DISTINCT fabricante) AS fabricantes
        FROM pecas
        GROUP BY grupo
        ORDER BY total DESC
    """).fetchall()

    logging.info("Resumo por grupo:")

    for grupo, total, fabricantes in rows:
        logging.info(
            "%-22s %5s peças | %s fabricantes",
            grupo,
            total,
            fabricantes,
        )

    con.close()
    logging.info("=" * 60)


if __name__ == "__main__":
    main()