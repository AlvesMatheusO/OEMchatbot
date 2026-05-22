#!/usr/bin/env python3
"""
Backend Flask – Assistente de Peças OEM
Fonte: Auto Parts Catalog API (RapidAPI)
Estrutura JSON mapeada via inspeção real da API.
"""

import os, re, time
from pathlib import Path
from datetime import datetime
from io import BytesIO

import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

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
BASE_DIR       = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

RAPIDAPI_KEY   = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST  = "auto-parts-catalog.p.rapidapi.com"
BASE_URL       = f"https://{RAPIDAPI_HOST}"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

LANG_ID    = 4    # English
COUNTRY_ID = 63   # Brasil
TYPE_ID    = 1    # Automóveis

MODELOS_GEMINI = ["gemini-2.0-flash","gemini-1.5-flash",
                  "gemini-1.5-flash-latest","gemini-1.5-pro"]

# ── Gemini ────────────────────────────────────────────────────
def _init_gemini():
    global cliente_genai, MODELO_VISAO
    if not GEMINI_OK or not GEMINI_API_KEY: return
    try:
        cliente_genai = genai.Client(api_key=GEMINI_API_KEY)
        disp = {m.name.split("/")[-1] for m in cliente_genai.models.list()}
        for c in MODELOS_GEMINI:
            if c in disp:
                MODELO_VISAO = c
                print(f"✅ Gemini → {MODELO_VISAO}"); return
    except Exception as e:
        print(f"⚠️ Gemini: {e}")

_init_gemini()

# ── Flask ─────────────────────────────────────────────────────
app = Flask(__name__, template_folder=str(BASE_DIR), static_folder=str(BASE_DIR))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
CORS(app, supports_credentials=True)

# ── Sessões em memória ────────────────────────────────────────
_SESSIONS: dict = {}

def _client_key():
    return request.headers.get("X-Forwarded-For", request.remote_addr) or "local"

def get_sess():
    k = _client_key()
    if k not in _SESSIONS:
        _SESSIONS[k] = _novo_estado()
    return k, _SESSIONS[k]

def save_sess(k, s):
    _SESSIONS[k] = s

def _novo_estado():
    return {
        "estado": "livre",
        "opcoes": [],
        "peca_atual": None,
        "mfr_id": None,  "mfr_nome": None,
        "mod_id": None,  "mod_nome": None,
        "veh_id": None,  "veh_nome": None,
        "cat_id": None,  "cat_nome": None,
        "historico": [],
        "_pendente": None,
    }

# ── Cache + HTTP ──────────────────────────────────────────────
_CACHE: dict = {}

def _get(path: str, params: dict | None = None):
    ck = path + str(sorted((params or {}).items()))
    if ck in _CACHE: return _CACHE[ck]
    if not RAPIDAPI_KEY:
        print("❌ RAPIDAPI_KEY não configurada"); return None
    hdrs = {"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": RAPIDAPI_KEY}
    url  = f"{BASE_URL}/{path.lstrip('/')}"
    try:
        r = requests.get(url, headers=hdrs, params=params or {}, timeout=15)
        r.raise_for_status()
        d = r.json(); _CACHE[ck] = d; return d
    except requests.HTTPError as e:
        print(f"❌ {e.response.status_code} /{path} {params}"); return None
    except Exception as e:
        print(f"❌ {e}"); return None

# ── Parsers — cada endpoint tem estrutura própria ─────────────

def api_fabricantes() -> list:
    """{"countManufactures": N, "manufacturers": [{"manufacturerId":N, "manufacturerName":"..."}]}"""
    d = _get(f"manufacturers/list/type-id/{TYPE_ID}")
    if not d: return []
    return d.get("manufacturers", [])

def api_modelos(mfr_id) -> list:
    """Estrutura variável — tenta todas as chaves conhecidas"""
    d = _get(
        f"models/list/type-id/{TYPE_ID}/manufacturer-id/{mfr_id}"
        f"/lang-id/{LANG_ID}/country-filter-id/{COUNTRY_ID}"
    )
    if not d: return []
    if isinstance(d, list): return d
    for k in ("models","modelSeries","vehicleModels","data","result","items"):
        if k in d and isinstance(d[k], list):
            return d[k]
    # fallback: primeiro valor que seja lista
    for v in d.values():
        if isinstance(v, list): return v
    return []

