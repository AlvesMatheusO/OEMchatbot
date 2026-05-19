#!/usr/bin/env python3
"""
Backend Flask – Assistente de Peças OEM
Estado conversacional salvo em arquivo JSON no servidor (pasta sessions/).
Cookie guarda apenas o session_id.
"""

import os, re, sys, sqlite3, json, uuid
import pandas as pd
from pathlib import Path
from datetime import datetime
from io import BytesIO

from flask import Flask, request, jsonify, send_from_directory, make_response
from flask_cors import CORS
from dotenv import load_dotenv
from rapidfuzz import fuzz

# Gemini opcional
try:
    from google import genai
    from PIL import Image as PILImage
    GEMINI_OK = True
except ImportError:
    GEMINI_OK = False

# ============================================================
# CONFIG
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

EXCEL_PATH = BASE_DIR / "data" / "CATALOGO_AUTOFLEX_BD_v1-3.xlsx"
DB_PATH = BASE_DIR / "autoflex_catalog.db"

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
cliente_genai = None
MODELO_VISAO = None
if GEMINI_OK and GEMINI_API_KEY:
    try:
        cliente_genai = genai.Client(api_key=GEMINI_API_KEY)
        MODELO_VISAO = "gemini-2.0-flash-exp"
        print("✅ Gemini pronto.")
    except Exception as e:
        print(f"⚠️ Gemini: {e}")

# ============================================================
# FLASK
# ============================================================
app = Flask(__name__, template_folder="templates", static_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
CORS(app, supports_credentials=True)

# ============================================================
# SESSÃO EM ARQUIVO
# ============================================================
COOKIE_NAME = "af_sid"

def _sid_from_request():
    return request.cookies.get(COOKIE_NAME)

def _load_session(sid):
    if not sid:
        return {}
    path = SESSIONS_DIR / f"{sid}.json"
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            return {}
    return {}

def _save_session(sid, data):
    path = SESSIONS_DIR / f"{sid}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, default=str), "utf-8")

def _ensure_sid(response, sid):
    response.set_cookie(
        COOKIE_NAME, sid,
        max_age=86400,
        httponly=True,
        samesite="Lax",
        secure=False,
    )
    return response

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

# ============================================================
# EXCEL
# ============================================================
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
        ["SKU Autoflex","Código OEM","Descrição","Montadora","Grupo","Veículo"]
    ).lower(), axis=1
)

# ============================================================
# SQLITE
# ============================================================
def init_db():
    c = sqlite3.connect(DB_PATH); cur = c.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            sku_autoflex TEXT PRIMARY KEY, codigo_oem TEXT, descricao TEXT,
            veiculo TEXT, montadora TEXT, linha TEXT, grupo TEXT,
            ncm TEXT, ipi_percent TEXT, codigo_barras TEXT
        );
        CREATE TABLE IF NOT EXISTS fitment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku_autoflex TEXT, montadora TEXT, modelo TEXT,
            ano_inicio INTEGER, ano_fim INTEGER, motor_versao TEXT, confidence REAL,
            FOREIGN KEY(sku_autoflex) REFERENCES products(sku_autoflex)
        );
        CREATE INDEX IF NOT EXISTS idx_prod_sku ON products(sku_autoflex);
        CREATE INDEX IF NOT EXISTS idx_prod_oem ON products(codigo_oem);
        CREATE INDEX IF NOT EXISTS idx_prod_desc ON products(descricao);
        CREATE INDEX IF NOT EXISTS idx_fit_mod ON fitment(modelo);
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
                "INSERT INTO fitment "
                "(sku_autoflex,montadora,modelo,ano_inicio,ano_fim,motor_versao,confidence) "
                "VALUES (?,?,?,?,?,?,?)",
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

# ============================================================
# UTILITÁRIOS
# ============================================================
def norm(s):
    return re.sub(r"[^a-zA-Z0-9]", "", str(s)).lower()

def eh_codigo(t):
    t = t.strip()
    return bool(re.fullmatch(r"[a-zA-Z0-9\-.]{4,}", t)) and len(t.split()) == 1

MODELOS = [
    "palio","uno","strada","siena","punto","linea","idea","doblo","doblô",
    "argo","cronos","mobi","fiorino","gol","fox","voyage","saveiro","virtus",
    "onix","prisma","cobalt","spin","corsa","celta","agile","montana",
    "toro","renegade","compass","hilux","corolla","hb20","civic","fit",
]
IGNORAR = {
    "quero","preciso","procuro","tem","vc","voce","você","me","manda","ver",
    "uma","um","o","a","os","as","de","do","da","dos","das","para","pra",
    "com","peca","peça","produto",
}

