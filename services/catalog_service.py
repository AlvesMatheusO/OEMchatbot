import sqlite3
import re
from pathlib import Path
from rapidfuzz import fuzz

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH  = BASE_DIR / "data" / "autoflex_catalog.db"

# =========================================================
# PESOS DE RELEVÂNCIA — ajustados para peças de câmbio
# =========================================================

IMPORTANT_TERMS = {
    # Grupos de câmbio (alta prioridade)
    "trambulador":   80,
    "alavanca":      70,
    "cambio":        65,
    "selecao":       65,
    "engate":        60,
    "marcha":        60,
    # Embreagem
    "embreagem":     60,
    "disco":         55,
    "platô":         55,
    "plato":         55,
    # Freio
    "freio":         55,
    "pastilha":      55,
    "abs":           45,
    # Suspensão / direção
    "amortecedor":   55,
    "suspensao":     50,
    "suporte":       45,
    "reacao":        45,
    "bucha":         45,
    "barra":         40,
    "pivô":          45,
    "pivo":          45,
    # Motor / arrefecimento
    "filtro":        45,
    "radiador":      45,
    "bomba":         45,
    "motor":         40,
    # Carroceria / abertura
    "abertura":      35,
    "cabo":          20,
    "manopla":       35,
    "kit":           20,
    "reparo":        25,
}

NEGATIVE_TERMS = {
    "trambulador": ["abertura", "porta", "capo", "tampa", "combustivel"],
    "embreagem":   ["tampa", "porta", "capo", "abertura"],
    "freio":       ["retrovisor", "tampa"],
    "amortecedor": ["acabamento", "moldura"],
    "suporte":     ["cabo", "abertura"],
}

STOPWORDS = {
    "de","da","do","dos","das","para","com","e","o","a",
    "um","uma","os","as","em","no","na","por","se",
}

SYNONYMS = {
    "câmbio":    "cambio",
    "capô":      "capo",
    "injeção":   "injecao",
    "seleção":   "selecao",
    "suspensão": "suspensao",
    "direção":   "direcao",
    "pastilhas": "pastilha",
}


# =========================================================
# Conexão
# =========================================================

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# =========================================================
# Normalização
# =========================================================