def api_motores(model_id) -> list:
    """Lista de versões/veículos para um modelo"""
    d = _get(
        f"types/type-id/{TYPE_ID}/list-vehicles-types/{model_id}"
        f"/lang-id/{LANG_ID}/country-filter-id/{COUNTRY_ID}"
    )
    if not d: return []
    if isinstance(d, list): return d
    for k in ("vehicles","types","vehicleTypes","data","result","items"):
        if k in d and isinstance(d[k], list):
            return d[k]
    for v in d.values():
        if isinstance(v, list): return v
    return []

def api_categorias() -> list:
    """
    Retorna dict aninhado:
    {"Braking System": {"categoryId":N, "categoryName":"...", "children": {...}}}
    → achata em lista plana de {"categoryId", "categoryName"}
    """
    d = _get(f"category/type-id/{TYPE_ID}/list-category-tree-structure/lang-id/{LANG_ID}")
    if not d: return []
    if isinstance(d, list): return d

    result = []
    def _achatar(obj):
        if not isinstance(obj, dict): return
        cid  = obj.get("categoryId")
        nome = obj.get("categoryName","")
        if cid:
            result.append({"categoryId": cid, "categoryName": nome})
        children = obj.get("children", {})
        if isinstance(children, dict):
            for child in children.values():
                _achatar(child)
        elif isinstance(children, list):
            for child in children:
                _achatar(child)

    for top in d.values():
        _achatar(top)
    return result

def api_artigos(veh_id, cat_id) -> list:
    """{"countArticles": N, "articles": [...]}"""
    d = _get(
        f"articles/list/type-id/{TYPE_ID}/vehicle-id/{veh_id}"
        f"/category-id/{cat_id}/lang-id/{LANG_ID}"
    )
    if not d: return []
    if isinstance(d, list): return d
    return d.get("articles", d.get("data", d.get("result", [])))

def api_busca_numero(nr: str) -> list:
    """{"countArticles": N, "articles": [...]}"""
    d = _get("artlookup/search-articles-by-article-no", {
        "langId": LANG_ID, "articleNo": nr, "articleType": "ArticleNumber"
    })
    if not d: return []
    if isinstance(d, list): return d
    return d.get("articles", [])

def api_busca_oem(nr: str) -> list:
    d = _get("artlookup/search-articles-by-article-no", {
        "langId": LANG_ID, "articleNo": nr, "articleType": "OENumber"
    })
    if not d: return []
    if isinstance(d, list): return d
    return d.get("articles", [])

def api_busca_auto(nr: str) -> list:
    r = api_busca_numero(nr)
    return r if r else api_busca_oem(nr)

def api_compat(article_no: str, supplier_id) -> list:
    d = _get(
        f"articles/get-compatible-cars-by-article-number/type-id/{TYPE_ID}",
        {"articleNo": article_no, "supplierId": supplier_id,
         "langId": LANG_ID, "countryFilterId": COUNTRY_ID},
    )
    if not d: return []
    if isinstance(d, list): return d
    for k in ("vehicles","cars","data","result","items"):
        if k in d and isinstance(d[k], list): return d[k]
    return []

# ── Cache aquecido ────────────────────────────────────────────
_FABS_CACHE:  list = []   # todos os 698 fabricantes
_CATS_CACHE:  list = []   # categorias achatadas
_PRODS_CACHE: list = []   # 11092 nomes de produtos

def _aquecer():
    global _FABS_CACHE, _CATS_CACHE, _PRODS_CACHE
    print("🔄 Carregando fabricantes…")
    _FABS_CACHE = api_fabricantes()
    print(f"   {len(_FABS_CACHE)} fabricantes.")
    print("🔄 Carregando categorias…")
    _CATS_CACHE = api_categorias()
    print(f"   {len(_CATS_CACHE)} categorias.")
    print("🔄 Carregando nomes de produtos…")
    d = _get(f"category/list-products-names/lang-id/{LANG_ID}")
    _PRODS_CACHE = d if isinstance(d, list) else []
    print(f"   {len(_PRODS_CACHE)} produtos.")

