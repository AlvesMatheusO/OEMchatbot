#!/usr/bin/env python3
"""
app_db.py – AutoFlex OEM Conversacional
========================================
Arquitetura de custo mínimo:
  • Veículos  → API FIPE pública (parallelum.com.br) — GRATUITA, sem banco
  • Peças     → Apify Part Number Cross Reference ($19/mês) — só após veículo definido

Fluxo conversacional:
  "palio 2006 embreagem"
    → detecta FIAT + palio + 2006
    → FIPE API: marcas → modelos FIAT → filtra Palio → filtra versão 2006
    → pergunta: "Qual versão?" se houver mais de uma
    → pergunta: "Qual peça?" se ainda não souber
    → Apify: busca cross-reference da peça no veículo
    → mostra resultado com OEM, marca, referência

  "oem 7700115294"
    → Apify direto, sem FIPE

.env:
  APIFY_TOKEN=apify_api_...
"""

import os, re, json
import requests
from pathlib import Path
from datetime import datetime
from functools import lru_cache

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__, template_folder=str(BASE_DIR), static_folder=str(BASE_DIR))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
CORS(app, supports_credentials=True)

# ── Config ─────────────────────────────────────────────────────
APIFY_TOKEN   = os.getenv("APIFY_TOKEN", "")
APIFY_ACTOR   = "making-data-meaningful~part-number-cross-reference"
APIFY_URL     = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
APIFY_TIMEOUT = 90

FIPE_BASE     = "https://parallelum.com.br/fipe/api/v1/carros"
FIPE_TIMEOUT  = 10

# ══════════════════════════════════════════════════════════════
# API FIPE — gratuita, sem autenticação
# ══════════════════════════════════════════════════════════════

_fipe_cache: dict = {}

