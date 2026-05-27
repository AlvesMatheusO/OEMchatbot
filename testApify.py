#!/usr/bin/env python3
"""
test_apify.py — Testa o Apify Part Number Cross Reference diretamente.

Uso:
    python test_apify.py                        # modo interativo
    python test_apify.py 7700115294             # cross-reference OENumber
    python test_apify.py 7700115294 --compat    # veículos compatíveis
    python test_apify.py 7700115294 --analogos  # análogos
    python test_apify.py 7700115294 --raw       # JSON bruto (debug)
    python test_apify.py 7700115294 --tipo ArticleNumber

Comandos no modo interativo:
    <número>            cross-reference OENumber
    <número> art        cross-reference ArticleNumber
    <número> compat     veículos compatíveis
    <número> analogos   análogos/similares
    <número> raw        JSON bruto
    sair
"""

import os, sys, json, time, argparse
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

APIFY_TOKEN  = os.getenv("APIFY_TOKEN", "")
APIFY_ACTOR  = "making-data-meaningful~part-number-cross-reference"
APIFY_URL    = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"
APIFY_TIMEOUT = 90

# ── Cores ───────────────────────────────────────────────────
G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"
C = "\033[96m"; B = "\033[1m";  D = "\033[2m"; X = "\033[0m"
def g(t): return f"{G}{t}{X}"
def y(t): return f"{Y}{t}{X}"
def r(t): return f"{R}{t}{X}"
def c(t): return f"{C}{t}{X}"
def b(t): return f"{B}{t}{X}"
def d(t): return f"{D}{t}{X}"


# ── Chamada Apify ───────────────────────────────────────────
def apify_call(payload: dict) -> tuple[list, float]:
    if not APIFY_TOKEN:
        print(r("❌ APIFY_TOKEN não encontrado no .env")); sys.exit(1)

    print(d(f"  → {json.dumps(payload, ensure_ascii=False)}"))
    print(f"  ⏳ Aguardando...", end="", flush=True)
    t0 = time.time()

    try:
        resp = requests.post(
            APIFY_URL,
            params={"token": APIFY_TOKEN},
            json=payload,
            timeout=APIFY_TIMEOUT,
        )
        elapsed = time.time() - t0
        print(f" {elapsed:.1f}s  HTTP {resp.status_code}")

        if resp.status_code not in (200, 201):
            print(r(f"  ❌ {resp.text[:300]}")); return [], elapsed

        data = resp.json()
        artigos = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    if isinstance(item.get("articles"), list):
                        artigos.extend(item["articles"])
                    elif "articleId" in item or "articleNo" in item:
                        artigos.append(item)
        elif isinstance(data, dict):
            artigos = data.get("articles", data.get("items", []))

        return artigos, elapsed

    except requests.Timeout:
        print(r(f"\n  ❌ Timeout após {APIFY_TIMEOUT}s")); return [], APIFY_TIMEOUT
    except Exception as e:
        print(r(f"\n  ❌ {e}")); return [], 0.0


# ── Deduplicação ────────────────────────────────────────────
def deduplicar(artigos: list) -> list:
    """
    Agrupa por (referência + marca) e mantém apenas 1 item por grupo.
    Preserva a imagem principal (primeiro .webp encontrado).
    """
    grupos: dict = {}
    for a in artigos:
        ref   = (a.get("articleNo") or "").strip()
        marca = (a.get("supplierName") or "").strip()
        chave = f"{ref}||{marca}"

        if chave not in grupos:
            grupos[chave] = dict(a)
            grupos[chave]["_imgs"] = []

        img = a.get("s3image","")
        if img and img.endswith(".webp"):
            grupos[chave]["_imgs"].append(img)

    # Mantém só a primeira imagem por grupo
    result = []
    for item in grupos.values():
        imgs = item.pop("_imgs", [])
        if imgs:
            item["s3image"] = imgs[0]
        else:
            item.pop("s3image", None)
        result.append(item)

    return result