# ── Accessors de campos (suportam vários nomes de chave) ──────

def _id_fab(it):
    return it.get("manufacturerId") or it.get("mfrId") or it.get("id")

def _nome_fab(it):
    return _v(it.get("manufacturerName") or it.get("mfrName") or it.get("name",""))

def _id_mod(it):
    return it.get("modelId") or it.get("vehicleModelSeriesId") or it.get("id")

def _nome_mod(it):
    return _v(it.get("modelName") or it.get("vehicleModelSeriesName") or
              it.get("name") or it.get("description",""))

def _ano_mod(it):
    """Retorna (ano_inicio, ano_fim) do modelo como strings 4-char."""
    ini = str(it.get("modelYearFrom") or it.get("yearOfConstrFrom") or
              it.get("constructionYearFrom") or "")[:4]
    fim = str(it.get("modelYearTo")   or it.get("yearOfConstrTo")   or
              it.get("constructionYearTo")   or "")[:4]
    return ini, fim

def _id_veh(it):
    return it.get("vehicleId") or it.get("carId") or it.get("id")

def _nome_veh(it):
    return _v(it.get("fulldescription") or it.get("description") or
              it.get("name") or it.get("vehicleName",""))

def _id_cat(it):
    return it.get("categoryId") or it.get("genericArticleId") or it.get("id")

def _nome_cat(it):
    return _v(it.get("categoryName") or it.get("genericArticleDescription") or
              it.get("name") or it.get("description",""))

def _id_art(it):
    return it.get("articleId") or it.get("id")

def _nome_art(it):
    return _v(it.get("articleProductName") or it.get("articleName") or
              it.get("description") or it.get("name",""))

def _ref_art(it):
    return _v(it.get("articleNo") or it.get("articleNumber") or
              it.get("articleSearchNo",""))

def _marca_art(it):
    return _v(it.get("supplierName") or it.get("brandName",""))

# ── Utilitários ───────────────────────────────────────────────
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())

def _v(val):
    s = str(val).strip() if val is not None else ""
    return "" if s.lower() in ("nan","none","null","") else s

IGNORAR = {
    "quero","preciso","procuro","tem","voce","você","me","manda","ver",
    "uma","um","o","a","os","as","de","do","da","dos","das","para","pra",
    "com","peca","peça","produto","carro","veiculo","veículo","qual",
    "tenho","meu","minha","pelo","pela","preciso","buscar","busco",
}

def _termos(txt, ex=None):
    return [p for p in re.findall(r"[a-zA-ZÀ-ÿ0-9]+", txt.lower())
            if p not in IGNORAR and p not in (ex or []) and len(p) > 2]

def _norm_match(texto, nome):
    """Retorna True se texto aparece (normalizado) dentro de nome."""
    tn, nn = _norm(texto), _norm(nome)
    return tn in nn or any(_norm(p) in nn for p in texto.split() if len(p) > 2)

def _melhor_match(texto, lista, *campos):
    """Retorna o item de lista com maior sobreposição a texto."""
    tn = _norm(texto); hits = []
    for it in lista:
        for c in campos:
            nn = _norm(str(it.get(c, "")))
            if not nn: continue
            if tn in nn:
                hits.append((len(tn) / max(len(nn), 1), it)); break
            elif any(_norm(p) in nn for p in texto.split() if len(p) > 2):
                hits.append((0.3, it)); break
    hits.sort(key=lambda x: x[0], reverse=True)
    return hits[0][1] if hits else None

def _extrair_ano(txt):
    m = re.search(r"\b(19|20)\d{2}\b", txt)
    return m.group() if m else None

def _veh_no_ano(veh, ano):
    try:
        a = int(ano)
        ini, fim = _ano_mod(veh)
        if ini and int(ini) > a: return False
        if fim and int(fim) < a: return False
    except Exception: pass
    return True