def _fipe_get(path: str) -> list | dict:
    if path in _fipe_cache:
        return _fipe_cache[path]
    try:
        r = requests.get(f"{FIPE_BASE}/{path}", timeout=FIPE_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        _fipe_cache[path] = data
        return data
    except Exception as e:
        raise RuntimeError(f"FIPE API: {e}")

def fipe_marcas() -> list:
    """Retorna lista de marcas: [{codigo, nome}, ...]"""
    return _fipe_get("marcas")

def fipe_modelos(marca_cod: str) -> list:
    """Retorna modelos de uma marca."""
    data = _fipe_get(f"marcas/{marca_cod}/modelos")
    return data.get("modelos", data) if isinstance(data, dict) else data

def fipe_anos(marca_cod: str, modelo_cod: str) -> list:
    """Retorna anos/versões de um modelo: [{codigo, nome}, ...]"""
    return _fipe_get(f"marcas/{marca_cod}/modelos/{modelo_cod}/anos")

# ══════════════════════════════════════════════════════════════
# Apify — Part Number Cross Reference
# ══════════════════════════════════════════════════════════════

def _apify(payload: dict) -> list:
    if not APIFY_TOKEN:
        raise RuntimeError("APIFY_TOKEN não configurado no .env")
    try:
        r = requests.post(APIFY_URL,
                          params={"token": APIFY_TOKEN},
                          json=payload, timeout=APIFY_TIMEOUT)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        # Desempacota wrapper: [{"countArticles": N, "articles": [...]}]
        artigos = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    if isinstance(item.get("articles"), list):
                        artigos.extend(item["articles"])
                    elif "articleId" in item or "articleNo" in item:
                        artigos.append(item)
        return artigos
    except requests.Timeout:
        raise RuntimeError("Apify: timeout após 90s")

def apify_cross(numero: str, tipo: str = "OENumber") -> list:
    return _apify({"selectPageType": "search-for-cross-number-by-article-no",
                   "articleNo": numero, "articleType": tipo})

def apify_veiculos(oem: str) -> list:
    return _apify({"selectPageType": "get-list-of-vehicles-by-oem-part",
                   "articleOemNo": oem})

def apify_analogos(oem: str) -> list:
    return _apify({"selectPageType": "search-for-analogue-of-spare-parts-by-oem-number",
                   "articleOemNo": oem})

# ══════════════════════════════════════════════════════════════
# Mapeamentos de texto → código
# ══════════════════════════════════════════════════════════════

# Mapa modelo → nome de marca para lookup rápido
MODELO_FAB = {
    "palio":"FIAT","siena":"FIAT","uno":"FIAT","argo":"FIAT","cronos":"FIAT",
    "mobi":"FIAT","fiorino":"FIAT","doblo":"FIAT","toro":"FIAT","strada":"FIAT",
    "pulse":"FIAT","fastback":"FIAT","bravo":"FIAT","linea":"FIAT","idea":"FIAT",
    "gol":"VOLKSWAGEN","polo":"VOLKSWAGEN","virtus":"VOLKSWAGEN","voyage":"VOLKSWAGEN",
    "saveiro":"VOLKSWAGEN","fox":"VOLKSWAGEN","taos":"VOLKSWAGEN","nivus":"VOLKSWAGEN",
    "tcross":"VOLKSWAGEN","amarok":"VOLKSWAGEN","tiguan":"VOLKSWAGEN","jetta":"VOLKSWAGEN",
    "crossfox":"VOLKSWAGEN","up":"VOLKSWAGEN","spacefox":"VOLKSWAGEN",
    "onix":"CHEVROLET","tracker":"CHEVROLET","spin":"CHEVROLET","montana":"CHEVROLET",
    "cobalt":"CHEVROLET","celta":"CHEVROLET","corsa":"CHEVROLET","vectra":"CHEVROLET",
    "astra":"CHEVROLET","s10":"CHEVROLET","cruze":"CHEVROLET","prisma":"CHEVROLET",
    "agile":"CHEVROLET","zafira":"CHEVROLET","blazer":"CHEVROLET","captiva":"CHEVROLET",
    "civic":"HONDA","hrv":"HONDA","wrv":"HONDA","crv":"HONDA","city":"HONDA","fit":"HONDA",
    "corolla":"TOYOTA","hilux":"TOYOTA","yaris":"TOYOTA","etios":"TOYOTA",
    "sw4":"TOYOTA","rav4":"TOYOTA","fortuner":"TOYOTA","camry":"TOYOTA",
    "hb20":"HYUNDAI","creta":"HYUNDAI","tucson":"HYUNDAI","ix35":"HYUNDAI","i30":"HYUNDAI",
    "kwid":"RENAULT","sandero":"RENAULT","logan":"RENAULT","duster":"RENAULT",
    "captur":"RENAULT","stepway":"RENAULT","oroch":"RENAULT","master":"RENAULT",
    "megane":"RENAULT","kangoo":"RENAULT","fluence":"RENAULT","symbol":"RENAULT",
    "ka":"FORD","fiesta":"FORD","ecosport":"FORD","ranger":"FORD",
    "fusion":"FORD","maverick":"FORD","transit":"FORD","focus":"FORD","edge":"FORD",
    "kicks":"NISSAN","versa":"NISSAN","march":"NISSAN","frontier":"NISSAN","tiida":"NISSAN",
    "sportage":"KIA","sorento":"KIA","cerato":"KIA","soul":"KIA","carnival":"KIA",
    "compass":"JEEP","renegade":"JEEP","wrangler":"JEEP","commander":"JEEP",
    "outlander":"MITSUBISHI","asx":"MITSUBISHI","pajero":"MITSUBISHI","l200":"MITSUBISHI",
    "208":"PEUGEOT","2008":"PEUGEOT","308":"PEUGEOT","3008":"PEUGEOT",
    "c3":"CITROEN","c4":"CITROEN",
}

FAB_ALIASES = {
    "vw":"VOLKSWAGEN","gm":"CHEVROLET","chevy":"CHEVROLET",
    "fiat":"FIAT","volkswagen":"VOLKSWAGEN","toyota":"TOYOTA","honda":"HONDA",
    "chevrolet":"CHEVROLET","ford":"FORD","renault":"RENAULT","hyundai":"HYUNDAI",
    "nissan":"NISSAN","kia":"KIA","jeep":"JEEP","mitsubishi":"MITSUBISHI",
    "peugeot":"PEUGEOT","citroen":"CITROEN",
}

_STOP = {
    "o","a","os","as","de","do","da","para","com","em","um","uma",
    "meu","minha","preciso","quero","procuro","tem","voce","você",
    "carro","veiculo","veículo","auto","peca","peça","produto",
    "novo","nova","original","oem","ref","sku","qual","me","por",
}

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())