def extrair(texto):
    t = texto.lower().strip()
    modelo = next((m for m in MODELOS if re.search(rf"\b{re.escape(m)}\b", t)), None)
    ano_m = re.search(r"\b(19|20)\d{2}\b", t)
    ano = ano_m.group() if ano_m else None
    termos = [p for p in re.findall(r"[a-zA-ZÀ-ÿ0-9]+", t)
              if p not in IGNORAR and p != modelo and p != ano and len(p) > 2]
    return termos, modelo, ano

# ============================================================
# BUSCAS
# ============================================================
def row2p(r):
    return {
        "sku": str(r[0]).strip(),
        "codigo_oem": str(r[1]).strip() if len(r)>1 else "",
        "descricao": str(r[2]).strip() if len(r)>2 else "",
        "veiculo": str(r[3]).strip() if len(r)>3 else "",
        "montadora": str(r[4]).strip() if len(r)>4 else "",
        "grupo": str(r[5]).strip() if len(r)>5 else "",
        "ano_inicio": None,
        "ano_fim": None,
    }

def fit(pecas):
    if not pecas: return pecas
    c = sqlite3.connect(DB_PATH); cur = c.cursor()
    for p in pecas:
        cur.execute(
            "SELECT ano_inicio,ano_fim FROM fitment "
            "WHERE sku_autoflex=? ORDER BY confidence DESC,ano_inicio LIMIT 1",
            (p["sku"],))
        r = cur.fetchone()
        if r: p["ano_inicio"], p["ano_fim"] = r[0], r[1]
    c.close(); return pecas

def buscar_codigo(codigo):
    n = norm(codigo)
    c = sqlite3.connect(DB_PATH); cur = c.cursor()
    cur.execute("""
        SELECT sku_autoflex,codigo_oem,descricao,veiculo,montadora,grupo FROM products
        WHERE LOWER(REPLACE(REPLACE(sku_autoflex,'.','' ),'-',''))=?
           OR LOWER(REPLACE(REPLACE(codigo_oem, '.','' ),'-',''))=?
           OR codigo_oem LIKE ? OR sku_autoflex LIKE ?
        LIMIT 10""", (n,n,f"%{codigo}%",f"%{codigo}%"))
    rows = cur.fetchall(); c.close()
    return fit([row2p(r) for r in rows])

def buscar_ref(texto):
    m = re.search(r"(ref|rf|oem)\s*[:\-]?\s*([a-zA-Z0-9\-.]+)", texto.lower())
    return buscar_codigo(m.group(2)) if m else []

def buscar_veiculo(modelo, ano=None, termos=None):
    c = sqlite3.connect(DB_PATH); cur = c.cursor()
    q = """SELECT p.sku_autoflex,p.codigo_oem,p.descricao,p.veiculo,p.montadora,p.grupo,
                  f.modelo,f.ano_inicio,f.ano_fim,f.motor_versao,f.confidence
           FROM fitment f JOIN products p ON p.sku_autoflex=f.sku_autoflex
           WHERE LOWER(f.modelo) LIKE ?"""
    params = [f"%{modelo.lower()}%"]
    if ano:
        q += " AND (f.ano_inicio<=? AND (f.ano_fim>=? OR f.ano_fim IS NULL))"
        params += [int(ano), int(ano)]
    q += " ORDER BY f.confidence DESC LIMIT 30"
    cur.execute(q, params); rows = cur.fetchall(); c.close()
    pecas = []
    for r in rows:
        p = {
            "sku": str(r[0]).strip(), "codigo_oem": str(r[1]).strip(),
            "descricao": str(r[2]).strip(), "veiculo": str(r[3]).strip(),
            "montadora": str(r[4]).strip(), "grupo": str(r[5]).strip(),
            "modelo": str(r[6]).strip(), "ano_inicio": r[7], "ano_fim": r[8],
            "motor_versao": str(r[9]).strip(), "confidence": r[10],
        }
        if termos and not any(t in p["descricao"].lower() for t in termos):
            continue
        pecas.append(p)
    return pecas[:10]