# ── Formatação ────────────────────────────────────────────────
def _fmt_lista(lista, tipo="itens"):
    if not lista:
        return f"Nenhum(a) {tipo} encontrado(a)."
    txt = f"Encontrei {len(lista)} {tipo}:\n\n"
    for i, it in enumerate(lista[:12], 1):
        # tenta nome pelo tipo correto
        n = (_nome_fab(it) or _nome_mod(it) or _nome_veh(it) or
             _nome_cat(it) or _nome_art(it) or "?")
        ini, fim = _ano_mod(it)
        txt += f"  {i}. {n}"
        if ini: txt += f" ({ini}" + (f"–{fim}" if fim else "") + ")"
        txt += "\n"
    txt += "\nDigite o número ou escreva o nome:"
    return txt

def _fmt_detalhe(a):
    nome  = _nome_art(a)
    ref   = _ref_art(a)
    marca = _marca_art(a)
    txt   = "✅ Peça encontrada\n\n"
    txt  += f"  🔧 {nome}\n\n"
    if ref:   txt += f"  Referência: {ref}\n"
    if marca: txt += f"  Marca:      {marca}\n"
    crit = a.get("criteria") or a.get("attributes") or []
    if isinstance(crit, list):
        for c in crit[:6]:
            cn = _v(c.get("criteriaDescription") or c.get("name",""))
            cv = _v(c.get("rawValue") or c.get("value",""))
            un = _v(c.get("criteriaUnitDescription") or c.get("unit",""))
            if cn and cv: txt += f"  {cn}: {cv}{' '+un if un else ''}\n"
    txt += "\n  1 → veículos compatíveis  2 → similares  3 → nova busca"
    return txt

def _fmt_vazio(q):
    return (
        f"Não encontrei resultados para: *{q}*\n\n"
        "Tente:\n"
        "  • fiat palio 2006 cabo embreagem\n"
        "  • renault kwid amortecedor\n"
        "  • oem 7700115294\n"
        "  • /ajuda"
    )

# ── Processador principal ─────────────────────────────────────
def processar(consulta: str, sess: dict):
    consulta = consulta.strip()
    if not consulta:
        return "Digite uma peça, código OEM ou veículo.", sess

    cmd    = consulta.lower().strip()
    estado = sess["estado"]

    if cmd in ("/ajuda","ajuda","help"):
        return _ajuda(), sess
    if cmd in ("/historico","historico"):
        return _historico(sess), sess
    if cmd in ("/limpar","limpar","sair","reset","novo"):
        return "🔍 Contexto limpo.", _novo_estado()

    # ── detalhe ───────────────────────────────────────────────
    if estado == "detalhe":
        if cmd == "1": return _compat(sess)
        if cmd == "2": return _similares(sess)
        if cmd == "3": return "🔍 Nova busca.", _novo_estado()
        return processar(consulta, _novo_estado())

    # ── lista ─────────────────────────────────────────────────
    if estado == "lista":
        r, s = _escolha(cmd, sess)
        if r is not None: return r, s
        return processar(consulta, _novo_estado())

    # ── guiado ────────────────────────────────────────────────
    if estado.startswith("guiado_"):
        return _guiado(consulta, cmd, sess)

    return _livre(consulta, sess)

# ── Busca livre ───────────────────────────────────────────────
NOMES_CARROS = {
    "palio","gol","uno","corsa","civic","hilux","corolla","onix","hb20",
    "fiat","volkswagen","vw","toyota","honda","chevrolet","ford","renault",
    "hyundai","nissan","peugeot","citroen","mitsubishi","kia","jeep",
    "strada","creta","kwid","sandero","logan","ka","fiesta","ecosport",
    "ranger","duster","captur","stepway","oroch","tucson","yaris","etios",
    "sw4","rav4","hr-v","hrv","wrv","wr-v","crv","cr-v","brv","br-v",
    "tracker","spin","montana","agile","cobalt","celta","kadett","monza",
    "siena","argo","cronos","mobi","fiorino","doblo","toro","pulse",
    "polo","virtus","voyage","saveiro","fox","up","t-cross","taos","nivus",
}