def _extrair_ano(txt: str) -> str | None:
    m = re.search(r"\b(19[5-9]\d|20[0-3]\d)\b", txt)
    return m.group() if m else None

def _extrair_fab_modelo(txt: str):
    t = txt.lower()
    for token in re.findall(r"[a-zA-Z0-9]+", t):
        if token in MODELO_FAB:
            return MODELO_FAB[token], token
    for token in re.findall(r"[a-zA-Z]+", t):
        if token in FAB_ALIASES:
            return FAB_ALIASES[token], None
    return None, None

def _extrair_oem(txt: str):
    m = re.search(r'\boem\s*:?\s*([A-Za-z0-9][\-\. ]?[A-Za-z0-9]{3,}(?:[\-\. ][A-Za-z0-9]+)*)', txt, re.I)
    if m:
        return re.sub(r'\s+', '', m.group(1)), "OENumber"
    m = re.search(r'\bref\s*:?\s*([A-Za-z0-9]{5,}(?:[\-\.][A-Za-z0-9]+)*)', txt, re.I)
    if m:
        return m.group(1), "ArticleNumber"
    return None, None

def _termos_peca(txt: str, fab: str = "", modelo: str = "") -> list:
    excluir = _STOP | {fab.lower()} | {modelo.lower()} | set(MODELO_FAB) | set(FAB_ALIASES)
    ano = _extrair_ano(txt)
    return [t for t in re.findall(r"[a-zA-ZÀ-ÿ]+", txt.lower())
            if t not in excluir and len(t) > 2 and t != (ano or "")]

def _melhor(texto: str, lista: list, *campos) -> dict | None:
    tn = _norm(texto)
    hits = []
    for it in lista:
        for c in campos:
            nn = _norm(str(it.get(c, "")))
            if not nn: continue
            if tn == nn:                                         hits.append((1.0, it)); break
            if tn in nn and len(tn) >= len(nn)*0.4:             hits.append((0.8, it)); break
            if re.search(r'(?<![a-z0-9])'+re.escape(tn)+r'(?![a-z0-9])', nn):
                                                                 hits.append((0.5, it)); break
    hits.sort(key=lambda x: x[0], reverse=True)
    return hits[0][1] if hits else None

# ══════════════════════════════════════════════════════════════
# Sessões
# ══════════════════════════════════════════════════════════════

_SESSIONS: dict = {}

def _client_key():
    return request.headers.get("X-Forwarded-For", request.remote_addr) or "local"

def get_sess():
    k = _client_key()
    if k not in _SESSIONS:
        _SESSIONS[k] = _novo()
    return k, _SESSIONS[k]

def save_sess(k, s): _SESSIONS[k] = s

def _novo():
    return {
        "estado":    "livre",
        # contexto do veículo
        "fab_cod":   None, "fab_nome":   None,
        "mod_cod":   None, "mod_nome":   None,
        "ano_cod":   None, "ano_nome":   None,
        # resultado
        "opcoes":    [],
        "peca":      None,
        "pendente":  "",
        "historico": [],
    }

# ══════════════════════════════════════════════════════════════
# Formatação
# ══════════════════════════════════════════════════════════════

def _fmt_marcas(lista: list) -> str:
    txt = f"Encontrei {len(lista)} marcas. Qual é a sua?\n\n"
    for i, m in enumerate(lista[:20], 1):
        txt += f"  {i}. {m['nome']}\n"
    txt += "\nDigite o número ou o nome:"
    return txt

def _fmt_modelos(lista: list, fab: str) -> str:
    txt = f"Fabricante: **{fab}**\nQual modelo?\n\n"
    for i, m in enumerate(lista[:20], 1):
        txt += f"  {i}. {m['nome']}\n"
    txt += "\nDigite o número ou o nome:"
    return txt

