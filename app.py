#!/usr/bin/env python3
"""
Backend Flask – Assistente de Peças OEM
Estado em memória, chave = IP do cliente (sem cookie, sem arquivo).
"""

import os, re, sys, sqlite3, time
import pandas as pd
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

MODELOS_CANDIDATOS = [
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-flash-latest",
    "gemini-1.5-pro",
    "gemini-1.5-pro-latest",
]

def _init_gemini():
    global cliente_genai, MODELO_VISAO
    if not GEMINI_OK or not GEMINI_API_KEY:
        print("⚠️  Gemini não configurado (sem GEMINI_API_KEY ou sem google-genai instalado).")
        return
    try:
        cliente_genai = genai.Client(api_key=GEMINI_API_KEY)
        disponiveis = {m.name.split("/")[-1] for m in cliente_genai.models.list()}
        print(f"   Modelos disponíveis: {sorted(disponiveis)}")
        for cand in MODELOS_CANDIDATOS:
            if cand in disponiveis:
                MODELO_VISAO = cand
                print(f"✅ Gemini pronto  →  modelo: {MODELO_VISAO}")
                return
        MODELO_VISAO = MODELOS_CANDIDATOS[0]
        print(f"⚠️  Nenhum modelo preferido disponível; tentando {MODELO_VISAO} assim mesmo.")
    except Exception as e:
        print(f"⚠️  Falha ao inicializar Gemini: {e}")

_init_gemini()

# ── Flask — serve arquivos estáticos da raiz do projeto ───────
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
        CREATE INDEX IF NOT EXISTS idx_prod_sku  ON products(sku_autoflex);
        CREATE INDEX IF NOT EXISTS idx_prod_oem  ON products(codigo_oem);
        CREATE INDEX IF NOT EXISTS idx_prod_desc ON products(descricao);
        CREATE INDEX IF NOT EXISTS idx_fit_mod   ON fitment(modelo);
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
    "com","peca","peça","produto",
}

def extrair(texto):
    t = texto.lower().strip()
    modelo = next((m for m in MODELOS_VEICULO if re.search(rf"\b{re.escape(m)}\b",t)), None)
    ano_m  = re.search(r"\b(19|20)\d{2}\b", t)
    ano    = ano_m.group() if ano_m else None
    termos = [p for p in re.findall(r"[a-zA-ZÀ-ÿ0-9]+",t)
              if p not in IGNORAR and p != modelo and p != ano and len(p) > 2]
    return termos, modelo, ano

# ── Buscas ────────────────────────────────────────────────────
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
    q += " ORDER BY f.confidence DESC LIMIT 30"
    cur.execute(q,params); rows = cur.fetchall(); c.close()
    pecas = []
    for r in rows:
        p = {"sku":str(r[0]).strip(),"codigo_oem":str(r[1]).strip(),
             "descricao":str(r[2]).strip(),"veiculo":str(r[3]).strip(),
             "montadora":str(r[4]).strip(),"grupo":str(r[5]).strip(),
             "modelo":str(r[6]).strip(),"ano_inicio":r[7],"ano_fim":r[8],
             "motor_versao":str(r[9]).strip(),"confidence":r[10]}
        if termos and not any(t in p["descricao"].lower() for t in termos): continue
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
    cur.execute(q,params); rows = cur.fetchall(); c.close()
    return enrich([row2p(r) for r in rows])

def buscar_fuzzy(consulta, limite=10):
    q = consulta.lower().strip(); res = []
    for _, row in df.iterrows():
        t = str(row["busca_texto"]).lower()
        sc = max(fuzz.partial_ratio(q,t),fuzz.token_sort_ratio(q,t),fuzz.token_set_ratio(q,t))
        if sc >= 65:
            res.append({"score":sc,"sku":str(row.get("SKU Autoflex","")).strip(),
                "codigo_oem":str(row.get("Código OEM","")).strip(),
                "descricao":str(row.get("Descrição","")).strip(),
                "veiculo":str(row.get("Veículo","")).strip(),
                "montadora":str(row.get("Montadora","")).strip(),
                "grupo":str(row.get("Grupo","")).strip(),
                "ano_inicio":None,"ano_fim":None})
    res.sort(key=lambda x:x["score"],reverse=True)
    return enrich(res[:limite])