def _livre(consulta: str, sess: dict):
    txt = consulta.lower()
    ano = _extrair_ano(txt)

    # 1) Código/OEM: contém dígitos, sem palavras de carro
    tok      = consulta.strip()
    palavras = set(re.findall(r"[a-zA-Z]+", txt))
    tem_num  = bool(re.search(r"\d{4,}", tok))
    eh_cod   = tem_num and not (palavras & NOMES_CARROS)

    if eh_cod or "oem" in txt or "ref" in txt:
        nr = re.sub(r"(?i)(oem|ref)\s*:?\s*", "", tok).strip()
        return _busca_numero(nr, sess)

    # 2) Tenta identificar fabricante no texto (busca em TODOS os 698)
    fabs      = _FABS_CACHE or api_fabricantes()
    fab_match = _melhor_match(txt, fabs, "manufacturerName", "mfrName")

    if fab_match:
        mfr_id   = _id_fab(fab_match)
        mfr_nome = _nome_fab(fab_match)
        sess["mfr_id"]   = mfr_id
        sess["mfr_nome"] = mfr_nome

        modelos   = api_modelos(mfr_id)
        mod_match = _melhor_match(txt, modelos, "modelName",
                                  "vehicleModelSeriesName","name","description")

        if mod_match:
            mod_id   = _id_mod(mod_match)
            mod_nome = _nome_mod(mod_match)
            sess["mod_id"]   = mod_id
            sess["mod_nome"] = mod_nome

            motores = api_motores(mod_id)
            if ano:
                f2 = [v for v in motores if _veh_no_ano(v, ano)]
                if f2: motores = f2

            if len(motores) == 1:
                return _sel_veiculo(motores[0], sess, consulta)
            if motores:
                sess["estado"] = "guiado_motor"
                sess["opcoes"] = motores[:15]
                _hist(sess, consulta)
                return (f"Fabricante: **{mfr_nome}** | Modelo: **{mod_nome}**\n"
                        f"Qual versão/motor?\n\n" +
                        _fmt_lista(motores[:15], "versões")), sess
        else:
            if modelos:
                sess["estado"] = "guiado_modelo"
                sess["opcoes"] = modelos[:15]
                _hist(sess, consulta)
                return (f"Fabricante: **{mfr_nome}**\n"
                        f"Qual modelo?\n\n" +
                        _fmt_lista(modelos[:15], "modelos")), sess

    # 3) Tem veículo na sessão → busca por categoria/termos
    if sess.get("veh_id"):
        return _cats_termos(consulta, sess), sess

    # 4) Tenta produto nos 11k nomes → pede fabricante
    ts = _termos(txt)
    if ts and _PRODS_CACHE:
        prod = _melhor_match(txt, _PRODS_CACHE, "productName")
        if prod:
            sess["_pendente"] = consulta
            _hist(sess, consulta)
            sess["estado"] = "guiado_fabricante"
            sess["opcoes"] = fabs[:20]
            return (f"Entendi: **{prod['productName']}**\n"
                    f"De qual fabricante?\n\n" +
                    _fmt_lista(fabs[:20], "fabricantes")), sess

    # 5) Fluxo guiado completo
    sess["_pendente"] = consulta
    _hist(sess, consulta)
    sess["estado"] = "guiado_fabricante"
    sess["opcoes"] = fabs[:20]
    return ("Qual o **fabricante** (montadora)?\n\n" +
            _fmt_lista(fabs[:20], "fabricantes")), sess

def _busca_numero(nr: str, sess: dict):
    arts = api_busca_auto(nr)
    if not arts: return _fmt_vazio(nr), sess
    if len(arts) == 1:
        sess["peca_atual"] = arts[0]
        sess["estado"]     = "detalhe"
        _hist(sess, nr)
        return _fmt_detalhe(arts[0]), sess
    sess["estado"] = "lista"
    sess["opcoes"] = arts[:10]
    _hist(sess, nr)
    return _fmt_lista(arts[:10], "artigos"), sess