def _fmt_versoes(lista: list, mod: str, ano: str = "") -> str:
    titulo = f"Modelo: **{mod}**"
    if ano: titulo += f" | Ano buscado: {ano}"
    txt = titulo + "\nQual versão/ano?\n\n"
    for i, v in enumerate(lista[:20], 1):
        txt += f"  {i}. {v['nome']}\n"
    txt += "\nDigite o número:"
    return txt

def _fmt_artigos(lista: list) -> str:
    if not lista:
        return "Nenhuma peça encontrada."
    txt = f"Encontrei {len(lista)} peça(s):\n\n"
    for i, a in enumerate(lista[:12], 1):
        nome  = (a.get("articleProductName") or a.get("name") or "").strip()
        ref   = (a.get("articleNo") or "").strip()
        marca = (a.get("supplierName") or "").strip()
        txt += f"  {i}. {nome}"
        if marca: txt += f"  ({marca})"
        if ref:   txt += f"  — ref: {ref}"
        txt += "\n"
    txt += "\nDigite o número para ver detalhes:"
    return txt

def _fmt_detalhe(a: dict) -> str:
    nome  = (a.get("articleProductName") or a.get("name") or "").strip()
    ref   = (a.get("articleNo") or "").strip()
    marca = (a.get("supplierName") or "").strip()
    oem   = (a.get("articleSearchNo") or "").strip()
    txt   = "✅ Peça encontrada\n\n"
    txt  += f"  🔧 {nome}\n\n"
    if ref:   txt += f"  Referência : {ref}\n"
    if marca: txt += f"  Marca      : {marca}\n"
    if oem:   txt += f"  OEM        : {oem}\n"
    crit = a.get("criteria") or []
    for c in (crit[:5] if isinstance(crit, list) else []):
        n_ = c.get("criteriaDescription") or c.get("name","")
        v_ = c.get("rawValue") or c.get("value","")
        u_ = c.get("criteriaUnitDescription") or c.get("unit","")
        if n_ and v_: txt += f"  {n_}: {v_}{' '+u_ if u_ else ''}\n"
    img = a.get("s3image","")
    if img: txt += f"\n  🖼️  {img}\n"
    txt += "\n  1 → veículos compatíveis  2 → análogos  3 → nova busca"
    return txt

def _fmt_vazio(q: str) -> str:
    return (
        f"Não encontrei resultados para: *{q}*\n\n"
        "Exemplos que funcionam:\n"
        "  palio 2006 embreagem\n"
        "  kwid filtro oleo\n"
        "  oem 7700115294\n"
        "  /ajuda"
    )

# ══════════════════════════════════════════════════════════════
# Processador principal
# ══════════════════════════════════════════════════════════════

def processar(consulta: str, sess: dict):
    consulta = consulta.strip()
    if not consulta:
        return "O que você precisa? Ex: palio 2006 embreagem", sess

    cmd    = consulta.lower().strip()
    estado = sess["estado"]

    if cmd in ("/ajuda","ajuda","help"):    return _ajuda(), sess
    if cmd in ("/limpar","limpar","reset"): return "🔍 Contexto limpo.", _novo()

    # Estados de escolha guiada
    if estado == "escolha_marca":   return _escolher_marca(cmd, sess)
    if estado == "escolha_modelo":  return _escolher_modelo(cmd, sess)
    if estado == "escolha_versao":  return _escolher_versao(cmd, sess)
    if estado == "pedir_peca":      return _receber_peca(consulta, sess)

    # Lista de artigos
    if estado == "lista_artigos":
        if cmd.isdigit():
            n = int(cmd)
            if 1 <= n <= len(sess["opcoes"]):
                a = sess["opcoes"][n-1]
                sess["peca"]   = a
                sess["estado"] = "detalhe"
                return _fmt_detalhe(a), sess
            return f"❌ Digite de 1 a {len(sess['opcoes'])}.", sess
        return processar(consulta, _novo())

    # Detalhe da peça
    if estado == "detalhe":
        if cmd == "1": return _compat(sess)
        if cmd == "2": return _analogos(sess)
        if cmd == "3": return "🔍 Nova busca.", _novo()
        return processar(consulta, _novo())

    # Livre: tenta interpretar
    oem, tipo = _extrair_oem(consulta)
    if oem:
        return _busca_oem(oem, tipo, sess)

    return _busca_catalogo(consulta, sess)