# ── Formatação ────────────────────────────────────────────────
def v(val):
    s = str(val).strip() if val is not None else ""
    return "" if s.lower() in ("nan","none","null","") else s

def fmt_resumo(p, n=None):
    txt  = (f"{n}️⃣ " if n else "") + f"{v(p.get('descricao'))}\n"
    txt += f"SKU: {v(p.get('sku'))}"
    if v(p.get("codigo_oem")): txt += f" | OEM: {v(p['codigo_oem'])}"
    if p.get("ano_inicio"):    txt += f" | Ano: {p['ano_inicio']} - {p.get('ano_fim') or 'atual'}"
    return txt

def fmt_detalhe(p):
    txt  = "✅ Peça encontrada\n\n"
    txt += f"🔧 {v(p.get('descricao'))}\n\n"
    txt += f"SKU Autoflex: {v(p.get('sku'))}\n"
    if v(p.get("codigo_oem")):   txt += f"OEM / Referência: {v(p['codigo_oem'])}\n"
    if v(p.get("montadora")):    txt += f"Montadora: {v(p['montadora'])}\n"
    if v(p.get("grupo")):        txt += f"Grupo: {v(p['grupo'])}\n"
    if v(p.get("veiculo")):      txt += f"Aplicação: {v(p['veiculo'])}\n"
    if v(p.get("modelo")):       txt += f"Modelo: {v(p['modelo'])}\n"
    if v(p.get("motor_versao")): txt += f"Motor/versão: {v(p['motor_versao'])}\n"
    if p.get("ano_inicio"):      txt += f"Ano: {p['ano_inicio']} até {p.get('ano_fim') or 'atual'}\n"
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

    if not pecas: pecas = buscar_fuzzy(consulta)

    if not pecas:
        _salvar_hist(sess, consulta)
        return fmt_vazio(consulta), sess

    if len(pecas) == 1:
        sess["peca_atual"] = pecas[0]
        sess["estado"]     = "detalhe"
        _salvar_hist(sess, consulta)
        return fmt_detalhe(pecas[0]), sess

    sess["opcoes"] = pecas[:10]
    sess["estado"] = "lista"
    _salvar_hist(sess, consulta)
    return fmt_lista(pecas[:10]), sess

def _tentar_escolha(cmd, opcoes, sess):
    if cmd.isdigit():
        n = int(cmd)
        if 1 <= n <= len(opcoes):
            peca = opcoes[n-1]
            sess["peca_atual"] = peca
            sess["estado"]     = "detalhe"
            return fmt_detalhe(peca), sess
        return f"❌ Número inválido. Digite de 1 a {len(opcoes)}.", sess
    if eh_codigo(cmd):
        for p in opcoes:
            if norm(cmd) in [norm(p.get("sku","")), norm(p.get("codigo_oem",""))]:
                sess["peca_atual"] = p
                sess["estado"]     = "detalhe"
                return fmt_detalhe(p), sess
        pecas = buscar_codigo(cmd)
        if pecas:
            if len(pecas) == 1:
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
    gemini_status = f"✅ ativo ({MODELO_VISAO})" if MODELO_VISAO else "❌ não configurado"
    return (f"Você pode buscar assim:\n\n"
            "• cabo embreagem palio 2006\n• palio 2008\n• 4002\n"
            "• oem 55204912\n• ref 55204912\n"
            "• 📷 envie uma foto da peça\n\n"
            "Depois de uma lista, responda com:\n"
            "• número da opção  • SKU  • OEM\n\n"
            "Após ver os detalhes, digite:\n"
            "1 → aplicação completa\n2 → peças similares\n3 → nova busca\n\n"
            f"Reconhecimento por imagem: {gemini_status}")