def _cats_termos(consulta: str, sess: dict):
    cats = _CATS_CACHE or api_categorias()
    ts   = _termos(consulta)
    cat  = None
    for t in ts:
        cat = _melhor_match(t, cats, "categoryName","genericArticleDescription","name")
        if cat: break

    if cat:
        cat_id   = _id_cat(cat)
        cat_nome = _nome_cat(cat)
        sess["cat_id"]   = cat_id
        sess["cat_nome"] = cat_nome
        arts = api_artigos(sess["veh_id"], cat_id)
        if arts:
            if len(arts) == 1:
                sess["peca_atual"] = arts[0]
                sess["estado"]     = "detalhe"
                return _fmt_detalhe(arts[0])
            sess["estado"] = "lista"
            sess["opcoes"] = arts[:10]
            return f"Categoria: **{cat_nome}**\n\n" + _fmt_lista(arts[:10], "peças")

    sess["estado"] = "guiado_categoria"
    sess["opcoes"] = cats[:20]
    return "Qual categoria de peça?\n\n" + _fmt_lista(cats[:20], "categorias")

# ── Fluxo guiado ──────────────────────────────────────────────
def _guiado(consulta: str, cmd: str, sess: dict):
    opcoes = sess["opcoes"]
    item   = None

    if cmd.isdigit():
        n = int(cmd)
        if 1 <= n <= len(opcoes):
            item = opcoes[n - 1]
        else:
            return f"❌ Digite de 1 a {len(opcoes)}.", sess
    else:
        # match por nome em todas as opções
        for op in opcoes:
            nome = (_nome_fab(op) or _nome_mod(op) or _nome_veh(op) or
                    _nome_cat(op) or _nome_art(op))
            if _norm_match(consulta, nome):
                item = op; break

    if item is None:
        # não encontrou na lista → trata como nova busca
        return processar(consulta, _novo_estado())

    estado = sess["estado"]

    if estado == "guiado_fabricante":
        mfr_id   = _id_fab(item)
        mfr_nome = _nome_fab(item)
        sess["mfr_id"]   = mfr_id
        sess["mfr_nome"] = mfr_nome
        modelos = api_modelos(mfr_id)
        if not modelos:
            return f"Não encontrei modelos para {mfr_nome}.", _novo_estado()
        sess["estado"] = "guiado_modelo"
        sess["opcoes"] = modelos[:15]
        return (f"Fabricante: **{mfr_nome}**\n"
                f"Qual modelo?\n\n" +
                _fmt_lista(modelos[:15], "modelos")), sess

    if estado == "guiado_modelo":
        mod_id   = _id_mod(item)
        mod_nome = _nome_mod(item)
        sess["mod_id"]   = mod_id
        sess["mod_nome"] = mod_nome
        motores = api_motores(mod_id)
        if not motores:
            return f"Não encontrei versões para {mod_nome}.", _novo_estado()
        if len(motores) == 1:
            return _sel_veiculo(motores[0], sess)
        sess["estado"] = "guiado_motor"
        sess["opcoes"] = motores[:15]
        return (f"Modelo: **{mod_nome}**\n"
                f"Qual versão/motor?\n\n" +
                _fmt_lista(motores[:15], "versões")), sess

    if estado == "guiado_motor":
        return _sel_veiculo(item, sess)

    if estado == "guiado_categoria":
        cat_id   = _id_cat(item)
        cat_nome = _nome_cat(item)
        sess["cat_id"]   = cat_id
        sess["cat_nome"] = cat_nome
        arts = api_artigos(sess["veh_id"], cat_id)
        if not arts:
            return f"Não encontrei peças em '{cat_nome}'.", sess
        if len(arts) == 1:
            sess["peca_atual"] = arts[0]
            sess["estado"]     = "detalhe"
            return _fmt_detalhe(arts[0]), sess
        sess["estado"] = "lista"
        sess["opcoes"] = arts[:10]
        return (f"Categoria: **{cat_nome}**\n\n" +
                _fmt_lista(arts[:10], "peças")), sess

    return "Estado inesperado. Digite /limpar.", _novo_estado()