def normalize_text(text: str) -> str:
    text = text.lower()
    for old, new in SYNONYMS.items():
        text = text.replace(old, new)
    text = re.sub(r"[^a-zA-Z0-9À-ÿ\s\.]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list:
    return [t for t in normalize_text(text).split() if t not in STOPWORDS]


# =========================================================
# Score de relevância — melhorado
# =========================================================

def calculate_score(termo: str, descricao: str, motor: str = None) -> float:
    termo_n     = normalize_text(termo)
    descricao_n = normalize_text(descricao)

    # Base fuzzy
    score = float(fuzz.token_set_ratio(termo_n, descricao_n))

    t_tokens = tokenize(termo_n)
    d_tokens = tokenize(descricao_n)

    # Bônus por termo importante presente nos dois lados
    for term, peso in IMPORTANT_TERMS.items():
        if term in t_tokens and term in d_tokens:
            score += peso

    # Bônus por cada token da busca presente na descrição
    for token in t_tokens:
        if len(token) < 3:
            continue
        if token in d_tokens:
            score += 20
        else:
            # Partial match (token contido na palavra)
            for d_tok in d_tokens:
                if token in d_tok and len(token) >= 4:
                    score += 8
                    break

    # Bônus: todos os tokens da busca presentes na descrição
    if t_tokens and all(t in d_tokens for t in t_tokens):
        score += 50

    # Bônus: substring exata
    if termo_n in descricao_n:
        score += 35

    # Bônus motor
    if motor:
        if motor.lower() in descricao_n:
            score += 80
        else:
            score -= 15

    # Penalidades por termos contraditórios
    for term, negatives in NEGATIVE_TERMS.items():
        if term in t_tokens:
            for neg in negatives:
                if neg in d_tokens:
                    score -= 50

    return score


# =========================================================
# Busca fitment
# =========================================================

def search_vehicle_fitment(modelo: str, ano=None) -> list:
    if not modelo:
        return []
    conn = get_connection()
    rows = conn.execute("""
        SELECT *
        FROM fitment
        WHERE LOWER(modelo) LIKE ?
           OR LOWER(modelo) LIKE ?
        LIMIT 200
    """, (f"%{modelo.lower()}%", f"{modelo.lower()}%")).fetchall()
    conn.close()

    results = [dict(r) for r in rows]

    if ano and results:
        filtrados = []
        for r in results:
            try:
                ini = int(r.get("ano_ini","") or 0)
                fim = int(r.get("ano_fim","") or 9999)
                a   = int(ano)
                if (ini == 0 or a >= ini) and (fim == 9999 or a <= fim):
                    filtrados.append(r)
            except:
                filtrados.append(r)
        if filtrados:
            results = filtrados

    return results


# =========================================================
# Produtos por veículo
# =========================================================

def search_products_by_vehicle(fitments: list, termo: str, motor: str = None) -> list:
    if not fitments or not termo:
        return []

    conn = get_connection()
    sku_list = list({str(f.get("sku") or f.get("SKU","")) for f in fitments})
    sku_list = [s for s in sku_list if s]

    if not sku_list:
        conn.close()
        return []

    placeholders = ",".join("?" * len(sku_list))
    rows = conn.execute(
        f"SELECT * FROM products WHERE sku_autoflex IN ({placeholders})",
        sku_list
    ).fetchall()
    conn.close()

    if not rows:
        return []

    ranked = []
    for row in rows:
        r    = dict(row)
        desc = r.get("descricao","")
        r["_score"] = calculate_score(termo, desc, motor)
        ranked.append(r)

    ranked.sort(key=lambda x: x["_score"], reverse=True)

    print("\n=== TOP RESULTADOS ===")
    for r in ranked[:5]:
        print(f"  {r['_score']:>6.1f} → {r.get('descricao')}")

    return ranked[:10]


# =========================================================
# Busca direta na tabela oem_parts (sem fitment)
# =========================================================

def search_oem_parts_by_term(termo: str, modelo: str = None, fabricante: str = None) -> list:
    """
    Busca na tabela oem_parts por descrição e/ou aplicação.
    Usado como fallback quando fitment não encontra resultado.
    """
    conn = get_connection()

    try:
        # Monta filtro
        conditions = ["UPPER(o.descricao) LIKE UPPER(?)"]
        params     = [f"%{termo}%"]

        if modelo:
            conditions.append("UPPER(o.aplicacao) LIKE UPPER(?)")
            params.append(f"%{modelo}%")

        if fabricante:
            conditions.append("UPPER(o.fabricante) LIKE UPPER(?)")
            params.append(f"%{fabricante}%")

        where = " OR ".join(conditions)
        rows = conn.execute(f"""
            SELECT o.codigo, o.descricao, o.aplicacao, o.oem_montadora,
                   o.fabricante, o.ref_autoflex, o.quantidade,
                   o.subsecao, o.local
            FROM oem_parts o
            WHERE {where}
            LIMIT 100
        """, params).fetchall()
    except Exception as e:
        print(f"[oem_parts search] erro: {e}")
        conn.close()
        return []

    conn.close()

    ranked = []
    for row in rows:
        r    = dict(row)
        desc = r.get("descricao","") + " " + r.get("aplicacao","")
        busca = f"{termo} {modelo or ''} {fabricante or ''}"
        r["_score"] = calculate_score(busca, desc)

        # Normaliza campos para formato igual ao products
        r["sku_autoflex"] = r.get("ref_autoflex","")
        r["codigo_oem"]   = r.get("oem_montadora","")
        r["grupo"]        = r.get("subsecao","")
        r["montadora"]    = r.get("fabricante","")
        ranked.append(r)

    ranked.sort(key=lambda x: x["_score"], reverse=True)

    print("\n=== TOP OEM PARTS ===")
    for r in ranked[:5]:
        print(f"  {r['_score']:>6.1f} → {r.get('descricao')}")

    return ranked[:10]


# =========================================================
# Helpers adicionais
# =========================================================

def get_product_by_sku(sku: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM products WHERE sku_autoflex = ?", (str(sku),)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def search_by_oem(oem: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM products WHERE UPPER(codigo_oem) = UPPER(?)", (oem,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_cross_refs(sku: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM cross_refs WHERE sku_autoflex = ?", (str(sku),)
        ).fetchone()
    except:
        row = None
    conn.close()
    return dict(row) if row else None


def get_compatible_vehicles(sku: str) -> list:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM fitment WHERE sku = ?", (str(sku),)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_oem_part_detail(ref: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM oem_parts WHERE ref_autoflex = ?", (str(ref),)
        ).fetchone()
    except:
        row = None
    conn.close()
    return dict(row) if row else None