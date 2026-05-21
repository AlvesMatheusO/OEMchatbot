#!/usr/bin/env python3
"""
Backend Flask – Assistente de Peças OEM
Estado em memória, chave = IP do cliente.
Integração RapidAPI Auto Parts Catalog (TecDoc) como fallback.
"""

import os, re, sys, sqlite3, time, json
import urllib.parse
import pandas as pd
import requests
from pathlib import Path
from datetime import datetime
from io import BytesIO

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from rapidfuzz import fuzz

# ── Gemini opcional ───────────────────────────────────────────
GEMINI_OK     = False
cliente_genai = None
MODELO_VISAO  = None

try:
    from google import genai
    from PIL import Image as PILImage
    GEMINI_OK = True
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

EXCEL_PATH     = BASE_DIR / "data" / "CATALOGO_AUTOFLEX_BD_v1-3.xlsx"
DB_PATH        = BASE_DIR / "autoflex_catalog.db"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# RapidAPI
RAPIDAPI_KEY      = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST     = os.getenv("RAPIDAPI_HOST", "auto-parts-catalog.p.rapidapi.com")
RAPIDAPI_BASE_URL = os.getenv("RAPIDAPI_BASE_URL", "https://auto-parts-catalog.p.rapidapi.com")
RAPIDAPI_LANG_ID  = os.getenv("RAPIDAPI_LANG_ID", "31")   # 31 = português (BR)

MODELOS_CANDIDATOS = [
    "gemini-2.0-flash", "gemini-1.5-flash",
    "gemini-1.5-flash-latest", "gemini-1.5-pro",
]

def _init_gemini():
    global cliente_genai, MODELO_VISAO
    if not GEMINI_OK or not GEMINI_API_KEY:
        print("⚠️  Gemini não configurado.")
        return
    try:
        cliente_genai = genai.Client(api_key=GEMINI_API_KEY)
        disponiveis = {m.name.split("/")[-1] for m in cliente_genai.models.list()}
        for cand in MODELOS_CANDIDATOS:
            if cand in disponiveis:
                MODELO_VISAO = cand
                print(f"✅ Gemini pronto → modelo: {MODELO_VISAO}")
                return
        MODELO_VISAO = MODELOS_CANDIDATOS[0]
    except Exception as e:
        print(f"⚠️  Falha Gemini: {e}")

_init_gemini()

# ── Flask ─────────────────────────────────────────────────────
app = Flask(__name__,
            template_folder=str(BASE_DIR),
            static_folder=str(BASE_DIR))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
CORS(app, supports_credentials=True)

# ── Sessões em memória (chave = IP) ───────────────────────────
_SESSIONS: dict = {}

def _client_key():
    return request.headers.get("X-Forwarded-For", request.remote_addr) or "local"

def get_sess():
    key = _client_key()
    if key not in _SESSIONS:
        _SESSIONS[key] = estado_inicial()
    return key, _SESSIONS[key]

def save_sess(key, sess):
    _SESSIONS[key] = sess

def estado_inicial():
    return {
        "estado": "livre",
        "opcoes": [],
        "peca_atual": None,
        "ultimo_modelo": None,
        "ultimo_ano": None,
        "ultimos_termos": [],
        "historico": [],
    }

# ── Excel ─────────────────────────────────────────────────────
print("🔄 Carregando catálogo…")
try:
    df = pd.read_excel(EXCEL_PATH, sheet_name="Catálogo Mestre")
    print(f"✅ {len(df)} peças.")
except Exception as e:
    print(f"❌ {e}"); sys.exit(1)

for col, alias in [
    ("SKU Autoflex","sku_str"), ("Código OEM","codigo_oem_str"),
    ("Descrição","descricao_lower"), ("Montadora","montadora_str"),
    ("Grupo","grupo_str"), ("Veículo","veiculo_str"),
]:
    df[alias] = df[col].fillna("").astype(str).str.strip()

df["busca_texto"] = df.apply(
    lambda r: " ".join(str(r.get(c,"")) for c in
        ["SKU Autoflex","Código OEM","Descrição","Montadora","Grupo","Veículo"]).lower(),
    axis=1)