def _sel_veiculo(veh: dict, sess: dict, cp: str | None = None):
    veh_id   = _id_veh(veh)
    veh_nome = _nome_veh(veh)
    sess["veh_id"]   = veh_id
    sess["veh_nome"] = veh_nome
    cp   = cp or sess.pop("_pendente", None)
    cats = _CATS_CACHE or api_categorias()

    if cp:
        for t in _termos(cp):
            cat = _melhor_match(t, cats, "categoryName",
                                "genericArticleDescription","name")
            if cat:
                cat_id   = _id_cat(cat)
                cat_nome = _nome_cat(cat)
                arts     = api_artigos(veh_id, cat_id)
                if arts:
                    sess["cat_id"]   = cat_id
                    sess["cat_nome"] = cat_nome
                    if len(arts) == 1:
                        sess["peca_atual"] = arts[0]
                        sess["estado"]     = "detalhe"
                        return _fmt_detalhe(arts[0]), sess
                    sess["estado"] = "lista"
                    sess["opcoes"] = arts[:10]
                    return (f"Veículo: **{veh_nome}** | "
                            f"Categoria: **{cat_nome}**\n\n" +
                            _fmt_lista(arts[:10], "peças")), sess

    sess["estado"] = "guiado_categoria"
    sess["opcoes"] = cats[:20]
    return (f"Veículo: **{veh_nome}**\n"
            f"Qual categoria de peça?\n\n" +
            _fmt_lista(cats[:20], "categorias")), sess

# ── Pós-detalhe ───────────────────────────────────────────────
def _compat(sess: dict):
    peca = sess.get("peca_atual")
    if not peca: return "Peça não encontrada.", sess
    ref = _ref_art(peca)
    sid = peca.get("supplierId","")
    if not ref: return "Sem referência para buscar compatibilidade.", sess
    veics = api_compat(ref, sid)
    if not veics: return "Não encontrei veículos compatíveis.", sess
    txt = f"Veículos compatíveis com **{ref}**:\n\n"
    for i, v in enumerate(veics[:15], 1):
        n        = _nome_veh(v)
        ini, fim = _ano_mod(v)
        txt += f"  {i}. {n}"
        if ini: txt += f" ({ini}" + (f"–{fim}" if fim else "") + ")"
        txt += "\n"
    txt += "\n\n1 → compatíveis  2 → similares  3 → nova busca"
    return txt, sess

def _similares(sess: dict):
    peca = sess.get("peca_atual")
    if not peca: return "Peça não encontrada.", sess
    ref = _ref_art(peca)
    if ref:
        arts = [a for a in api_busca_auto(ref) if _id_art(a) != _id_art(peca)]
        if arts:
            sess["estado"] = "lista"; sess["opcoes"] = arts[:10]
            return "Similares:\n\n" + _fmt_lista(arts[:10], "artigos"), sess
    if sess.get("veh_id") and sess.get("cat_id"):
        arts = [a for a in api_artigos(sess["veh_id"], sess["cat_id"])
                if _id_art(a) != _id_art(peca)]
        if arts:
            sess["estado"] = "lista"; sess["opcoes"] = arts[:10]
            return ("Outras peças da categoria:\n\n" +
                    _fmt_lista(arts[:10], "peças")), sess
    return "Não encontrei similares.", sess

def _escolha(cmd: str, sess: dict):
    opcoes = sess["opcoes"]
    if cmd.isdigit():
        n = int(cmd)
        if 1 <= n <= len(opcoes):
            p = opcoes[n - 1]
            sess["peca_atual"] = p; sess["estado"] = "detalhe"
            return _fmt_detalhe(p), sess
        return f"❌ Digite de 1 a {len(opcoes)}.", sess
    # busca por código
    if re.fullmatch(r"[a-zA-Z0-9\-\. ]{4,}", cmd):
        arts = api_busca_auto(cmd)
        if arts:
            if len(arts) == 1:
                sess["peca_atual"] = arts[0]; sess["estado"] = "detalhe"
                return _fmt_detalhe(arts[0]), sess
            sess["estado"] = "lista"; sess["opcoes"] = arts[:10]
            return _fmt_lista(arts[:10], "artigos"), sess
    return None, sess

def _hist(sess, q):
    h = sess.get("historico", [])
    h.append({"hora": datetime.now().strftime("%H:%M"), "q": q})
    sess["historico"] = h[-10:]

def _historico(sess):
    h = sess.get("historico", [])
    if not h: return "Nenhum histórico ainda."
    return "Últimas buscas:\n" + "".join(f"  {i['hora']} {i['q']}\n" for i in h[-5:])