# ── Busca OEM direto ───────────────────────────────────────────
def _busca_oem(numero: str, tipo: str, sess: dict):
    _add_hist(sess, numero)
    try:
        itens = apify_cross(numero, tipo)
        if not itens and tipo == "ArticleNumber":
            itens = apify_cross(numero, "OENumber")
    except RuntimeError as e:
        return f"❌ Erro Apify: {e}", sess
    if not itens:
        return _fmt_vazio(numero), sess
    for it in itens:
        it.setdefault("articleSearchNo", numero)
    if len(itens) == 1:
        sess["peca"]   = itens[0]
        sess["estado"] = "detalhe"
        return _fmt_detalhe(itens[0]), sess
    sess["estado"] = "lista_artigos"
    sess["opcoes"] = itens[:12]
    return _fmt_artigos(itens[:12]), sess


# ── Busca por catálogo (FIPE + Apify) ─────────────────────────
def _busca_catalogo(consulta: str, sess: dict):
    _add_hist(sess, consulta)
    sess["pendente"] = consulta

    fab_nome, mod_hint = _extrair_fab_modelo(consulta)
    ano = _extrair_ano(consulta)

    # Sem fabricante → pede para escolher
    if not fab_nome:
        try:
            marcas = fipe_marcas()
        except RuntimeError as e:
            return f"❌ Erro FIPE: {e}", sess
        sess["estado"] = "escolha_marca"
        sess["opcoes"] = marcas
        return _fmt_marcas(marcas[:20]), sess

    # Busca fabricante na FIPE
    try:
        marcas = fipe_marcas()
    except RuntimeError as e:
        return f"❌ Erro FIPE: {e}", sess

    marca = _melhor(fab_nome, marcas, "nome")
    if not marca:
        sess["estado"] = "escolha_marca"
        sess["opcoes"] = marcas
        return f"Não encontrei '{fab_nome}'. Escolha:\n\n" + _fmt_marcas(marcas[:20]), sess

    sess["fab_cod"]  = marca["codigo"]
    sess["fab_nome"] = marca["nome"]

    # Modelos
    try:
        modelos = fipe_modelos(marca["codigo"])
    except RuntimeError as e:
        return f"❌ Erro FIPE: {e}", sess

    mod = None
    if mod_hint:
        mod = _melhor(mod_hint, modelos, "nome")
    if not mod:
        mod = _melhor(consulta, modelos, "nome")

    if not mod:
        sess["estado"] = "escolha_modelo"
        sess["opcoes"] = modelos
        return _fmt_modelos(modelos[:20], marca["nome"]), sess

    sess["mod_cod"]  = mod["codigo"]
    sess["mod_nome"] = mod["nome"]

    return _continuar_versoes(sess, ano, consulta)


def _continuar_versoes(sess: dict, ano: str | None, consulta: str):
    try:
        versoes = fipe_anos(sess["fab_cod"], sess["mod_cod"])
    except RuntimeError as e:
        return f"❌ Erro FIPE: {e}", sess

    # Filtra por ano se informado
    filtradas = versoes
    if ano:
        filtradas = [v for v in versoes if ano in v.get("nome","")]
        if not filtradas:
            filtradas = versoes  # sem filtro se não achou

    if len(filtradas) == 1:
        return _apos_versao(filtradas[0], sess, consulta)

    sess["estado"] = "escolha_versao"
    sess["opcoes"] = filtradas
    return _fmt_versoes(filtradas[:20], sess["mod_nome"], ano or ""), sess


def _apos_versao(versao: dict, sess: dict, consulta: str):
    sess["ano_cod"]  = versao["codigo"]
    sess["ano_nome"] = versao["nome"]

    # Tenta extrair nome da peça da consulta
    termos = _termos_peca(consulta, sess.get("fab_nome",""), sess.get("mod_nome",""))

    if termos:
        peca_txt = " ".join(termos)
        return _busca_peca_apify(peca_txt, sess)

    # Não identificou a peça — pede
    vn = f"{sess['mod_nome']} {sess['ano_nome']}"
    sess["estado"] = "pedir_peca"
    return f"Veículo: **{vn}**\n\nQual peça você precisa?", sess