# ── SQLite ────────────────────────────────────────────────────
def init_db():
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            sku_autoflex TEXT PRIMARY KEY, codigo_oem TEXT, descricao TEXT,
            veiculo TEXT, montadora TEXT, linha TEXT, grupo TEXT,
            ncm TEXT, ipi_percent TEXT, codigo_barras TEXT);
        CREATE TABLE IF NOT EXISTS fitment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku_autoflex TEXT, montadora TEXT, modelo TEXT,
            ano_inicio INTEGER, ano_fim INTEGER, motor_versao TEXT, confidence REAL,
            FOREIGN KEY(sku_autoflex) REFERENCES products(sku_autoflex));
        CREATE TABLE IF NOT EXISTS api_cache (
            cache_key TEXT PRIMARY KEY, query TEXT, source TEXT,
            response_json TEXT, created_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_prod_sku  ON products(sku_autoflex);
        CREATE INDEX IF NOT EXISTS idx_prod_oem  ON products(codigo_oem);
        CREATE INDEX IF NOT EXISTS idx_prod_desc ON products(descricao);
        CREATE INDEX IF NOT EXISTS idx_fit_mod   ON fitment(modelo);
        CREATE INDEX IF NOT EXISTS idx_cache_key ON api_cache(cache_key);
    """)
    c.commit(); c.close()

def import_excel():
    c = sqlite3.connect(DB_PATH); cur = c.cursor()
    cur.execute("SELECT COUNT(*) FROM products")
    if cur.fetchone()[0] > 0:
        c.close(); print("✅ SQLite já populado."); return
    print("📥 Importando…")
    df_p = pd.read_excel(EXCEL_PATH, sheet_name="Catálogo Mestre")
    for _, row in df_p.iterrows():
        cur.execute("INSERT OR REPLACE INTO products VALUES (?,?,?,?,?,?,?,?,?,?)",
            tuple(str(row.get(k,"")).strip() for k in [
                "SKU Autoflex","Código OEM","Descrição","Veículo",
                "Montadora","Linha","Grupo","NCM","IPI %","Cód. Barras"]))
    c.commit(); print(f"✅ {len(df_p)} produtos.")
    try:
        df_f = pd.read_excel(EXCEL_PATH, sheet_name="Fitment Completo"); n = 0
        for _, row in df_f.iterrows():
            sku = str(row.get("SKU","")).strip()
            cur.execute("SELECT 1 FROM products WHERE sku_autoflex=?", (sku,))
            if not cur.fetchone(): continue
            ai, af, cf = row.get("Ano Início"), row.get("Ano Fim"), row.get("Confidence", 0.6)
            cur.execute(
                "INSERT INTO fitment (sku_autoflex,montadora,modelo,ano_inicio,ano_fim,motor_versao,confidence)"
                " VALUES (?,?,?,?,?,?,?)",
                (sku, str(row.get("Montadora","")).strip(), str(row.get("Modelo","")).strip(),
                 int(ai) if pd.notna(ai) else None, int(af) if pd.notna(af) else None,
                 str(row.get("Motor/Versão","")).strip(), float(cf) if pd.notna(cf) else 0.6))
            n += 1
        c.commit(); print(f"✅ {n} aplicações.")
    except Exception as e:
        print(f"⚠️ Fitment: {e}")
    c.close()

init_db()
import_excel()

# ── Utilitários ───────────────────────────────────────────────
def norm(s):
    return re.sub(r"[^a-zA-Z0-9]","",str(s)).lower()

def eh_codigo(t):
    t = t.strip()
    return bool(re.fullmatch(r"[a-zA-Z0-9\-.]{4,}", t)) and len(t.split())==1

MODELOS_VEICULO = [
    "palio","uno","strada","siena","punto","linea","idea","doblo","doblô",
    "argo","cronos","mobi","fiorino","gol","fox","voyage","saveiro","virtus",
    "onix","prisma","cobalt","spin","corsa","celta","agile","montana",
    "toro","renegade","compass","hilux","corolla","hb20","civic","fit",
]
IGNORAR = {
    "quero","preciso","procuro","tem","vc","voce","você","me","manda","ver",
    "uma","um","o","a","os","as","de","do","da","dos","das","para","pra",
    "com","peca","peça","produto","kit","completo","completa",
    "dianteiro","dianteira","traseiro","traseira",
}

# Palavras que identificam TIPO de peça — usadas para evitar falsos positivos
PALAVRAS_TIPO = {
    "embreagem","freio","oleo","óleo","amortecedor","correia","filtro",
    "vela","pastilha","rolamento","tensor","bomba","radiador","bateria",
    "alternador","escapamento","suspensao","suspensão","mola","coxim",
    "bucha","pivo","pivô","cubo","manga","semi","eixo","diferencial",
    "marcha","transmissao","transmissão","cabo","sensor","bobina",
}

def extrair(texto):
    t = texto.lower().strip()
    modelo = next((m for m in MODELOS_VEICULO if re.search(rf"\b{re.escape(m)}\b",t)), None)
    ano_m  = re.search(r"\b(19|20)\d{2}\b", t)
    ano    = ano_m.group() if ano_m else None
    termos = [p for p in re.findall(r"[a-zA-ZÀ-ÿ0-9]+",t)
              if p not in IGNORAR and p != modelo and p != ano and len(p) > 2]
    return termos, modelo, ano

def score_relevancia(descricao_peca, termos_busca):
    """
    Relevância por match de palavras inteiras + penalidade por tipo conflitante.

    1. Termo buscado = palavra inteira na desc → +25 pts
    2. Termo buscado = substring na desc       → +10 pts
    3. PALAVRA_TIPO na desc mas não buscada    → -30 pts
    4. Nenhum termo encontrado                 → 0 (descarta)
    """
    if not termos_busca:
        return 0

    desc = descricao_peca.lower()
    desc_palavras = set(re.findall(r"[a-zA-ZÀ-ÿ0-9]+", desc))

    score = 0
    termos_encontrados = 0
    for t in termos_busca:
        tl = t.lower()
        if tl in desc_palavras:          # match exato de palavra inteira
            score += 25
            termos_encontrados += 1
        elif tl in desc:                 # substring (ex: "cabo" em "cabos")
            score += 10
            termos_encontrados += 1

    if termos_encontrados == 0:
        return 0

    # penalidade por tipo conflitante
    tipos_buscados = {tl for tl in (t.lower() for t in termos_busca) if tl in PALAVRAS_TIPO}
    tipos_na_desc  = {w for w in desc_palavras if w in PALAVRAS_TIPO}
    tipos_conflito = tipos_na_desc - tipos_buscados
    score -= len(tipos_conflito) * 30

    return max(score, 0)

# ── Buscas locais ─────────────────────────────────────────────
def row2p(r):
    return {"sku":str(r[0]).strip(),
            "codigo_oem":str(r[1]).strip() if len(r)>1 else "",
            "descricao":str(r[2]).strip()  if len(r)>2 else "",
            "veiculo":str(r[3]).strip()    if len(r)>3 else "",
            "montadora":str(r[4]).strip()  if len(r)>4 else "",
            "grupo":str(r[5]).strip()      if len(r)>5 else "",
            "ano_inicio":None,"ano_fim":None}

def enrich(pecas):
    if not pecas: return pecas
    c = sqlite3.connect(DB_PATH); cur = c.cursor()
    for p in pecas:
        cur.execute("SELECT ano_inicio,ano_fim FROM fitment "
                    "WHERE sku_autoflex=? ORDER BY confidence DESC,ano_inicio LIMIT 1",(p["sku"],))
        r = cur.fetchone()
        if r: p["ano_inicio"],p["ano_fim"] = r[0],r[1]
    c.close(); return pecas

def filtrar_por_ano(pecas, ano):
    if not ano: return pecas
    ano_int = int(ano)
    resultado = []
    for p in pecas:
        ai, af = p.get("ano_inicio"), p.get("ano_fim")
        if ai is None:
            p["ano_confirmado"] = False; resultado.append(p)
        elif ai <= ano_int <= (af or 9999):
            p["ano_confirmado"] = True; resultado.append(p)
    resultado.sort(key=lambda x: 0 if x.get("ano_confirmado") else 1)
    return resultado

def buscar_codigo(codigo):
    n = norm(codigo)
    c = sqlite3.connect(DB_PATH); cur = c.cursor()
    cur.execute("""SELECT sku_autoflex,codigo_oem,descricao,veiculo,montadora,grupo FROM products
        WHERE LOWER(REPLACE(REPLACE(sku_autoflex,'.','' ),'-',''))=?
           OR LOWER(REPLACE(REPLACE(codigo_oem, '.','' ),'-',''))=?
           OR codigo_oem LIKE ? OR sku_autoflex LIKE ? LIMIT 10""",
        (n,n,f"%{codigo}%",f"%{codigo}%"))
    rows = cur.fetchall(); c.close()
    return enrich([row2p(r) for r in rows])

def buscar_ref(texto):
    m = re.search(r"(ref|rf|oem)\s*[:\-]?\s*([a-zA-Z0-9\-.]+)",texto.lower())
    return buscar_codigo(m.group(2)) if m else []

def buscar_veiculo(modelo, ano=None, termos=None):
    c = sqlite3.connect(DB_PATH); cur = c.cursor()
    q = ("SELECT p.sku_autoflex,p.codigo_oem,p.descricao,p.veiculo,p.montadora,p.grupo,"
         "f.modelo,f.ano_inicio,f.ano_fim,f.motor_versao,f.confidence "
         "FROM fitment f JOIN products p ON p.sku_autoflex=f.sku_autoflex "
         "WHERE LOWER(f.modelo) LIKE ?")
    params = [f"%{modelo.lower()}%"]
    if ano:
        q += " AND (f.ano_inicio<=? AND (f.ano_fim>=? OR f.ano_fim IS NULL))"
        params += [int(ano),int(ano)]
    q += " ORDER BY f.confidence DESC LIMIT 50"
    cur.execute(q,params); rows = cur.fetchall(); c.close()
    pecas = []
    for r in rows:
        p = {"sku":str(r[0]).strip(),"codigo_oem":str(r[1]).strip(),
             "descricao":str(r[2]).strip(),"veiculo":str(r[3]).strip(),
             "montadora":str(r[4]).strip(),"grupo":str(r[5]).strip(),
             "modelo":str(r[6]).strip(),"ano_inicio":r[7],"ano_fim":r[8],
             "motor_versao":str(r[9]).strip(),"confidence":r[10],"ano_confirmado":True}
        if termos:
            sc = score_relevancia(p["descricao"], termos)
            # threshold dinâmico: mais termos → exige mais pontos
            # 1 termo genérico ("cabo") → exige 25 (pelo menos 1 match exato)
            # 2+ termos → exige 20 por termo
            threshold = max(25, 20 * len(termos))
            if sc < threshold: continue
            p["_score"] = sc
        pecas.append(p)
    if termos:
        pecas.sort(key=lambda x: x.get("_score",0), reverse=True)
    return pecas[:10]

def buscar_desc(termos, modelo=None, ano=None):
    c = sqlite3.connect(DB_PATH); cur = c.cursor()
    # tenta frase completa primeiro
    frase = " ".join(termos)
    q = "SELECT sku_autoflex,codigo_oem,descricao,veiculo,montadora,grupo FROM products WHERE LOWER(descricao) LIKE ?"
    params = [f"%{frase.lower()}%"]
    if modelo:
        q += " AND LOWER(veiculo) LIKE ?"; params.append(f"%{modelo.lower()}%")
    q += " LIMIT 30"
    cur.execute(q, params); rows = cur.fetchall()
    if not rows:
        q = "SELECT sku_autoflex,codigo_oem,descricao,veiculo,montadora,grupo FROM products WHERE 1=1"
        params = []
        for t in termos:
            q += " AND LOWER(descricao) LIKE ?"; params.append(f"%{t.lower()}%")
        if modelo:
            q += " AND LOWER(veiculo) LIKE ?"; params.append(f"%{modelo.lower()}%")
        q += " LIMIT 30"
        cur.execute(q, params); rows = cur.fetchall()
    c.close()
    pecas = enrich([row2p(r) for r in rows])
    scored = []
    threshold = max(25, 20 * len(termos)) if termos else 25
    for p in pecas:
        sc = score_relevancia(p["descricao"], termos)
        if sc >= threshold:
            p["_score"] = sc; scored.append(p)
    scored.sort(key=lambda x: x["_score"], reverse=True)
    if ano:
        scored = filtrar_por_ano(scored, ano)
    return scored[:10]

def buscar_fuzzy(consulta, limite=10):
    termos, _, _ = extrair(consulta)
    q = consulta.lower().strip(); res = []
    for _, row in df.iterrows():
        t = str(row["busca_texto"]).lower()
        sc_base = max(fuzz.partial_ratio(q,t), fuzz.token_sort_ratio(q,t), fuzz.token_set_ratio(q,t))
        if sc_base < 78: continue
        desc = str(row.get("Descrição","")).strip()
        sc_final = score_relevancia(desc, termos) if termos else sc_base
        threshold_fuzzy = max(25, 20 * len(termos)) if termos else 50
        if sc_final < threshold_fuzzy: continue
        res.append({"score":sc_final,"sku":str(row.get("SKU Autoflex","")).strip(),
            "codigo_oem":str(row.get("Código OEM","")).strip(),
            "descricao":desc,"veiculo":str(row.get("Veículo","")).strip(),
            "montadora":str(row.get("Montadora","")).strip(),
            "grupo":str(row.get("Grupo","")).strip(),
            "ano_inicio":None,"ano_fim":None})
    res.sort(key=lambda x:x["score"],reverse=True)
    return enrich(res[:limite])

# ── RapidAPI Cache ────────────────────────────────────────────
def cache_get(cache_key):
    try:
        c = sqlite3.connect(DB_PATH); cur = c.cursor()
        cur.execute("SELECT response_json FROM api_cache WHERE cache_key=?", (cache_key,))
        row = cur.fetchone(); c.close()
        return json.loads(row[0]) if row else None
    except Exception:
        return None

def cache_set(cache_key, query, source, data):
    try:
        c = sqlite3.connect(DB_PATH); cur = c.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO api_cache (cache_key,query,source,response_json,created_at)"
            " VALUES (?,?,?,?,?)",
            (cache_key, query, source, json.dumps(data, ensure_ascii=False), datetime.now().isoformat()))
        c.commit(); c.close()
    except Exception as e:
        print(f"⚠️ cache_set erro: {e}")

# ── RapidAPI HTTP ─────────────────────────────────────────────
def rapidapi_ativo():
    return bool(RAPIDAPI_KEY)

def rapidapi_get(path):
    if not rapidapi_ativo():
        return None, "RAPIDAPI_KEY não configurada no .env"
    url = f"{RAPIDAPI_BASE_URL}{path}"
    headers = {
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key":  RAPIDAPI_KEY,
    }
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 429: return None, "Rate-limit RapidAPI. Aguarde e tente novamente."
        if r.status_code in (401,403): return None, "Chave RapidAPI inválida."
        if not r.ok: return None, f"Erro RapidAPI {r.status_code}: {r.text[:120]}"
        return r.json(), ""
    except requests.Timeout:
        return None, "Timeout RapidAPI."
    except Exception as e:
        return None, f"Erro RapidAPI: {e}"

# ── RapidAPI: busca por descrição/categoria ───────────────────
def buscar_peca_api(texto):
    """
    Endpoint correto: /category/search-by-description/lang-id/{id}/search-query/{query}
    Retorna grupos de produtos (categorias de peça) que correspondem à busca.
    Os espaços e caracteres especiais são codificados em URL.
    """
    texto = str(texto).strip()
    print(f"🌐 RapidAPI categoria → {texto!r}")

    cache_key = f"catdesc:{norm(texto)}"
    cached = cache_get(cache_key)
    if cached:
        print("📦 Cache API hit")
        return _normalizar_categorias(cached)

    query_enc = urllib.parse.quote(texto, safe="")
    path = f"/category/search-by-description/lang-id/{RAPIDAPI_LANG_ID}/search-query/{query_enc}"
    print(f"🔗 {path}")

    data, erro = rapidapi_get(path)
    if erro:
        print(f"❌ RapidAPI categoria: {erro}")
        return []
    if not data:
        return []

    cache_set(cache_key, texto, "rapidapi_cat_search", data)
    return _normalizar_categorias(data)

def _normalizar_categorias(data):
    """Transforma a resposta de categorias no formato interno de peças."""
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("data","items","assemblies","productGroups","results","assemblyGroups"):
            if key in data and isinstance(data[key], list):
                items = data[key]; break

    pecas = []
    for item in items:
        name = str(item.get("assemblyGroupName",
                   item.get("name",
                   item.get("description","")))).strip()
        gid  = str(item.get("assemblyGroupNodeId",
                   item.get("productGroupId",
                   item.get("id","")))).strip()
        if not name: continue
        pecas.append({
            "sku": gid, "codigo_oem": "",
            "descricao": name, "veiculo": "",
            "montadora": "TecDoc API", "grupo": "CATEGORIA",
            "ano_inicio": None, "ano_fim": None,
            "origem": "rapidapi", "imagem": "",
            "article_id": None, "supplier_id": None,
        })
    print(f"   Categorias encontradas: {len(pecas)}")
    return pecas

# ── RapidAPI: cross-reference por OEM ────────────────────────
def buscar_cross_oem_api(oem):
    """
    Endpoint: /artlookup/search-for-cross-numbers/lang-id/{id}/article-type/OENumber/article-no/{no}
    Retorna peças aftermarket equivalentes ao número OEM informado.
    """
    oem = str(oem).strip()
    if not oem: return []
    print(f"🌐 RapidAPI cross-OEM → {oem!r}")

    cache_key = f"cross_oem:{norm(oem)}"
    cached = cache_get(cache_key)
    if cached:
        return _normalizar_artigos(cached)

    oem_enc = urllib.parse.quote(oem, safe="")
    path = (f"/artlookup/search-for-cross-numbers"
            f"/lang-id/{RAPIDAPI_LANG_ID}"
            f"/article-type/OENumber"
            f"/article-no/{oem_enc}")

    data, erro = rapidapi_get(path)
    if erro:
        print(f"❌ RapidAPI cross-OEM: {erro}")
        return []
    if not data:
        return []

    cache_set(cache_key, oem, "rapidapi_cross_oem", data)
    return _normalizar_artigos(data)

# ── RapidAPI: busca artigo por número ────────────────────────
def buscar_artigo_api(article_no):
    """
    Endpoint: /artlookup/search/lang-id/{id}/article-no/{no}
    Busca um artigo específico pelo número (SKU/artigo).
    """
    article_no = str(article_no).strip()
    if not article_no: return []

    cache_key = f"artno:{norm(article_no)}"
    cached = cache_get(cache_key)
    if cached:
        return _normalizar_artigos(cached)

    no_enc = urllib.parse.quote(article_no, safe="")
    path = f"/artlookup/search/lang-id/{RAPIDAPI_LANG_ID}/article-no/{no_enc}"
    print(f"🌐 RapidAPI artigo → {article_no!r}")

    data, erro = rapidapi_get(path)
    if erro:
        print(f"❌ RapidAPI artigo: {erro}")
        return []
    if not data:
        return []

    cache_set(cache_key, article_no, "rapidapi_art_search", data)
    return _normalizar_artigos(data)

def _normalizar_artigos(data):
    """Transforma resposta de artigos no formato interno de peças."""
    artigos = []
    if isinstance(data, list):
        artigos = data
    elif isinstance(data, dict):
        for key in ("articles","data","items","results"):
            if key in data and isinstance(data[key], list):
                artigos = data[key]; break

    pecas = []
    for item in artigos:
        article_no   = str(item.get("articleNo","")).strip()
        search_no    = str(item.get("articleSearchNo","")).strip()
        product_name = str(item.get("articleProductName",
                           item.get("name",""))).strip()
        supplier     = str(item.get("supplierName",
                           item.get("supplier",""))).strip()
        image        = str(item.get("s3image",
                           item.get("image",""))).strip()
        descricao    = f"{product_name} - {supplier}" if supplier else product_name
        pecas.append({
            "sku": article_no or search_no,
            "codigo_oem": search_no,
            "descricao": descricao,
            "veiculo": "",
            "montadora": supplier,
            "grupo": "ARTIGO API",
            "ano_inicio": None, "ano_fim": None,
            "origem": "rapidapi", "imagem": image,
            "article_id": item.get("articleId"),
            "supplier_id": item.get("supplierId"),
        })
    return pecas

# ── Formatação ────────────────────────────────────────────────
def v(val):
    s = str(val).strip() if val is not None else ""
    return "" if s.lower() in ("nan","none","null","") else s

def fmt_resumo(p, n=None):
    origem = p.get("origem","")
    txt = (f"{n}️⃣ " if n else "") + f"{v(p.get('descricao'))}\n"
    label = "Artigo" if origem in ("rapidapi","cache_api") else "SKU"
    txt += f"{label}: {v(p.get('sku'))}"
    if v(p.get("codigo_oem")): txt += f" | OEM: {v(p['codigo_oem'])}"
    if p.get("ano_inicio"):    txt += f" | Ano: {p['ano_inicio']} - {p.get('ano_fim') or 'atual'}"
    if p.get("ano_confirmado") is False: txt += " ⚠️ ano não confirmado"
    if origem == "rapidapi":   txt += " 🌐"
    return txt

def fmt_detalhe(p):
    txt  = "✅ Peça encontrada\n\n"
    txt += f"🔧 {v(p.get('descricao'))}\n\n"
    if p.get("origem") in ("rapidapi","cache_api"):
        txt += f"Artigo API: {v(p.get('sku'))}\n"
    else:
        txt += f"SKU Autoflex: {v(p.get('sku'))}\n"
    if v(p.get("codigo_oem")):   txt += f"OEM / Referência: {v(p['codigo_oem'])}\n"
    if v(p.get("montadora")):    txt += f"Montadora/Fornecedor: {v(p['montadora'])}\n"
    if v(p.get("grupo")):        txt += f"Grupo: {v(p['grupo'])}\n"
    if v(p.get("veiculo")):      txt += f"Aplicação: {v(p['veiculo'])}\n"
    if v(p.get("modelo")):       txt += f"Modelo: {v(p['modelo'])}\n"
    if v(p.get("motor_versao")): txt += f"Motor/versão: {v(p['motor_versao'])}\n"
    if p.get("ano_inicio"):      txt += f"Ano: {p['ano_inicio']} até {p.get('ano_fim') or 'atual'}\n"
    if v(p.get("imagem")):       txt += f"🖼️ {v(p.get('imagem'))}\n"
    # equivalentes
    equivalentes = p.get("equivalentes", [])
    if equivalentes:
        txt += "\n🔄 Equivalentes aftermarket:\n"
        for eq in equivalentes[:5]:
            txt += f"• {v(eq.get('descricao'))} (art. {v(eq.get('sku'))})\n"
    txt += "\nVocê pode pedir:\n1️⃣ aplicação completa\n2️⃣ peças similares\n3️⃣ nova busca"
    return txt

def fmt_lista(pecas):
    txt = f"Encontrei {len(pecas)} opção(ões):\n\n"
    for i,p in enumerate(pecas[:9],1):
        txt += fmt_resumo(p,i) + "\n\n"
    txt += "Responda com o número, SKU, OEM ou mande uma nova busca."
    return txt

def fmt_vazio(q):
    return (f"Não encontrei uma peça com essa busca.\n\nBusca feita: {q}\n\n"
            "Tente assim:\n• cabo embreagem palio 2006\n• 4002\n• oem 55204912\n• amortecedor uno")

# ── Lógica conversacional ─────────────────────────────────────
def processar(consulta, sess):
    consulta = consulta.strip()
    if not consulta:
        return "Digite uma peça, código, OEM ou veículo.", sess

    cmd    = consulta.lower().strip()
    estado = sess.get("estado","livre")

    if cmd == "/ajuda":     return _ajuda(), sess
    if cmd == "/historico": return _historico(sess), sess
    if cmd in ("/limpar","sair"):
        sess.update(estado_inicial()); return "Contexto limpo. Nova busca pronta.", sess

    if estado == "detalhe":
        peca = sess.get("peca_atual")
        if cmd == "1": return _aplicacao(peca), sess
        if cmd == "2": return _similares(peca, sess)
        if cmd == "3":
            sess.update(estado_inicial())
            return "🔍 Nova busca. Digite o que deseja.", sess
        sess.update(estado_inicial())
        return processar(consulta, sess)

    if estado == "lista":
        opcoes = sess.get("opcoes",[])
        resp, sess = _tentar_escolha(cmd, opcoes, sess)
        if resp is not None: return resp, sess
        sess.update(estado_inicial())
        return processar(consulta, sess)

    return _nova_busca(consulta, sess)

def _nova_busca(consulta, sess):
    termos, modelo, ano = extrair(consulta)
    ult_m = sess.get("ultimo_modelo")
    ult_a = sess.get("ultimo_ano")
    ult_t = sess.get("ultimos_termos",[])
    if not modelo and ult_m and len(consulta.split()) <= 3: modelo = ult_m
    if not ano and ult_a and modelo: ano = ult_a
    if not termos and ult_t and modelo: termos = ult_t
    if modelo: sess["ultimo_modelo"] = modelo
    if ano:    sess["ultimo_ano"]    = ano
    if termos: sess["ultimos_termos"] = termos

    # ── 1. Busca local ────────────────────────────────────────
    pecas = []
    if "ref" in consulta.lower() or "oem" in consulta.lower():
        pecas = buscar_ref(consulta)
    elif eh_codigo(consulta):
        pecas = buscar_codigo(consulta)
    elif modelo:
        pecas = buscar_veiculo(modelo, ano, termos)
        if not pecas and termos: pecas = buscar_desc(termos, modelo, ano)
    elif termos:
        pecas = buscar_desc(termos, ano=ano)

    # ── 2. Fuzzy local ────────────────────────────────────────
    if not pecas:
        print(f"🧠 Fuzzy → {consulta!r}")
        pecas = buscar_fuzzy(consulta)

    # ── 3. RapidAPI: busca por código OEM (fallback direto) ───
    if not pecas and rapidapi_ativo():
        codigo_api = consulta.strip()
        m = re.search(r"(ref|rf|oem)\s*[:\-]?\s*([a-zA-Z0-9\-.]+)", consulta.lower())
        if m: codigo_api = m.group(2)
        if eh_codigo(codigo_api) or "oem" in consulta.lower():
            try:
                pecas = buscar_cross_oem_api(codigo_api)
                if not pecas:
                    pecas = buscar_artigo_api(codigo_api)
            except Exception as e:
                print(f"❌ RapidAPI OEM: {e}")

    # ── 4. RapidAPI: busca por categoria/descrição ────────────
    if not pecas and rapidapi_ativo():
        try:
            # usa só termos relevantes para a query (remove modelo e ano)
            query_api = " ".join(termos) if termos else consulta
            pecas = buscar_peca_api(query_api)
        except Exception as e:
            print(f"❌ RapidAPI categoria: {e}")

    # ── Sem resultado ─────────────────────────────────────────
    if not pecas:
        _salvar_hist(sess, consulta)
        return fmt_vazio(consulta), sess

    # ── Resultado único → enriquece com equivalentes API ──────
    if len(pecas) == 1:
        peca = pecas[0]
        peca["equivalentes"] = []
        oem = peca.get("codigo_oem")
        if oem and rapidapi_ativo():
            try:
                equiv = buscar_cross_oem_api(oem)
                peca["equivalentes"] = [e for e in equiv if e.get("sku") != peca.get("sku")][:5]
                print(f"🌐 Equivalentes: {len(peca['equivalentes'])}")
            except Exception as e:
                print(f"❌ Enriquecimento: {e}")
        sess["peca_atual"] = peca
        sess["estado"]     = "detalhe"
        _salvar_hist(sess, consulta)
        return fmt_detalhe(peca), sess

    # ── Lista ─────────────────────────────────────────────────
    sess["opcoes"] = pecas[:10]
    sess["estado"] = "lista"
    _salvar_hist(sess, consulta)
    return fmt_lista(pecas[:10]), sess

def _tentar_escolha(cmd, opcoes, sess):
    if cmd.isdigit():
        n = int(cmd)
        if 1 <= n <= len(opcoes):
            peca = opcoes[n-1]
            peca["equivalentes"] = []
            oem = peca.get("codigo_oem")
            if oem and rapidapi_ativo():
                try:
                    equiv = buscar_cross_oem_api(oem)
                    peca["equivalentes"] = [e for e in equiv if e.get("sku") != peca.get("sku")][:5]
                except Exception:
                    pass
            sess["peca_atual"] = peca
            sess["estado"]     = "detalhe"
            return fmt_detalhe(peca), sess
        return f"❌ Número inválido. Digite de 1 a {len(opcoes)}.", sess
    if eh_codigo(cmd):
        for p in opcoes:
            if norm(cmd) in [norm(p.get("sku","")), norm(p.get("codigo_oem",""))]:
                p["equivalentes"] = []
                sess["peca_atual"] = p
                sess["estado"]     = "detalhe"
                return fmt_detalhe(p), sess
        pecas = buscar_codigo(cmd)
        if not pecas and rapidapi_ativo():
            pecas = buscar_cross_oem_api(cmd) or buscar_artigo_api(cmd)
        if pecas:
            if len(pecas) == 1:
                pecas[0]["equivalentes"] = []
                sess["peca_atual"] = pecas[0]
                sess["estado"]     = "detalhe"
                return fmt_detalhe(pecas[0]), sess
            sess["opcoes"] = pecas[:10]
            sess["estado"] = "lista"
            return fmt_lista(pecas[:10]), sess
    return None, sess

def _aplicacao(peca):
    if not peca: return "Peça não encontrada na sessão."
    sku = peca.get("sku","")
    c = sqlite3.connect(DB_PATH); cur = c.cursor()
    cur.execute("SELECT modelo,ano_inicio,ano_fim,motor_versao FROM fitment "
                "WHERE sku_autoflex=? ORDER BY modelo,ano_inicio LIMIT 20",(sku,))
    rows = cur.fetchall(); c.close()
    if not rows: return "Não encontrei aplicação detalhada.\n\nDigite 1, 2 ou 3 para continuar."
    txt = "Aplicação encontrada:\n\n"
    for mod,ai,af,motor in rows:
        txt += f"• {mod}"
        if ai:    txt += f" {ai}-{af or 'atual'}"
        if motor: txt += f" | {motor}"
        txt += "\n"
    return txt + "\n\nDigite 1, 2 ou 3 para continuar."

def _similares(peca, sess):
    if not peca: return "Peça não encontrada.", sess
    termos,_,_ = extrair(peca.get("descricao",""))
    if not termos: return "Não consegui identificar similares.", sess
    sim = [p for p in buscar_desc(termos[:2]) if p.get("sku") != peca.get("sku")]
    if not sim: return "Não encontrei peças similares.", sess
    sess["opcoes"] = sim[:10]
    sess["estado"] = "lista"
    return fmt_lista(sim[:10]), sess

def _salvar_hist(sess, consulta):
    h = sess.get("historico",[])
    h.append({"hora":datetime.now().strftime("%H:%M:%S"),"consulta":consulta})
    sess["historico"] = h[-10:]

def _historico(sess):
    h = sess.get("historico",[])
    if not h: return "Nenhum histórico ainda."
    txt = "Últimas buscas:\n\n"
    for item in h[-5:]: txt += f"- {item['hora']} | {item['consulta']}\n"
    return txt

def _ajuda():
    gemini_status  = f"✅ ({MODELO_VISAO})" if MODELO_VISAO else "❌ não configurado"
    rapidapi_status = "✅ ativa" if rapidapi_ativo() else "❌ não configurada"
    return ("Você pode buscar assim:\n\n"
            "• cabo embreagem palio 2006\n• palio 2008\n• 4002\n"
            "• oem 55204912\n• ref 55204912\n"
            "• 📷 envie uma foto da peça\n\n"
            "Depois de uma lista, responda com:\n"
            "• número da opção  • SKU  • OEM\n\n"
            "Após ver os detalhes, digite:\n"
            "1 → aplicação completa\n2 → peças similares\n3 → nova busca\n\n"
            f"Gemini visão: {gemini_status}\n"
            f"RapidAPI catálogo: {rapidapi_status}")

# ── Gemini: reconhecimento de imagem ─────────────────────────
def reconhecer_imagem(file_storage):
    if not cliente_genai or not MODELO_VISAO:
        return "", "Reconhecimento por imagem não configurado. Adicione GEMINI_API_KEY no .env."
    img_bytes = file_storage.read()
    try:
        img = PILImage.open(BytesIO(img_bytes))
        if img.mode not in ("RGB","L"): img = img.convert("RGB")
    except Exception as e:
        return "", f"Não foi possível abrir a imagem: {e}"

    prompt = (
        "Você é um especialista em peças automotivas. "
        "Analise a imagem e identifique qual peça automotiva está sendo mostrada. "
        "Responda APENAS com o nome técnico da peça em português, curto e direto. "
        "Exemplos: 'cabo de embreagem', 'amortecedor dianteiro', 'filtro de óleo'. "
        "Se não for uma peça automotiva, responda: 'não identificado'."
    )
    MAX_TENTATIVAS = 3
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            resp = cliente_genai.models.generate_content(model=MODELO_VISAO, contents=[prompt, img])
            desc = resp.text.strip()
            print(f"   Gemini identificou: {desc!r}")
            if not desc or "não identificado" in desc.lower():
                return "", "Não consegui identificar a peça. Tente foto mais próxima ou descreva em texto."
            return desc, ""
        except Exception as e:
            err = str(e)
            print(f"⚠️  Gemini erro (tentativa {tentativa}/{MAX_TENTATIVAS}): {err[:200]}")
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                m = re.search(r"retryDelay[\W]+(\d+)", err)
                delay = int(m.group(1))+2 if m else 35
                if tentativa < MAX_TENTATIVAS:
                    time.sleep(delay); continue
                return "", f"⏳ Limite Gemini atingido. Aguarde ~{delay}s ou descreva em texto."
            if "404" in err or "NOT_FOUND" in err:
                return "", f"Modelo Gemini indisponível. Reinicie o servidor."
            if "403" in err or "API_KEY" in err.upper():
                return "", "Chave GEMINI_API_KEY inválida."
            return "", f"Erro: {err[:100]}"
    return "", "Falha após múltiplas tentativas."

# ── Rotas ─────────────────────────────────────────────────────
ARQUIVOS_PERMITIDOS = {"index.html","favicon.ico"}

@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "index.html")

@app.route("/<path:filename>")
def static_files(filename):
    if filename not in ARQUIVOS_PERMITIDOS: return "", 404
    return send_from_directory(str(BASE_DIR), filename)

@app.route("/chat", methods=["POST"])
def chat():
    key, sess = get_sess()
    print(f"[{key}] estado={sess.get('estado')} opcoes={len(sess.get('opcoes',[]))}")

    if request.content_type and "multipart" in request.content_type:
        mensagem = request.form.get("message","").strip()
        imagem   = request.files.get("image")
    elif request.is_json:
        data     = request.get_json(silent=True) or {}
        mensagem = data.get("message","").strip()
        imagem   = None
    else:
        mensagem = request.form.get("message","").strip()
        imagem   = request.files.get("image")

    MSG_GENERICA = {"busca imagem","busca por imagem","identificar peça por imagem"}
    texto_e_generico = mensagem.lower().strip() in MSG_GENERICA

    if imagem and imagem.filename:
        desc, erro = reconhecer_imagem(imagem)
        if erro:
            if texto_e_generico or not mensagem:
                return jsonify({"response": f"📷 {erro}"})
            print(f"   Imagem falhou; usando texto: {mensagem!r}")
        elif desc:
            mensagem = f"{desc} {mensagem}" if mensagem and not texto_e_generico else desc
            print(f"   Busca por imagem: {mensagem!r}")

    if texto_e_generico:
        return jsonify({"response":"📷 Envie uma imagem junto com a mensagem para buscar por foto."}), 200
    if not mensagem:
        return jsonify({"error":"Nenhuma mensagem."}), 400

    resp_txt, sess = processar(mensagem, sess)
    save_sess(key, sess)
    print(f"[{key}] → estado={sess.get('estado')} | {resp_txt[:80]!r}")
    return jsonify({"response": resp_txt})

@app.route("/reset", methods=["POST"])
def reset_route():
    key, _ = get_sess()
    save_sess(key, estado_inicial())
    return jsonify({"ok": True})

@app.route("/ping")
def ping():
    key, sess = get_sess()
    return jsonify({
        "status":   "ok",
        "pecas":    len(df),
        "estado":   sess.get("estado","livre"),
        "gemini":   MODELO_VISAO or "não configurado",
        "rapidapi": "ativa" if rapidapi_ativo() else "não configurada",
        "client":   key,
    })

# ── Main ──────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("="*60)
    print(f"🚀  AutoFlex Backend  →  http://localhost:{port}")
    print(f"🤖  Gemini:   {MODELO_VISAO or 'não configurado'}")
    print(f"🌐  RapidAPI: {'ativa' if rapidapi_ativo() else 'não configurada'}")
    print("📦  Sessões em memória, chave = IP do cliente.")
    print("="*60)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)