def _ajuda():
    g = f"✅ {MODELO_VISAO}" if MODELO_VISAO else "❌ não configurado"
    return (
        "Como buscar:\n\n"
        "  Texto livre:\n"
        "    fiat palio 2006 embreagem\n"
        "    renault kwid amortecedor\n"
        "    hyundai hb20 filtro óleo\n\n"
        "  Por código/OEM:\n"
        "    7700115294\n"
        "    oem 0242236561\n\n"
        "  Após ver uma peça:\n"
        "    1 → veículos compatíveis\n"
        "    2 → similares\n"
        "    3 → nova busca\n\n"
        "  Comandos: /limpar  /historico  /ajuda\n\n"
        f"  📷 Reconhecimento por imagem: {g}"
    )

# ── Gemini imagem ─────────────────────────────────────────────
def reconhecer_imagem(file_storage):
    if not cliente_genai or not MODELO_VISAO:
        return "", "Reconhecimento por imagem não configurado."
    img_bytes = file_storage.read()
    try:
        img = PILImage.open(BytesIO(img_bytes))
        if img.mode not in ("RGB","L"): img = img.convert("RGB")
    except Exception as e:
        return "", f"Não foi possível abrir a imagem: {e}"
    prompt = (
        "You are an automotive parts expert. Identify the part in the image. "
        "Reply ONLY with the short English technical name. "
        "Examples: 'clutch cable', 'front shock absorber', 'oil filter', "
        "'brake pad', 'timing belt'. If you cannot identify: 'not identified'."
    )
    for t in range(1, 4):
        try:
            resp = cliente_genai.models.generate_content(
                model=MODELO_VISAO, contents=[prompt, img])
            desc = resp.text.strip()
            if not desc or "not identified" in desc.lower():
                return "", "Não consegui identificar. Descreva a peça em texto."
            return desc, ""
        except Exception as e:
            es = str(e)
            if "429" in es or "RESOURCE_EXHAUSTED" in es:
                dm = re.search(r"retryDelay[\W]+(\d+)", es)
                d  = int(dm.group(1)) + 2 if dm else 35
                if t < 3: time.sleep(d); continue
                return "", f"⏳ Limite Gemini. Aguarde ~{d}s."
            return "", f"Erro: {es[:120]}"
    return "", "Falha após múltiplas tentativas."

# ── Rotas ─────────────────────────────────────────────────────
ARQUIVOS_PERMITIDOS = {"index.html", "favicon.ico"}

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

    GENERICAS = {"busca imagem","busca por imagem","identificar peça por imagem"}
    generico  = mensagem.lower().strip() in GENERICAS

    if imagem and imagem.filename:
        desc, erro = reconhecer_imagem(imagem)
        if erro:
            if generico or not mensagem:
                return jsonify({"response": f"📷 {erro}"})
        elif desc:
            mensagem = f"{desc} {mensagem}".strip() if mensagem and not generico else desc

    if generico:
        return jsonify({"response": "📷 Envie uma imagem junto com a mensagem."}), 200
    if not mensagem:
        return jsonify({"error": "Nenhuma mensagem."}), 400

    resp, sess = processar(mensagem, sess)
    save_sess(key, sess)
    print(f"[{key}] {sess['estado']} | {resp[:80]!r}")
    return jsonify({"response": resp})

@app.route("/reset", methods=["POST"])
def reset_route():
    k, _ = get_sess()
    save_sess(k, _novo_estado())
    return jsonify({"ok": True})

@app.route("/ping")
def ping():
    k, sess = get_sess()
    return jsonify({
        "status":  "ok",
        "estado":  sess["estado"],
        "gemini":  MODELO_VISAO or "não configurado",
        "api_key": "✅" if RAPIDAPI_KEY else "❌ faltando",
        "fabs":    len(_FABS_CACHE),
        "cats":    len(_CATS_CACHE),
        "prods":   len(_PRODS_CACHE),
        "client":  k,
    })

# ── Main ──────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("=" * 60)
    print(f"🚀  AutoFlex  →  http://localhost:{port}")
    print(f"🔑  RapidAPI: {'✅' if RAPIDAPI_KEY else '❌ FALTANDO'}")
    print(f"🤖  Gemini:   {MODELO_VISAO or 'não configurado'}")
    print("=" * 60)
    _aquecer()
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)