def _receber_peca(consulta: str, sess: dict):
    return _busca_peca_apify(consulta, sess)


def _busca_peca_apify(peca_txt: str, sess: dict):
    """
    Busca a peça no Apify usando o nome do veículo + nome da peça como query OEM.
    Estratégia: busca pelo nome da peça como ArticleNumber (texto livre no TecDoc).
    """
    veiculo = f"{sess.get('fab_nome','')} {sess.get('mod_nome','')} {sess.get('ano_nome','')}".strip()
    print(f"  [Busca] veículo={veiculo!r} peça={peca_txt!r}")

    # O Apify Part Cross Reference não faz busca textual —
    # precisamos de um número. Porém, usamos o nome da peça
    # como ArticleNumber para ver se há match no TecDoc.
    try:
        itens = apify_cross(peca_txt, "ArticleNumber")
        if not itens:
            itens = apify_cross(_norm(peca_txt), "ArticleNumber")
    except RuntimeError as e:
        return f"❌ Erro Apify: {e}", sess

    if not itens:
        sess["estado"] = "pedir_peca"
        return (
            f"Não encontrei '{peca_txt}' pelo nome.\n\n"
            f"Veículo: **{veiculo}**\n"
            "Informe o número OEM ou referência da peça:\n\n"
            "  Ex: oem 7700115294\n"
            "  Ex: ref 0986018360"
        ), sess

    if len(itens) == 1:
        itens[0].setdefault("articleSearchNo", peca_txt)
        sess["peca"]   = itens[0]
        sess["estado"] = "detalhe"
        return (
            f"Veículo: **{veiculo}**\n\n" + _fmt_detalhe(itens[0])
        ), sess

    sess["estado"] = "lista_artigos"
    sess["opcoes"] = itens[:12]
    return f"Veículo: **{veiculo}**\n\n" + _fmt_artigos(itens[:12]), sess


# ── Escolhas guiadas ───────────────────────────────────────────
def _escolher_marca(cmd: str, sess: dict):
    opcoes = sess["opcoes"]
    item   = _pick(cmd, opcoes, "nome")
    if not item:
        return f"❌ Opção inválida. Digite de 1 a {min(len(opcoes),20)}.", sess
    sess["fab_cod"]  = item["codigo"]
    sess["fab_nome"] = item["nome"]
    try:
        modelos = fipe_modelos(item["codigo"])
    except RuntimeError as e:
        return f"❌ Erro FIPE: {e}", sess
    sess["estado"] = "escolha_modelo"
    sess["opcoes"] = modelos
    return _fmt_modelos(modelos[:20], item["nome"]), sess


def _escolher_modelo(cmd: str, sess: dict):
    opcoes = sess["opcoes"]
    item   = _pick(cmd, opcoes, "nome")
    if not item:
        return f"❌ Opção inválida. Digite de 1 a {min(len(opcoes),20)}.", sess
    sess["mod_cod"]  = item["codigo"]
    sess["mod_nome"] = item["nome"]
    ano = _extrair_ano(sess.get("pendente",""))
    return _continuar_versoes(sess, ano, sess.get("pendente",""))


def _escolher_versao(cmd: str, sess: dict):
    opcoes = sess["opcoes"]
    item   = _pick(cmd, opcoes, "nome")
    if not item:
        return f"❌ Opção inválida. Digite de 1 a {min(len(opcoes),20)}.", sess
    return _apos_versao(item, sess, sess.get("pendente",""))


def _pick(cmd: str, opcoes: list, campo: str) -> dict | None:
    if cmd.isdigit():
        n = int(cmd)
        return opcoes[n-1] if 1 <= n <= len(opcoes) else None
    return _melhor(cmd, opcoes, campo)


