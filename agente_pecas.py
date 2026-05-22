#!/usr/bin/env python3
"""
Agente CLI – Assistente de Peças OEM
Fonte: Auto Parts Catalog API (RapidAPI)
Estrutura JSON mapeada via inspeção real da API.
"""
import os, re, sys
from pathlib import Path
from datetime import datetime
from collections import deque
import requests
from dotenv import load_dotenv

BASE_DIR      = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

RAPIDAPI_KEY  = os.getenv("RAPIDAPI_KEY","")
RAPIDAPI_HOST = "auto-parts-catalog.p.rapidapi.com"
BASE_URL      = f"https://{RAPIDAPI_HOST}"
LANG_ID       = 4
COUNTRY_ID    = 63
TYPE_ID       = 1

# ── HTTP + cache ──────────────────────────────────────────────
_CACHE: dict = {}

def _get(path, params=None):
    ck = path + str(sorted((params or {}).items()))
    if ck in _CACHE: return _CACHE[ck]
    if not RAPIDAPI_KEY: print("❌ RAPIDAPI_KEY não configurada"); return None
    hdrs = {"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": RAPIDAPI_KEY}
    try:
        r = requests.get(f"{BASE_URL}/{path.lstrip('/')}", headers=hdrs,
                         params=params or {}, timeout=15)
        r.raise_for_status()
        d = r.json(); _CACHE[ck] = d; return d
    except requests.HTTPError as e:
        print(f"❌ {e.response.status_code} /{path}"); return None
    except Exception as e:
        print(f"❌ {e}"); return None

# ── Endpoints ─────────────────────────────────────────────────
def api_fabricantes():
    d = _get(f"manufacturers/list/type-id/{TYPE_ID}")
    return d.get("manufacturers", []) if d else []

def api_modelos(mfr_id):
    d = _get(f"models/list/type-id/{TYPE_ID}/manufacturer-id/{mfr_id}"
             f"/lang-id/{LANG_ID}/country-filter-id/{COUNTRY_ID}")
    if not d: return []
    if isinstance(d, list): return d
    for k in ("models","modelSeries","vehicleModels","data","result","items"):
        if k in d and isinstance(d[k], list): return d[k]
    for v in d.values():
        if isinstance(v, list): return v
    return []

def api_motores(model_id):
    d = _get(f"types/type-id/{TYPE_ID}/list-vehicles-types/{model_id}"
             f"/lang-id/{LANG_ID}/country-filter-id/{COUNTRY_ID}")
    if not d: return []
    if isinstance(d, list): return d
    for k in ("vehicles","types","vehicleTypes","data","result","items"):
        if k in d and isinstance(d[k], list): return d[k]
    for v in d.values():
        if isinstance(v, list): return v
    return []

def api_categorias():
    d = _get(f"category/type-id/{TYPE_ID}/list-category-tree-structure/lang-id/{LANG_ID}")
    if not d: return []
    if isinstance(d, list): return d
    result = []
    def _flat(obj):
        if not isinstance(obj, dict): return
        cid = obj.get("categoryId"); nome = obj.get("categoryName","")
        if cid: result.append({"categoryId": cid, "categoryName": nome})
        ch = obj.get("children",{})
        for c in (ch.values() if isinstance(ch, dict) else ch): _flat(c)
    for top in d.values(): _flat(top)
    return result

def api_artigos(veh_id, cat_id):
    d = _get(f"articles/list/type-id/{TYPE_ID}/vehicle-id/{veh_id}"
             f"/category-id/{cat_id}/lang-id/{LANG_ID}")
    if not d: return []
    if isinstance(d, list): return d
    return d.get("articles", d.get("data", d.get("result", [])))

def api_busca_nr(nr):
    d = _get("artlookup/search-articles-by-article-no",
             {"langId": LANG_ID, "articleNo": nr, "articleType": "ArticleNumber"})
    if not d: return []
    return d if isinstance(d, list) else d.get("articles", [])

