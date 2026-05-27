import sqlite3
import re
from pathlib import Path
from rapidfuzz import fuzz

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH  = BASE_DIR / "data" / "autoflex_catalog.db"

# =========================================================
# PESOS DE RELEVÂNCIA
# =========================================================

IMPORTANT_TERMS = {
    "trambulador": 80, "alavanca": 70, "cambio": 65,
    "selecao": 65, "engate": 60, "marcha": 60,
    "embreagem": 60, "disco": 55, "plato": 55,
    "freio": 55, "pastilha": 55, "abs": 45,
    "amortecedor": 55, "suspensao": 50, "suporte": 45,
    "reacao": 45, "bucha": 45, "barra": 40,
    "pivo": 45, "filtro": 45, "radiador": 45,
    "bomba": 45, "motor": 40, "abertura": 35,
    "cabo": 20, "manopla": 35, "lente": 35,
    "kit": 20, "reparo": 25,
}

NEGATIVE_TERMS = {
    "trambulador": ["abertura", "porta", "capo", "tampa", "combustivel"],
    "embreagem":   ["tampa", "porta", "capo", "abertura"],
    "freio":       ["retrovisor", "tampa"],
    "suporte":     ["cabo", "abertura"],
}

STOPWORDS = {
    "de","da","do","dos","das","para","com","e","o","a",
    "um","uma","os","as","em","no","na","por","se",
}

SYNONYMS = {
    "câmbio":"cambio","capô":"capo","injeção":"injecao",
    "seleção":"selecao","suspensão":"suspensao",
    "pastilhas":"pastilha","freios":"freio",
}


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_text(text: str) -> str:
    text = text.lower()
    for old, new in SYNONYMS.items():
        text = text.replace(old, new)
    text = re.sub(r"[^a-zA-Z0-9À-ÿ\s\.]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list:
    return [t for t in normalize_text(text).split() if t not in STOPWORDS]


def calculate_score(termo: str, descricao: str, motor: str = None) -> float:
    termo_n  = normalize_text(termo)
    desc_n   = normalize_text(descricao)
    score    = float(fuzz.token_set_ratio(termo_n, desc_n))
    t_tok    = tokenize(termo_n)
    d_tok    = tokenize(desc_n)

    for term, peso in IMPORTANT_TERMS.items():
        if term in t_tok and term in d_tok:
            score += peso

    for token in t_tok:
        if len(token) < 3: continue
        if token in d_tok:
            score += 20
        else:
            for dt in d_tok:
                if token in dt and len(token) >= 4:
                    score += 8; break

    if t_tok and all(t in d_tok for t in t_tok):
        score += 50
    if termo_n in desc_n:
        score += 35
    if motor:
        score += 80 if motor.lower() in desc_n else -15

    for term, negs in NEGATIVE_TERMS.items():
        if term in t_tok:
            for neg in negs:
                if neg in d_tok:
                    score -= 50
    return score


# =========================================================
# FITMENT — busca por modelo
# =========================================================

def search_vehicle_fitment(modelo: str, ano=None) -> list:
    if not modelo:
        return []
    conn = get_connection()

    # Busca exata + parcial no modelo
    rows = conn.execute("""
        SELECT * FROM fitment
        WHERE LOWER(modelo) LIKE ?
           OR LOWER(modelo) LIKE ?
        LIMIT 300
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
# PRODUTOS POR FITMENT
# =========================================================

def search_products_by_vehicle(fitments: list, termo: str, motor: str = None) -> list:
    if not fitments or not termo:
        return []
    conn = get_connection()

    sku_list = list({str(f.get("sku") or "") for f in fitments if f.get("sku")})
    if not sku_list:
        conn.close()
        return []

    ph   = ",".join("?" * len(sku_list))
    rows = conn.execute(
        f"SELECT * FROM products WHERE sku_autoflex IN ({ph})", sku_list
    ).fetchall()
    conn.close()

    ranked = []
    for row in rows:
        r = dict(row)
        r["_score"] = calculate_score(termo, r.get("descricao",""), motor)
        ranked.append(r)

    ranked.sort(key=lambda x: x["_score"], reverse=True)

    print("\n=== TOP RESULTADOS ===")
    for r in ranked[:5]:
        print(f"  {r['_score']:>6.1f} → {r.get('descricao')}")

    return ranked[:10]


# =========================================================
# BUSCA DIRETA — oem_parts + products por descrição
# =========================================================

def search_oem_parts_by_term(termo: str, modelo: str = None, fabricante: str = None) -> list:
    """
    Busca em products E oem_parts por descrição/aplicação.
    Usado como fallback quando fitment não encontra nada.
    """
    conn = get_connection()
    results = []

    # Termos individuais para busca ampla
    palavras = [p for p in normalize_text(termo).split() if len(p) >= 3 and p not in STOPWORDS]

    # ── Busca em products ──────────────────────────────────
    try:
        for palavra in palavras:
            rows = conn.execute("""
                SELECT * FROM products
                WHERE UPPER(descricao) LIKE UPPER(?)
                   OR UPPER(veiculo)   LIKE UPPER(?)
                LIMIT 60
            """, (f"%{palavra}%", f"%{modelo or palavra}%")).fetchall()
            results.extend([dict(r) for r in rows])
    except Exception as e:
        print(f"[products search] {e}")

    # ── Busca em oem_parts ─────────────────────────────────
    try:
        for palavra in palavras:
            rows = conn.execute("""
                SELECT
                    ref_autoflex AS sku_autoflex,
                    oem_montadora AS codigo_oem,
                    descricao,
                    aplicacao AS veiculo,
                    fabricante AS montadora,
                    subsecao AS grupo,
                    quantidade,
                    local,
                    'oem_parts' AS fonte,
                    aplicacao
                FROM oem_parts
                WHERE UPPER(descricao) LIKE UPPER(?)
                   OR UPPER(aplicacao) LIKE UPPER(?)
                LIMIT 60
            """, (f"%{palavra}%", f"%{modelo or palavra}%")).fetchall()
            results.extend([dict(r) for r in rows])
    except Exception as e:
        print(f"[oem_parts search] {e}")

    conn.close()

    if not results:
        return []

    # Deduplica por sku
    vistos = set()
    unicos = []
    for r in results:
        k = r.get("sku_autoflex","") or r.get("descricao","")
        if k and k not in vistos:
            vistos.add(k)
            unicos.append(r)

    # Filtra por fabricante se informado
    if fabricante:
        fab_n  = normalize_text(fabricante)
        filtrados = [r for r in unicos
                     if fab_n in normalize_text(r.get("montadora",""))
                     or fab_n in normalize_text(r.get("veiculo",""))
                     or fab_n in normalize_text(r.get("descricao",""))]
        if filtrados:
            unicos = filtrados

    # Rankeia
    busca = f"{termo} {modelo or ''} {fabricante or ''}"
    for r in unicos:
        desc_full = r.get("descricao","") + " " + r.get("veiculo","")
        r["_score"] = calculate_score(busca, desc_full)

    unicos.sort(key=lambda x: x["_score"], reverse=True)

    print("\n=== TOP OEM PARTS ===")
    for r in unicos[:5]:
        print(f"  {r['_score']:>6.1f} → {r.get('descricao')}")

    return unicos[:10]


# =========================================================
# HELPERS
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