def buscar_desc(termos, modelo=None, ano=None):
    c = sqlite3.connect(DB_PATH); cur = c.cursor()
    q = "SELECT sku_autoflex,codigo_oem,descricao,veiculo,montadora,grupo FROM products WHERE 1=1"
    params = []
    for t in termos:
        q += " AND LOWER(descricao) LIKE ?"; params.append(f"%{t.lower()}%")
    if modelo:
        q += " AND LOWER(veiculo) LIKE ?"; params.append(f"%{modelo.lower()}%")
    q += " LIMIT 20"
    cur.execute(q, params); rows = cur.fetchall(); c.close()
    return fit([row2p(r) for r in rows])

def buscar_fuzzy(consulta, limite=10):
    q = consulta.lower().strip(); res = []
    for _, row in df.iterrows():
        t = str(row["busca_texto"]).lower()
        sc = max(fuzz.partial_ratio(q,t), fuzz.token_sort_ratio(q,t), fuzz.token_set_ratio(q,t))
        if sc >= 65:
            res.append({
                "score": sc,
                "sku": str(row.get("SKU Autoflex","")).strip(),
                "codigo_oem": str(row.get("Código OEM","")).strip(),
                "descricao": str(row.get("Descrição","")).strip(),
                "veiculo": str(row.get("Veículo","")).strip(),
                "montadora": str(row.get("Montadora","")).strip(),
                "grupo": str(row.get("Grupo","")).strip(),
                "ano_inicio": None, "ano_fim": None,
            })
    res.sort(key=lambda x: x["score"], reverse=True)
    return fit(res[:limite])

# ============================================================
# FORMATAÇÃO
# ============================================================
def v(val):
    s = str(val).strip() if val is not None else ""
    return "" if s.lower() in ("nan","none","null","") else s

def fmt_resumo(p, n=None):
    txt = (f"{n}️⃣ " if n else "") + f"{v(p.get('descricao'))}\n"
    txt += f"SKU: {v(p.get('sku'))}"
    if v(p.get("codigo_oem")): txt += f" | OEM: {v(p['codigo_oem'])}"
    if p.get("ano_inicio"): txt += f" | Ano: {p['ano_inicio']} - {p.get('ano_fim') or 'atual'}"
    return txt

def fmt_detalhe(p):
    txt = "✅ Peça encontrada\n\n"
    txt += f"🔧 {v(p.get('descricao'))}\n\n"
    txt += f"SKU Autoflex: {v(p.get('sku'))}\n"
    if v(p.get("codigo_oem")): txt += f"OEM / Referência: {v(p['codigo_oem'])}\n"
    if v(p.get("montadora")): txt += f"Montadora: {v(p['montadora'])}\n"
    if v(p.get("grupo")): txt += f"Grupo: {v(p['grupo'])}\n"
    if v(p.get("veiculo")): txt += f"Aplicação: {v(p['veiculo'])}\n"
    if v(p.get("modelo")): txt += f"Modelo: {v(p['modelo'])}\n"
    if v(p.get("motor_versao")): txt += f"Motor/versão: {v(p['motor_versao'])}\n"
    if p.get("ano_inicio"): txt += f"Ano: {p['ano_inicio']} até {p.get('ano_fim') or 'atual'}\n"
    txt += "\nVocê pode pedir:\n1️⃣ aplicação completa\n2️⃣ peças similares\n3️⃣ nova busca"
    return txt

def fmt_lista(pecas):
    txt = f"Encontrei {len(pecas)} opção(ões):\n\n"
    for i, p in enumerate(pecas[:9], 1):
        txt += fmt_resumo(p, i) + "\n\n"
    txt += "Responda com o número, SKU, OEM ou mande uma nova busca."
    return txt

def fmt_vazio(q):
    return (f"Não encontrei uma peça com essa busca.\n\nBusca feita: {q}\n\n"
            "Tente assim:\n• cabo embreagem palio 2006\n• 4002\n• oem 55204912\n• amortecedor uno")