def api_busca_oem(nr):
    d = _get("artlookup/search-articles-by-article-no",
             {"langId": LANG_ID, "articleNo": nr, "articleType": "OENumber"})
    if not d: return []
    return d if isinstance(d, list) else d.get("articles", [])

def api_busca_auto(nr):
    r = api_busca_nr(nr); return r if r else api_busca_oem(nr)

def api_compat(article_no, supplier_id):
    d = _get(f"articles/get-compatible-cars-by-article-number/type-id/{TYPE_ID}",
             {"articleNo": article_no, "supplierId": supplier_id,
              "langId": LANG_ID, "countryFilterId": COUNTRY_ID})
    if not d: return []
    if isinstance(d, list): return d
    for k in ("vehicles","cars","data","result","items"):
        if k in d and isinstance(d[k], list): return d[k]
    return []

# ── Cache global ──────────────────────────────────────────────
_FABS: list = []
_CATS: list = []
_PRODS: list = []

def _carregar():
    global _FABS, _CATS, _PRODS
    print("  Carregando fabricantes...", end=" ", flush=True)
    _FABS = api_fabricantes(); print(f"{len(_FABS)}")
    print("  Carregando categorias...",  end=" ", flush=True)
    _CATS = api_categorias();  print(f"{len(_CATS)}")
    print("  Carregando produtos...",    end=" ", flush=True)
    d = _get(f"category/list-products-names/lang-id/{LANG_ID}")
    _PRODS = d if isinstance(d, list) else []; print(f"{len(_PRODS)}")

# ── Accessors ─────────────────────────────────────────────────
def _idf(it): return it.get("manufacturerId") or it.get("mfrId") or it.get("id")
def _nf(it):  return _v(it.get("manufacturerName") or it.get("mfrName") or it.get("name",""))
def _idm(it): return it.get("modelId") or it.get("vehicleModelSeriesId") or it.get("id")
def _nm(it):  return _v(it.get("modelName") or it.get("vehicleModelSeriesName") or it.get("name") or it.get("description",""))
def _idv(it): return it.get("vehicleId") or it.get("carId") or it.get("id")
def _nv(it):  return _v(it.get("fulldescription") or it.get("description") or it.get("name") or it.get("vehicleName",""))
def _idc(it): return it.get("categoryId") or it.get("genericArticleId") or it.get("id")
def _nc(it):  return _v(it.get("categoryName") or it.get("genericArticleDescription") or it.get("name",""))
def _ida(it): return it.get("articleId") or it.get("id")
def _na(it):  return _v(it.get("articleProductName") or it.get("articleName") or it.get("description") or it.get("name",""))
def _ra(it):  return _v(it.get("articleNo") or it.get("articleNumber") or it.get("articleSearchNo",""))
def _ma(it):  return _v(it.get("supplierName") or it.get("brandName",""))

def _anos(it):
    ini = str(it.get("modelYearFrom") or it.get("yearOfConstrFrom") or it.get("constructionYearFrom") or "")[:4]
    fim = str(it.get("modelYearTo")   or it.get("yearOfConstrTo")   or it.get("constructionYearTo")   or "")[:4]
    return ini, fim

def _nome_generico(it):
    return _nf(it) or _nm(it) or _nv(it) or _nc(it) or _na(it) or "?"

# ── Utils ─────────────────────────────────────────────────────
def _norm(s): return re.sub(r"[^a-z0-9]","",str(s).lower())
def _v(val):
    s = str(val).strip() if val is not None else ""
    return "" if s.lower() in ("nan","none","null","") else s

IGNORAR = {
    "quero","preciso","procuro","tem","voce","você","me","manda","ver",
    "uma","um","o","a","os","as","de","do","da","dos","das","para","pra",
    "com","peca","peça","produto","carro","veiculo","veículo","qual",
    "tenho","meu","minha","pelo","pela","buscar","busco",
}

def _termos(txt, ex=None):
    return [p for p in re.findall(r"[a-zA-ZÀ-ÿ0-9]+", txt.lower())
            if p not in IGNORAR and p not in (ex or []) and len(p) > 2]