# ── Pós-detalhe ────────────────────────────────────────────────
def _compat(sess: dict):
    a   = sess.get("peca", {})
    oem = a.get("articleSearchNo") or a.get("articleNo") or ""
    if not oem:
        return "Número OEM não disponível.", sess
    try:
        veics = apify_veiculos(oem)
    except RuntimeError as e:
        return f"❌ Erro Apify: {e}", sess
    if not veics:
        return f"Não encontrei veículos compatíveis para {oem}.", sess
    txt = f"Veículos compatíveis com {oem}:\n\n"
    for i, v in enumerate(veics[:20], 1):
        desc = (v.get("fulldescription") or v.get("description") or "").strip()
        ini  = str(v.get("yearOfConstrFrom") or "")[:4]
        fim  = str(v.get("yearOfConstrTo")   or "")[:4]
        anos = f" ({ini}–{fim})" if ini else ""
        txt += f"  {i}. {desc}{anos}\n"
    txt += "\n\n1 → compatíveis  2 → análogos  3 → nova busca"
    return txt, sess


def _analogos(sess: dict):
    a   = sess.get("peca", {})
    oem = a.get("articleSearchNo") or a.get("articleNo") or ""
    if not oem:
        return "Número não disponível.", sess
    try:
        itens = apify_analogos(oem)
    except RuntimeError as e:
        return f"❌ Erro Apify: {e}", sess
    if not itens:
        return "Não encontrei análogos.", sess
    result = []
    for it in itens:
        if isinstance(it, dict) and isinstance(it.get("articles"), list):
            result.extend(it["articles"])
        elif isinstance(it, dict):
            result.append(it)
    artigos = result or itens
    if len(artigos) == 1:
        sess["peca"]   = artigos[0]
        sess["estado"] = "detalhe"
        return _fmt_detalhe(artigos[0]), sess
    sess["estado"] = "lista_artigos"
    sess["opcoes"] = artigos[:12]
    return f"Análogos para {oem}:\n\n" + _fmt_artigos(artigos[:12]), sess


# ── Utilitários ────────────────────────────────────────────────
def _add_hist(sess, q):
    h = sess.get("historico", [])
    h.append({"h": datetime.now().strftime("%H:%M"), "q": q})
    sess["historico"] = h[-10:]

def _ajuda() -> str:
    return (
        "Como usar:\n\n"
        "  Texto livre:\n"
        "    palio 2006 embreagem\n"
        "    kwid 2020 filtro oleo\n"
        "    hb20 pastilha freio\n"
        "    corolla amortecedor\n\n"
        "  O sistema pergunta fabricante → modelo → versão\n"
        "  quando não consegue identificar automaticamente.\n\n"
        "  Número OEM direto:\n"
        "    oem 7700115294\n"
        "    ref 0986018360\n\n"
        "  Após ver uma peça:\n"
        "    1 → veículos compatíveis\n"
        "    2 → análogos / similares\n"
        "    3 → nova busca\n\n"
        "  /limpar  /ajuda\n\n"
        f"  🌐 Veículos: API FIPE (gratuita)\n"
        f"  🌐 Peças:    Apify Cross Reference\n"
        f"  🔑 Token:    {'✅ OK' if APIFY_TOKEN else '❌ ausente no .env'}"
    )

# ── Rotas Flask ────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(str(BASE_DIR), "index.html")

@app.route("/chat", methods=["POST"])
def chat():
    key, sess = get_sess()
    if request.is_json:
        data     = request.get_json(silent=True) or {}
        mensagem = data.get("message","").strip()
    else:
        mensagem = request.form.get("message","").strip()
    if not mensagem:
        return jsonify({"error": "Nenhuma mensagem."}), 400
    resp, sess = processar(mensagem, sess)
    save_sess(key, sess)
    return jsonify({"response": resp})

@app.route("/reset", methods=["POST"])
def reset_route():
    k, _ = get_sess()
    save_sess(k, _novo())
    return jsonify({"ok": True})

@app.route("/ping")
def ping():
    k, sess = get_sess()
    return jsonify({
        "status": "ok", "estado": sess["estado"],
        "apify_token": bool(APIFY_TOKEN), "client": k,
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("=" * 60)
    print(f"🚀  AutoFlex OEM  →  http://localhost:{port}")
    print(f"🌐  Veículos : API FIPE pública (parallelum.com.br)")
    print(f"🌐  Peças    : Apify {APIFY_ACTOR}")
    print(f"🔑  Token    : {'✅ OK' if APIFY_TOKEN else '❌ ausente'}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)