# ── Gemini: reconhecimento de imagem ─────────────────────────
def reconhecer_imagem(file_storage) -> tuple[str, str]:
    if not cliente_genai or not MODELO_VISAO:
        return "", ("Reconhecimento por imagem não configurado. "
                    "Adicione GEMINI_API_KEY no arquivo .env e reinicie o servidor.")

    img_bytes = file_storage.read()
    try:
        img = PILImage.open(BytesIO(img_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
    except Exception as e:
        return "", f"Não foi possível abrir a imagem: {e}"

    prompt = (
        "Você é um especialista em peças automotivas. "
        "Analise a imagem e identifique qual peça automotiva está sendo mostrada. "
        "Responda APENAS com o nome técnico da peça em português, de forma curta e direta. "
        "Exemplos de resposta: 'cabo de embreagem', 'amortecedor dianteiro', "
        "'filtro de óleo', 'pastilha de freio', 'correia dentada'. "
        "Se não conseguir identificar uma peça automotiva, responda: 'não identificado'."
    )

    MAX_TENTATIVAS = 3
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        try:
            resp      = cliente_genai.models.generate_content(
                model=MODELO_VISAO, contents=[prompt, img])
            descricao = resp.text.strip()
            print(f"   Gemini identificou: {descricao!r}")
            if not descricao or "não identificado" in descricao.lower():
                return "", ("Não consegui identificar a peça na imagem. "
                            "Tente uma foto mais próxima e com boa iluminação, "
                            "ou descreva a peça em texto.")
            return descricao, ""
        except Exception as e:
            err_str = str(e)
            print(f"⚠️  Gemini erro (tentativa {tentativa}/{MAX_TENTATIVAS}): {err_str[:300]}")
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                m_delay = re.search(r"retryDelay[\W]+(\d+)", err_str)
                delay   = int(m_delay.group(1)) + 2 if m_delay else 35
                if tentativa < MAX_TENTATIVAS:
                    print(f"   Rate-limit: aguardando {delay}s…")
                    time.sleep(delay)
                    continue
                return "", (f"⏳ Limite de uso gratuito do Gemini atingido. "
                            f"Aguarde ~{delay}s e tente novamente, "
                            "ou descreva a peça em texto.")
            if "404" in err_str or "NOT_FOUND" in err_str:
                return "", (f"Modelo Gemini indisponível ({MODELO_VISAO}). "
                            "Verifique sua chave de API e reinicie o servidor.")
            if "403" in err_str or "API_KEY" in err_str.upper():
                return "", "Chave GEMINI_API_KEY inválida ou sem permissão. Verifique o .env."
            return "", f"Erro no reconhecimento: {err_str[:120]}"

    return "", "Falha após múltiplas tentativas. Tente novamente mais tarde."

# ── Rotas ─────────────────────────────────────────────────────
# Servindo index.html diretamente da raiz do projeto (sem pasta templates/)
ARQUIVOS_PERMITIDOS = {"index.html", "favicon.ico"}

@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "index.html")

@app.route("/<path:filename>")
def static_files(filename):
    # permite apenas arquivos explicitamente listados para não expor código-fonte
    if filename not in ARQUIVOS_PERMITIDOS:
        return "", 404
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

    MSG_GENERICA = {"busca imagem", "busca por imagem", "identificar peça por imagem"}
    texto_e_generico = mensagem.lower().strip() in MSG_GENERICA

    if imagem and imagem.filename:
        desc, erro = reconhecer_imagem(imagem)
        if erro:
            if texto_e_generico or not mensagem:
                return jsonify({"response": f"📷 {erro}"})
            else:
                print(f"   Imagem falhou; usando texto do usuário: {mensagem!r}")
        elif desc:
            if mensagem and not texto_e_generico:
                mensagem = f"{desc} {mensagem}"
            else:
                mensagem = desc
            print(f"   Busca por imagem: {mensagem!r}")

    if texto_e_generico:
        return jsonify({"response": "📷 Envie uma imagem junto com a mensagem para usar a busca por foto."}), 200

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
        "status":  "ok",
        "pecas":   len(df),
        "estado":  sess.get("estado","livre"),
        "gemini":  MODELO_VISAO or "não configurado",
        "client":  key,
    })

# ── Main ──────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("="*60)
    print(f"🚀  AutoFlex Backend  →  http://localhost:{port}")
    print(f"🤖  Gemini visão:       {MODELO_VISAO or 'não configurado'}")
    print("📦  Sessões em memória, chave = IP do cliente.")
    print("="*60)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)