def _match(texto, nome):
    tn, nn = _norm(texto), _norm(nome)
    return tn in nn or any(_norm(p) in nn for p in texto.split() if len(p) > 2)

def _melhor(texto, lista, *campos):
    tn = _norm(texto); hits = []
    for it in lista:
        for c in campos:
            nn = _norm(str(it.get(c,"")))
            if not nn: continue
            if tn in nn: hits.append((len(tn)/max(len(nn),1), it)); break
            elif any(_norm(p) in nn for p in texto.split() if len(p)>2): hits.append((0.3,it)); break
    hits.sort(key=lambda x:x[0], reverse=True)
    return hits[0][1] if hits else None

def _ano_txt(txt):
    m = re.search(r"\b(19|20)\d{2}\b", txt); return m.group() if m else None

def _veh_ano(veh, ano):
    try:
        a = int(ano); ini, fim = _anos(veh)
        if ini and int(ini) > a: return False
        if fim and int(fim) < a: return False
    except: pass
    return True

NOMES_CARROS = {
    "palio","gol","uno","corsa","civic","hilux","corolla","onix","hb20",
    "fiat","volkswagen","vw","toyota","honda","chevrolet","ford","renault",
    "hyundai","nissan","peugeot","mitsubishi","kia","jeep","strada","creta",
    "kwid","sandero","logan","ka","fiesta","ecosport","ranger","duster",
    "captur","stepway","oroch","tucson","yaris","etios","hrv","wrv","brv",
    "tracker","spin","montana","agile","cobalt","celta","siena","argo",
    "cronos","mobi","fiorino","doblo","toro","pulse","polo","virtus",
    "voyage","saveiro","fox","taos","nivus",
}

# ── Formatação ────────────────────────────────────────────────
def fmt_lista(lista, tipo="itens"):
    if not lista: return f"Nenhum(a) {tipo} encontrado(a)."
    txt = f"Encontrei {len(lista)} {tipo}:\n\n"
    for i, it in enumerate(lista[:12], 1):
        n = _nome_generico(it)
        ini, fim = _anos(it)
        txt += f"  {i}. {n}"
        if ini: txt += f" ({ini}" + (f"–{fim}" if fim else "") + ")"
        txt += "\n"
    txt += "\nDigite o número ou escreva o nome:"
    return txt

def fmt_detalhe(a):
    nome  = _na(a); ref = _ra(a); marca = _ma(a)
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

def fmt_vazio(q):
    return (f"Não encontrei: {q}\n\nExemplos:\n"
            "  fiat palio 2006 embreagem\n"
            "  renault kwid amortecedor\n"
            "  hyundai hb20 filtro oleo\n"
            "  oem 7700115294\n  /ajuda")

# ── Estado ────────────────────────────────────────────────────
def _novo():
    return dict(estado="livre", opcoes=[], peca=None,
                mfr_id=None, mfr_nome=None, mod_id=None, mod_nome=None,
                veh_id=None, veh_nome=None, cat_id=None, cat_nome=None,
                hist=deque(maxlen=10), pendente=None)