# ── Formatação ──────────────────────────────────────────────
def fmt_artigo(a: dict, idx: int) -> str:
    nome  = (a.get("articleProductName") or a.get("name") or "—").strip()
    ref   = (a.get("articleNo") or "").strip()
    marca = (a.get("supplierName") or "").strip()
    oem   = (a.get("articleSearchNo") or "").strip()
    img   = a.get("s3image","")

    linhas = [f"  {b(str(idx)+'.')} {g(nome)}  {d('('+marca+')')}"]
    linhas.append(f"     Ref: {b(ref)}  |  OEM: {oem}")
    if img:
        linhas.append(f"     🖼  {d(img)}")
    return "\n".join(linhas)


def fmt_veiculo(v: dict, idx: int) -> str:
    desc = (v.get("fulldescription") or v.get("description") or v.get("name","")).strip()
    ini  = str(v.get("yearOfConstrFrom") or "")[:4]
    fim  = str(v.get("yearOfConstrTo")   or "")[:4]
    anos = f"  {y('('+ini+'–'+fim+')')}" if ini else ""
    return f"  {b(str(idx)+'.')} {desc}{anos}"


def cabecalho(titulo: str):
    print(f"\n{b('═'*55)}")
    print(f"  {b(titulo)}")
    print(b('═'*55))


# ── Endpoints ───────────────────────────────────────────────
def cross_reference(numero: str, tipo: str = "OENumber") -> list:
    cabecalho(f"CROSS-REFERENCE: {numero}  [{tipo}]")

    artigos, tempo = apify_call({
        "selectPageType": "search-for-cross-number-by-article-no",
        "articleNo":      numero,
        "articleType":    tipo,
    })

    if not artigos:
        print(r("  ✗ Sem resultados.")); return []

    total     = len(artigos)
    unicos    = deduplicar(artigos)
    removidos = total - len(unicos)

    print(g(f"\n  ✓ {total} resultado(s)") +
          (d(f"  →  {len(unicos)} únicos após deduplicar {removidos} duplicatas") if removidos else ""))

    for i, a in enumerate(unicos[:20], 1):
        print(fmt_artigo(a, i))
        print()

    if len(unicos) > 20:
        print(y(f"  ... e mais {len(unicos)-20} resultados únicos"))

    return unicos


def veiculos_compativeis(oem: str):
    cabecalho(f"VEÍCULOS COMPATÍVEIS: {oem}")

    itens, tempo = apify_call({
        "selectPageType": "get-list-of-vehicles-by-oem-part",
        "articleOemNo":   oem,
    })

    if not itens:
        print(r("  ✗ Sem veículos compatíveis.")); return

    print(g(f"\n  ✓ {len(itens)} veículo(s)\n"))
    for i, v in enumerate(itens[:25], 1):
        print(fmt_veiculo(v, i))

    if len(itens) > 25:
        print(y(f"\n  ... e mais {len(itens)-25} veículos"))


def analogos(oem: str):
    cabecalho(f"ANÁLOGOS/SIMILARES: {oem}")

    itens, tempo = apify_call({
        "selectPageType": "search-for-analogue-of-spare-parts-by-oem-number",
        "articleOemNo":   oem,
    })

    artigos = []
    for it in itens:
        if isinstance(it.get("articles"), list): artigos.extend(it["articles"])
        elif "articleId" in it or "articleNo" in it: artigos.append(it)
    artigos = artigos or itens

    if not artigos:
        print(r("  ✗ Sem análogos.")); return

    unicos = deduplicar(artigos)
    print(g(f"\n  ✓ {len(artigos)} análogo(s)  →  {len(unicos)} únicos\n"))
    for i, a in enumerate(unicos[:15], 1):
        print(fmt_artigo(a, i))
        print()