# ============================================================
# LÓGICA CONVERSACIONAL
# ============================================================
def processar(consulta, sess):
    consulta = consulta.strip()
    if not consulta:
        return "Digite uma peça, código, OEM ou veículo.", sess

    cmd = consulta.lower().strip()
    estado = sess.get("estado", "livre")

    # comandos globais
    if cmd == "/ajuda": return _ajuda(), sess
    if cmd == "/historico": return _historico(sess), sess
    if cmd in ("/limpar","sair"):
        sess.update(estado_inicial()); return "Contexto limpo. Nova busca pronta.", sess

    # estado detalhe (aguardando 1,2,3)
    if estado == "detalhe":
        peca = sess.get("peca_atual")
        if cmd == "1":
            sess["estado"] = "detalhe"  # mantém
            resp = _aplicacao(peca)
            return resp, sess
        if cmd == "2":
            return _similares(peca, sess)
        if cmd == "3":
            sess.update(estado_inicial())
            return "🔍 Nova busca. Digite o que deseja.", sess
        # qualquer outra coisa: reinicia
        sess.update(estado_inicial())
        return processar(consulta, sess)

    # estado lista (aguardando escolha)
    if estado == "lista":
        opcoes = sess.get("opcoes", [])
        resp, sess = _tentar_escolha(cmd, opcoes, sess)
        if resp is not None:
            return resp, sess
        # não foi escolha → nova busca
        sess.update(estado_inicial())
        return processar(consulta, sess)

    # busca normal
    return _nova_busca(consulta, sess)

def _nova_busca(consulta, sess):
    termos, modelo, ano = extrair(consulta)

    # contexto
    ult_m = sess.get("ultimo_modelo")
    ult_a = sess.get("ultimo_ano")
    ult_t = sess.get("ultimos_termos", [])
    if not modelo and ult_m and len(consulta.split()) <= 3: modelo = ult_m
    if not ano and ult_a and modelo: ano = ult_a
    if not termos and ult_t and modelo: termos = ult_t

    if modelo: sess["ultimo_modelo"] = modelo
    if ano: sess["ultimo_ano"] = ano
    if termos: sess["ultimos_termos"] = termos

    pecas = []
    if "ref" in consulta.lower() or "oem" in consulta.lower():
        pecas = buscar_ref(consulta)
    elif eh_codigo(consulta):
        pecas = buscar_codigo(consulta)
    elif modelo:
        pecas = buscar_veiculo(modelo, ano, termos)
        if not pecas and termos: pecas = buscar_desc(termos, modelo, ano)
    elif termos:
        pecas = buscar_desc(termos)

    if not pecas:
        pecas = buscar_fuzzy(consulta)

    if not pecas:
        _salvar_hist(sess, consulta)
        return fmt_vazio(consulta), sess

    if len(pecas) == 1:
        sess["peca_atual"] = pecas[0]
        sess["estado"] = "detalhe"
        _salvar_hist(sess, consulta)
        return fmt_detalhe(pecas[0]), sess

    sess["opcoes"] = pecas[:10]
    sess["estado"] = "lista"
    _salvar_hist(sess, consulta)
    return fmt_lista(pecas[:10]), sess

def _tentar_escolha(cmd, opcoes, sess):
    # por número
    if cmd.isdigit():
        n = int(cmd)
        if 1 <= n <= len(opcoes):
            peca = opcoes[n-1]
            sess["peca_atual"] = peca
            sess["estado"] = "detalhe"
            return fmt_detalhe(peca), sess
        return f"❌ Número inválido. Digite de 1 a {len(opcoes)}.", sess

    # por SKU/OEM
    if eh_codigo(cmd):
        for p in opcoes:
            if norm(cmd) in [norm(p.get("sku","")), norm(p.get("codigo_oem",""))]:
                sess["peca_atual"] = p
                sess["estado"] = "detalhe"
                return fmt_detalhe(p), sess
        # busca global
        pecas = buscar_codigo(cmd)
        if pecas:
            if len(pecas) == 1:
                sess["peca_atual"] = pecas[0]
                sess["estado"] = "detalhe"
                return fmt_detalhe(pecas[0]), sess
            sess["opcoes"] = pecas[:10]
            sess["estado"] = "lista"
            return fmt_lista(pecas[:10]), sess
    return None, sess

def _aplicacao(peca):
    if not peca: return "Peça não encontrada na sessão."
    sku = peca.get("sku","")
    c = sqlite3.connect(DB_PATH); cur = c.cursor()
    cur.execute(
        "SELECT modelo,ano_inicio,ano_fim,motor_versao FROM fitment "
        "WHERE sku_autoflex=? ORDER BY modelo,ano_inicio LIMIT 20", (sku,))
    rows = cur.fetchall(); c.close()
    if not rows: return "Não encontrei aplicação detalhada.\n\nDigite 1, 2 ou 3 para continuar."
    txt = "Aplicação encontrada:\n\n"
    for mod, ai, af, motor in rows:
        txt += f"• {mod}"
        if ai: txt += f" {ai}-{af or 'atual'}"
        if motor: txt += f" | {motor}"
        txt += "\n"
    return txt + "\n\nDigite 1, 2 ou 3 para continuar."