# ── Agente ────────────────────────────────────────────────────
class Agente:
    def __init__(self): self.s = _novo()

    def msg(self, consulta):
        consulta = consulta.strip()
        if not consulta: return "Digite uma peça, código ou veículo."
        cmd    = consulta.lower().strip()
        estado = self.s["estado"]

        if cmd in ("/ajuda","ajuda","help"):   return self._ajuda()
        if cmd in ("/historico","historico"):  return self._hist_txt()
        if cmd in ("/limpar","limpar","sair","reset","novo"):
            self.s = _novo(); return "🔍 Contexto limpo."

        if estado == "detalhe":
            if cmd == "1": return self._compat()
            if cmd == "2": return self._similares()
            if cmd == "3": self.s = _novo(); return "🔍 Nova busca."
            self.s = _novo(); return self.msg(consulta)

        if estado == "lista":
            r = self._escolha(cmd)
            if r is not None: return r
            self.s = _novo(); return self.msg(consulta)

        if estado.startswith("guiado_"):
            return self._guiado(consulta, cmd)

        return self._livre(consulta)

    # ── Busca livre ───────────────────────────────────────────
    def _livre(self, consulta):
        txt = consulta.lower(); ano = _ano_txt(txt)

        # código?
        tok      = consulta.strip()
        palavras = set(re.findall(r"[a-zA-Z]+", txt))
        tem_num  = bool(re.search(r"\d{4,}", tok))
        if (tem_num and not (palavras & NOMES_CARROS)) or "oem" in txt or "ref" in txt:
            nr = re.sub(r"(?i)(oem|ref)\s*:?\s*","",tok).strip()
            return self._busca_nr(nr)

        fabs = _FABS or api_fabricantes()
        fab  = _melhor(txt, fabs, "manufacturerName","mfrName")

        if fab:
            mid = _idf(fab); mn = _nf(fab)
            self.s["mfr_id"] = mid; self.s["mfr_nome"] = mn
            mods = api_modelos(mid)
            mm   = _melhor(txt, mods, "modelName","vehicleModelSeriesName","name","description")
            if mm:
                modid = _idm(mm); modn = _nm(mm)
                self.s["mod_id"] = modid; self.s["mod_nome"] = modn
                motores = api_motores(modid)
                if ano:
                    f2 = [v for v in motores if _veh_ano(v, ano)]
                    if f2: motores = f2
                if len(motores) == 1: return self._sel(motores[0], consulta)
                if motores:
                    self.s["estado"] = "guiado_motor"; self.s["opcoes"] = motores[:15]
                    self._add_hist(consulta)
                    return f"Fabricante: {mn} | Modelo: {modn}\nQual versão?\n\n" + fmt_lista(motores[:15],"versões")
            else:
                if mods:
                    self.s["estado"] = "guiado_modelo"; self.s["opcoes"] = mods[:15]
                    self._add_hist(consulta)
                    return f"Fabricante: {mn}\nQual modelo?\n\n" + fmt_lista(mods[:15],"modelos")

        if self.s.get("veh_id"): return self._cats_termos(consulta)

        # tenta produto
        ts = _termos(txt)
        if ts and _PRODS:
            prod = _melhor(txt, _PRODS, "productName")
            if prod:
                self.s["pendente"] = consulta; self._add_hist(consulta)
                self.s["estado"] = "guiado_fabricante"; self.s["opcoes"] = fabs[:20]
                return f"Entendi: {prod['productName']}\nQual fabricante?\n\n" + fmt_lista(fabs[:20],"fabricantes")

        self.s["pendente"] = consulta; self._add_hist(consulta)
        self.s["estado"] = "guiado_fabricante"; self.s["opcoes"] = fabs[:20]
        return "Qual o fabricante?\n\n" + fmt_lista(fabs[:20],"fabricantes")

    def _busca_nr(self, nr):
        arts = api_busca_auto(nr)
        if not arts: return fmt_vazio(nr)
        if len(arts) == 1:
            self.s["peca"] = arts[0]; self.s["estado"] = "detalhe"
            return fmt_detalhe(arts[0])
        self.s["estado"] = "lista"; self.s["opcoes"] = arts[:10]
        return fmt_lista(arts[:10],"artigos")

    def _cats_termos(self, consulta):
        cats = _CATS or api_categorias(); ts = _termos(consulta); cat = None
        for t in ts:
            cat = _melhor(t, cats, "categoryName","genericArticleDescription","name")
            if cat: break
        if cat:
            cid = _idc(cat); cn = _nc(cat)
            arts = api_artigos(self.s["veh_id"], cid)
            if arts:
                self.s["cat_id"] = cid; self.s["cat_nome"] = cn
                if len(arts) == 1:
                    self.s["peca"] = arts[0]; self.s["estado"] = "detalhe"
                    return fmt_detalhe(arts[0])
                self.s["estado"] = "lista"; self.s["opcoes"] = arts[:10]
                return f"Categoria: {cn}\n\n" + fmt_lista(arts[:10],"peças")
        self.s["estado"] = "guiado_categoria"; self.s["opcoes"] = cats[:20]
        return "Qual categoria de peça?\n\n" + fmt_lista(cats[:20],"categorias")

    # ── Guiado ────────────────────────────────────────────────
    def _guiado(self, consulta, cmd):
        opcoes = self.s["opcoes"]; item = None
        if cmd.isdigit():
            n = int(cmd)
            if 1 <= n <= len(opcoes): item = opcoes[n-1]
            else: return f"❌ Digite de 1 a {len(opcoes)}."
        else:
            for op in opcoes:
                if _match(consulta, _nome_generico(op)): item = op; break
        if item is None: self.s = _novo(); return self.msg(consulta)

        estado = self.s["estado"]

        if estado == "guiado_fabricante":
            mid = _idf(item); mn = _nf(item)
            self.s["mfr_id"] = mid; self.s["mfr_nome"] = mn
            mods = api_modelos(mid)
            if not mods: return f"Não encontrei modelos para {mn}."
            self.s["estado"] = "guiado_modelo"; self.s["opcoes"] = mods[:15]
            return f"Fabricante: {mn}\nQual modelo?\n\n" + fmt_lista(mods[:15],"modelos")

        if estado == "guiado_modelo":
            modid = _idm(item); modn = _nm(item)
            self.s["mod_id"] = modid; self.s["mod_nome"] = modn
            motores = api_motores(modid)
            if not motores: return f"Não encontrei versões para {modn}."
            if len(motores) == 1: return self._sel(motores[0])
            self.s["estado"] = "guiado_motor"; self.s["opcoes"] = motores[:15]
            return f"Modelo: {modn}\nQual versão?\n\n" + fmt_lista(motores[:15],"versões")

        if estado == "guiado_motor":
            return self._sel(item)

        if estado == "guiado_categoria":
            cid = _idc(item); cn = _nc(item)
            self.s["cat_id"] = cid; self.s["cat_nome"] = cn
            arts = api_artigos(self.s["veh_id"], cid)
            if not arts: return f"Não encontrei peças em '{cn}'."
            if len(arts) == 1:
                self.s["peca"] = arts[0]; self.s["estado"] = "detalhe"
                return fmt_detalhe(arts[0])
            self.s["estado"] = "lista"; self.s["opcoes"] = arts[:10]
            return f"Categoria: {cn}\n\n" + fmt_lista(arts[:10],"peças")

        return "Estado desconhecido. Digite /limpar."

    def _sel(self, veh, cp=None):
        vid = _idv(veh); vn = _nv(veh)
        self.s["veh_id"] = vid; self.s["veh_nome"] = vn
        cp  = cp or self.s.pop("pendente", None)
        cats = _CATS or api_categorias()
        if cp:
            for t in _termos(cp):
                cat = _melhor(t, cats, "categoryName","genericArticleDescription","name")
                if cat:
                    cid = _idc(cat); cn = _nc(cat)
                    arts = api_artigos(vid, cid)
                    if arts:
                        self.s["cat_id"] = cid; self.s["cat_nome"] = cn
                        if len(arts) == 1:
                            self.s["peca"] = arts[0]; self.s["estado"] = "detalhe"
                            return fmt_detalhe(arts[0])
                        self.s["estado"] = "lista"; self.s["opcoes"] = arts[:10]
                        return f"Veículo: {vn} | {cn}\n\n" + fmt_lista(arts[:10],"peças")
        self.s["estado"] = "guiado_categoria"; self.s["opcoes"] = cats[:20]
        return f"Veículo: {vn}\n\n" + fmt_lista(cats[:20],"categorias")

    # ── Pós-detalhe ───────────────────────────────────────────
    def _compat(self):
        p = self.s.get("peca")
        if not p: return "Peça não encontrada."
        ref = _ra(p); sid = p.get("supplierId","")
        if not ref: return "Sem referência para buscar compatibilidade."
        veics = api_compat(ref, sid)
        if not veics: return "Não encontrei veículos compatíveis."
        txt = f"Veículos compatíveis com {ref}:\n\n"
        for i, v in enumerate(veics[:15], 1):
            n = _nv(v); ini, fim = _anos(v)
            txt += f"  {i}. {n}"
            if ini: txt += f" ({ini}" + (f"–{fim}" if fim else "") + ")"
            txt += "\n"
        txt += "\n\n1 → compatíveis  2 → similares  3 → nova busca"
        return txt

    def _similares(self):
        p = self.s.get("peca")
        if not p: return "Peça não encontrada."
        ref = _ra(p)
        if ref:
            arts = [a for a in api_busca_auto(ref) if _ida(a) != _ida(p)]
            if arts:
                self.s["estado"] = "lista"; self.s["opcoes"] = arts[:10]
                return "Similares:\n\n" + fmt_lista(arts[:10],"artigos")
        if self.s.get("veh_id") and self.s.get("cat_id"):
            arts = [a for a in api_artigos(self.s["veh_id"], self.s["cat_id"])
                    if _ida(a) != _ida(p)]
            if arts:
                self.s["estado"] = "lista"; self.s["opcoes"] = arts[:10]
                return "Outras peças da categoria:\n\n" + fmt_lista(arts[:10],"peças")
        return "Não encontrei similares."

    def _escolha(self, cmd):
        opcoes = self.s["opcoes"]
        if cmd.isdigit():
            n = int(cmd)
            if 1 <= n <= len(opcoes):
                p = opcoes[n-1]; self.s["peca"] = p; self.s["estado"] = "detalhe"
                return fmt_detalhe(p)
            return f"❌ Digite de 1 a {len(opcoes)}."
        if re.fullmatch(r"[a-zA-Z0-9\-\. ]{4,}", cmd):
            arts = api_busca_auto(cmd)
            if arts:
                if len(arts) == 1:
                    self.s["peca"] = arts[0]; self.s["estado"] = "detalhe"
                    return fmt_detalhe(arts[0])
                self.s["estado"] = "lista"; self.s["opcoes"] = arts[:10]
                return fmt_lista(arts[:10],"artigos")
        return None

    def _add_hist(self, q):
        self.s["hist"].append({"h": datetime.now().strftime("%H:%M"), "q": q})

    def _hist_txt(self):
        h = list(self.s["hist"])
        if not h: return "Nenhum histórico."
        return "Últimas buscas:\n" + "".join(f"  {i['h']} {i['q']}\n" for i in h[-5:])

    def _ajuda(self):
        return (
            "Como usar:\n\n"
            "  Texto livre:\n"
            "    fiat palio 2006 embreagem\n"
            "    renault kwid amortecedor\n"
            "    hyundai hb20 filtro oleo\n\n"
            "  Por código/OEM:\n"
            "    7700115294\n"
            "    oem 0242236561\n\n"
            "  Navegação: responda com número\n\n"
            "  Após ver peça:\n"
            "    1 → veículos compatíveis\n"
            "    2 → similares\n"
            "    3 → nova busca\n\n"
            "  /limpar  /historico  /ajuda"
        )

# ── Main ─────────────────────────────────────────────────────
def main():
    if not RAPIDAPI_KEY:
        print("❌ RAPIDAPI_KEY não encontrada no .env"); sys.exit(1)
    print("=" * 60)
    print("🧠 AGENTE DE PEÇAS OEM  –  Auto Parts Catalog API")
    print("=" * 60)
    _carregar()
    print("=" * 60)
    ag = Agente()
    while True:
        try: q = input("\nVocê: ").strip()
        except (EOFError, KeyboardInterrupt): print("\nAté logo!"); break
        if not q: continue
        if q.lower() in ("fechar","exit","quit"): print("Até logo!"); break
        print(f"\n{ag.msg(q)}")

if __name__ == "__main__": main()