def raw_json(numero: str, tipo: str = "OENumber"):
    cabecalho(f"RAW JSON: {numero}  [{tipo}]")
    if not APIFY_TOKEN:
        print(r("❌ Token ausente")); return

    resp = requests.post(
        APIFY_URL,
        params={"token": APIFY_TOKEN},
        json={"selectPageType": "search-for-cross-number-by-article-no",
              "articleNo": numero, "articleType": tipo},
        timeout=APIFY_TIMEOUT,
    )
    print(f"HTTP {resp.status_code}\n")
    try:
        data = resp.json()
        # Mostra só os primeiros 2 artigos para não poluir
        if isinstance(data, list) and data:
            item = data[0]
            if isinstance(item.get("articles"), list):
                item = dict(item)
                item["articles"] = item["articles"][:2]
                item["_nota"] = f"Exibindo 2 de {data[0].get('countArticles','?')} artigos"
                print(json.dumps([item], ensure_ascii=False, indent=2))
            else:
                print(json.dumps(data[:3], ensure_ascii=False, indent=2))
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
    except:
        print(resp.text[:1000])


# ── Modo interativo ─────────────────────────────────────────
HELP = f"""
  {b('Comandos:')}
    <número>              cross-reference como OENumber
    <número> art          cross-reference como ArticleNumber
    <número> compat       veículos compatíveis
    <número> analogos     análogos/similares
    <número> raw          JSON bruto (debug)
    sair / exit           encerrar

  {b('Exemplos:')}
    7700115294            → motor de arranque Renault
    55208207 compat       → veículos que usam este cabo
    0986018360 art        → busca pela ref aftermarket BOSCH
"""

def modo_interativo():
    print(f"\n{b('═'*55)}")
    print(f"  {b('AutoFlex — Teste Apify Cross Reference')}")
    print(b('═'*55))
    tok_status = g("✓ " + APIFY_TOKEN[:24] + "...") if APIFY_TOKEN else r("✗ não encontrado")
    print(f"  Token : {tok_status}")
    print(f"  Actor : {d(APIFY_ACTOR)}")
    print(HELP)

    while True:
        try:
            entrada = input(c("OEM> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAté logo!"); break

        if not entrada: continue
        if entrada.lower() in ("sair","exit","quit","q"):
            print("Até logo!"); break
        if entrada.lower() in ("help","ajuda","?"): 
            print(HELP); continue

        partes = entrada.split()
        numero = partes[0]
        cmd    = partes[1].lower() if len(partes) > 1 else ""

        if cmd in ("compat","compativel","compativeis","veiculos","v"):
            veiculos_compativeis(numero)
        elif cmd in ("analogos","similar","similares","a"):
            analogos(numero)
        elif cmd in ("art","article","ref","r"):
            cross_reference(numero, "ArticleNumber")
        elif cmd == "raw":
            raw_json(numero)
        else:
            unicos = cross_reference(numero, "OENumber")
            if not unicos:
                print(y("  → Sem resultado como OENumber, tentando ArticleNumber..."))
                cross_reference(numero, "ArticleNumber")


# ── CLI ─────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Testa Apify Cross Reference")
    ap.add_argument("numero",  nargs="?",  help="Número OEM ou referência")
    ap.add_argument("--tipo",  default="OENumber",
                    choices=["OENumber","ArticleNumber","TradeNumber","EAN"])
    ap.add_argument("--compat",   action="store_true")
    ap.add_argument("--analogos", action="store_true")
    ap.add_argument("--raw",      action="store_true")
    args = ap.parse_args()

    if not args.numero:
        modo_interativo(); return

    if args.raw:           raw_json(args.numero, args.tipo)
    elif args.compat:      veiculos_compativeis(args.numero)
    elif args.analogos:    analogos(args.numero)
    else:
        unicos = cross_reference(args.numero, args.tipo)
        if not unicos and args.tipo == "OENumber":
            print(y("\n  → Tentando como ArticleNumber..."))
            cross_reference(args.numero, "ArticleNumber")

if __name__ == "__main__":
    main()