def _similares(peca, sess):
    if not peca: return "Peça não encontrada.", sess
    termos, _, _ = extrair(peca.get("descricao",""))
    if not termos: return "Não consegui identificar similares.", sess
    sim = [p for p in buscar_desc(termos[:2]) if p.get("sku") != peca.get("sku")]
    if not sim: return "Não encontrei peças similares.", sess
    sess["opcoes"] = sim[:10]
    sess["estado"] = "lista"
    return fmt_lista(sim[:10]), sess

def _salvar_hist(sess, consulta):
    h = sess.get("historico", [])
    h.append({"hora": datetime.now().strftime("%H:%M:%S"), "consulta": consulta})
    sess["historico"] = h[-10:]

def _historico(sess):
    h = sess.get("historico", [])
    if not h: return "Nenhum histórico ainda."
    txt = "Últimas buscas:\n\n"
    for item in h[-5:]: txt += f"- {item['hora']} | {item['consulta']}\n"
    return txt

def _ajuda():
    return ("Você pode buscar assim:\n\n"
            "• cabo embreagem palio 2006\n• palio 2008\n• 4002\n"
            "• oem 55204912\n• ref 55204912\n\n"
            "Depois de uma lista, responda com:\n"
            "• número da opção  • SKU  • OEM\n\n"
            "Após ver os detalhes, digite:\n"
            "1 → aplicação completa\n2 → peças similares\n3 → nova busca")

# ============================================================
# GEMINI (reconhecimento de imagem)
# ============================================================
def reconhecer_imagem(file_storage):
    if not cliente_genai: return ""
    try:
        img = PILImage.open(BytesIO(file_storage.read()))
        resp = cliente_genai.models.generate_content(
            model=MODELO_VISAO,
            contents=["Identifique a peça automotiva nesta imagem. "
                      "Responda APENAS com o nome da peça em português, clara e curta.", img])
        return resp.text.strip()
    except Exception as e:
        print(f"⚠️ Gemini: {e}"); return ""

# ============================================================
# ROTAS
# ============================================================
@app.route("/")
def index():
    return send_from_directory("templates", "index.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory("templates", filename)

@app.route("/chat", methods=["POST"])
def chat():
    sid = _sid_from_request()
    if not sid:
        sid = str(uuid.uuid4())
    sess = _load_session(sid) or estado_inicial()

    if request.content_type and "multipart" in request.content_type:
        mensagem = request.form.get("message", "").strip()
        imagem = request.files.get("image")
    elif request.is_json:
        data = request.get_json(silent=True) or {}
        mensagem = data.get("message", "").strip()
        imagem = None
    else:
        mensagem = request.form.get("message", "").strip()
        imagem = request.files.get("image")

    if imagem and imagem.filename:
        desc = reconhecer_imagem(imagem)
        if desc:
            mensagem = f"busca imagem: {desc}"
        elif not mensagem:
            return jsonify({"response": "📷 Imagem recebida, mas reconhecimento não configurado.\nAdicione GEMINI_API_KEY no .env ou descreva a peça em texto."})

    if not mensagem:
        return jsonify({"error": "Nenhuma mensagem."}), 400

    resp_txt, sess = processar(mensagem, sess)
    _save_session(sid, sess)
    response = make_response(jsonify({"response": resp_txt}))
    _ensure_sid(response, sid)
    return response

@app.route("/reset", methods=["POST"])
def reset_route():
    sid = _sid_from_request()
    if sid:
        _save_session(sid, estado_inicial())
    response = make_response(jsonify({"ok": True}))
    if sid:
        _ensure_sid(response, sid)
    return response

@app.route("/ping")
def ping():
    sid = _sid_from_request()
    sess = _load_session(sid) if sid else {}
    return jsonify({
        "status": "ok",
        "pecas": len(df),
        "estado": sess.get("estado", "livre"),
        "sid": sid or "sem sessão",
    })

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    print("="*60)
    print("🚀  AutoFlex Backend  →  http://localhost:5000")
    print(f"📁  Sessões em:       {SESSIONS_DIR}")
    print("="*60)
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)