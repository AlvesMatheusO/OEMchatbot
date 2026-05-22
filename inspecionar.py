#!/usr/bin/env python3
"""
Verifica modelos de todas as variantes de cada marca.
"""
import os, json, requests
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

KEY  = os.getenv("RAPIDAPI_KEY","")
HOST = "auto-parts-catalog.p.rapidapi.com"
HDR  = {"x-rapidapi-host": HOST, "x-rapidapi-key": KEY}

def get(path, params=None):
    r = requests.get(f"https://{HOST}/{path}", headers=HDR, params=params or {}, timeout=15)
    return r.json() if r.ok else None

def modelos_de(mfr_id, mfr_nome):
    d = get(f"models/list/type-id/1/manufacturer-id/{mfr_id}/lang-id/4/country-filter-id/63")
    if not d: return []
    if isinstance(d, list): return d
    # descobre chave correta
    for k in ("models","modelSeries","data","result","items","vehicleModels"):
        if k in d and isinstance(d[k], list): return d[k]
    # mostra estrutura desconhecida
    print(f"    ⚠️  Estrutura desconhecida para {mfr_nome}: {list(d.keys())[:5]}")
    print(f"    Exemplo: {json.dumps(d)[:300]}")
    return []

# Pega todos os fabricantes
d = get("manufacturers/list/type-id/1")
fabs = d.get("manufacturers", []) if d else []

# Grupos de marcas para testar — todas as variantes
GRUPOS = {
    "FIAT":       ["fiat"],
    "VW":         ["vw"],
    "CHEVROLET":  ["chevrolet"],
    "FORD":       ["ford"],
    "RENAULT":    ["renault"],
    "TOYOTA":     ["toyota"],
    "HONDA":      ["honda"],
    "HYUNDAI":    ["hyundai"],
}

MODELOS_BR = [
    "palio","uno","strada","siena","argo","cronos","mobi","fiorino","doblo",
    "bravo","punto","toro","pulse","fastback",
    "gol","voyage","saveiro","virtus","polo","fox","up","t-cross","nivus","taos",
    "onix","cobalt","spin","celta","corsa","agile","montana","tracker","s10",
    "ka","fiesta","ecosport","ranger","territory","bronco",
    "kwid","sandero","logan","duster","captur","stepway","oroch",
    "hb20","creta","tucson","santa fe","ix35",
    "hilux","corolla","yaris","etios","sw4","rav4","camry","prius",
    "civic","fit","city","hr-v","wr-v","cr-v","br-v",
]

print("=" * 60)
print("VERIFICANDO MODELOS BRASILEIROS POR MARCA")
print("=" * 60)

resumo = {}

for grupo, palavras in GRUPOS.items():
    print(f"\n{'─'*50}")
    print(f"▶ {grupo}")
    variantes = [f for f in fabs
                 if any(p in f["manufacturerName"].lower() for p in palavras)
                 and "(" not in f["manufacturerName"]]   # pega só o principal

    # se não achou sem parênteses, pega todos
    if not variantes:
        variantes = [f for f in fabs
                     if any(p in f["manufacturerName"].lower() for p in palavras)]

    total_modelos_br = []
    for fab in variantes:
        mfr_id   = fab["manufacturerId"]
        mfr_nome = fab["manufacturerName"]
        mods     = modelos_de(mfr_id, mfr_nome)
        br       = []
        for m in mods:
            nome_m = str(m.get("modelName") or m.get("vehicleModelSeriesName") or
                        m.get("name","")).lower()
            for br_nome in MODELOS_BR:
                if br_nome in nome_m:
                    br.append(m.get("modelName") or m.get("vehicleModelSeriesName") or nome_m)
                    break
        if br:
            print(f"  ✅ {mfr_nome} (id={mfr_id}) → {len(mods)} modelos totais")
            for b in br:
                print(f"      🔹 {b}")
            total_modelos_br.extend(br)
        else:
            print(f"  ⚪ {mfr_nome} (id={mfr_id}) → {len(mods)} modelos — nenhum BR")
            if mods:
                nomes = [m.get("modelName") or m.get("vehicleModelSeriesName","") for m in mods[:5]]
                print(f"     Exemplos: {', '.join(nomes)}")

    resumo[grupo] = total_modelos_br

print("\n" + "=" * 60)
print("RESUMO FINAL")
print("=" * 60)
for marca, modelos in resumo.items():
    status = "✅" if modelos else "❌"
    print(f"  {status} {marca}: {', '.join(modelos[:5]) if modelos else 'nenhum modelo BR'}")