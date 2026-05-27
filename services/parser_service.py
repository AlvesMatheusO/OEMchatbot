import re
from rapidfuzz import fuzz, process

# =========================================================
# MODELOS — união das duas planilhas
# =========================================================

MODELOS = [
    # FIAT
    "palio", "siena", "uno", "argo", "cronos", "mobi", "fiorino",
    "doblo", "doblò", "toro", "strada", "pulse", "fastback",
    "bravo", "linea", "idea", "punto", "stilo", "marea", "elba",
    "premio", "prêmio", "mille", "tempra", "tipo",
    # VOLKSWAGEN
    "gol", "polo", "virtus", "voyage", "saveiro", "fox", "taos",
    "nivus", "tcross", "amarok", "tiguan", "jetta", "golf",
    "crossfox", "spacefox", "up", "parati", "passat", "bora",
    "santana", "variant", "vento",
    # CHEVROLET / GM
    "onix", "tracker", "spin", "montana", "cobalt", "celta",
    "corsa", "vectra", "astra", "s10", "cruze", "prisma",
    "agile", "zafira", "blazer", "kadett", "ipanema", "omega",
    "meriva", "classic", "sonic", "equinox", "trailblazer",
    "captiva", "monza", "calibra", "tigra", "sigma",
    # HONDA
    "civic", "hrv", "hr-v", "wrv", "wr-v", "crv", "cr-v",
    "city", "fit", "accord",
    # TOYOTA
    "corolla", "hilux", "yaris", "etios", "sw4", "rav4", "rav-4",
    "fortuner", "camry",
    # HYUNDAI
    "hb20", "hb20s", "creta", "tucson", "ix35", "i30",
    "elantra", "sonata", "azera", "veloster",
    # KIA
    "sportage", "sorento", "sorente", "cerato", "soul",
    "carnival", "picanto", "carens", "mohave",
    # RENAULT
    "kwid", "sandero", "logan", "duster", "captur", "stepway",
    "oroch", "master", "megane", "kangoo", "fluence", "symbol",
    "clio", "scenic",
    # FORD
    "ka", "fiesta", "ecosport", "ranger", "fusion", "maverick",
    "transit", "focus", "edge", "escort", "courier", "f250",
    "mondeo",
    # NISSAN
    "kicks", "versa", "march", "frontier", "tiida", "livina",
    "sentra",
    # PEUGEOT
    "208", "206", "207", "308", "307", "408", "407", "2008",
    "3008", "hoggar", "partner", "boxer", "jumper", "expert",
    # CITROËN
    "c3", "c4", "aircross", "berlingo", "xsara", "jumpy",
    # JEEP
    "compass", "renegade", "wrangler", "commander",
    # MITSUBISHI
    "outlander", "asx", "pajero", "l200", "eclipse", "triton",
    # OUTROS
    "ram", "sprinter", "vito",
    "daily", "eurocargo", "tector", "stralis", "constellation", "trakker",
]

# Fabricantes — reconhecidos mas NÃO são modelos
# Quando o usuário digitar um fabricante, o parser deve anotá-lo
# mas não confundir com modelo
FABRICANTES = {
    "fiat", "volkswagen", "vw", "chevrolet", "gm", "ford", "renault",
    "toyota", "honda", "hyundai", "nissan", "kia", "jeep", "mitsubishi",
    "peugeot", "citroen", "citroën", "mercedes", "bmw", "audi",
    "iveco", "volvo", "scania", "sprinter",
}

# Aliases — variações comuns
ALIASES = {
    "grand siena":   "siena",
    "grand palio":   "palio",
    "novo uno":      "uno",
    "novo palio":    "palio",
    "grand punto":   "punto",
    "vw gol":        "gol",
    "vw polo":       "polo",
    "hb 20":         "hb20",
    "hr v":          "hrv",
    "wr v":          "wrv",
    "cr v":          "crv",
    "rav 4":         "rav4",
    "sw 4":          "sw4",
    "s 10":          "s10",
    "ix 35":         "ix35",
    "i 30":          "i30",
}

MOTORES = [
    "1.0", "1.3", "1.4", "1.5", "1.6", "1.8", "2.0", "2.4", "2.8",
    "fire", "elx", "hlx", "hlxe", "young", "sporting", "trekking",
    "flex", "turbo", "aspirado", "diesel",
    "8v", "16v", "4d55", "4d56", "e-torq", "etorq",
]

STOPWORDS = {
    "de", "da", "do", "dos", "das", "para", "com", "e", "o", "a",
    "um", "uma", "os", "as", "em", "no", "na", "por", "se",
}

SINONIMOS = {
    "câmbio":    "cambio",
    "capô":      "capo",
    "injeção":   "injecao",
    "seleção":   "selecao",
    "pastilhas": "pastilha",
    "freios":    "freio",
    "cabos":     "cabo",
}

# =========================================================
# Funções internas
# =========================================================

def normalize(text: str) -> str:
    text = text.lower().strip()
    for old, new in SINONIMOS.items():
        text = text.replace(old, new)
    text = re.sub(r"[^a-zA-Z0-9À-ÿ\s\.]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _remover_token(text: str, token: str) -> str:
    """Remove um token do texto respeitando fronteiras de palavra."""
    return re.sub(
        r'(?<![a-zA-Z0-9])' + re.escape(token) + r'(?![a-zA-Z0-9])',
        " ",
        text,
        flags=re.IGNORECASE
    ).strip()


def _remover_regex(text: str, pattern: str) -> str:
    return re.sub(pattern, " ", text, flags=re.IGNORECASE).strip()


# =========================================================
# Parser principal
# =========================================================

def parse_query(text: str) -> dict:
    original = text
    text_n   = normalize(text)
    modelo   = None
    ano      = None
    motor    = None
    fabricante = None

    # ── 1. Aliases (ex: "grand siena") ────────────────────
    for alias, canonical in ALIASES.items():
        if alias in text_n:
            modelo = canonical
            text_n = text_n.replace(alias, "")
            break

    # ── 2. Ano ────────────────────────────────────────────
    m = re.search(r"\b(19\d{2}|20\d{2})\b", text_n)
    if m:
        ano    = int(m.group(1))
        text_n = _remover_regex(text_n, r"\b" + m.group(1) + r"\b")

    # ── 3. Motor ──────────────────────────────────────────
    for mot in sorted(MOTORES, key=len, reverse=True):
        pat = r'(?<![a-zA-Z])' + re.escape(mot) + r'(?![a-zA-Z0-9])'
        if re.search(pat, text_n, re.IGNORECASE):
            motor  = mot
            text_n = re.sub(pat, " ", text_n, flags=re.IGNORECASE)
            break

    # ── 4. Fabricante — remove do texto sem confundir modelo
    for fab in FABRICANTES:
        pat = r'\b' + re.escape(fab) + r'\b'
        if re.search(pat, text_n, re.IGNORECASE):
            fabricante = fab.upper()
            text_n = re.sub(pat, " ", text_n, flags=re.IGNORECASE)
            break

    # ── 5. Modelo — match exato (palavra inteira) ──────────
    if not modelo:
        for m_name in sorted(MODELOS, key=len, reverse=True):
            pat = r'\b' + re.escape(m_name) + r'\b'
            if re.search(pat, text_n, re.IGNORECASE):
                modelo = m_name
                text_n = re.sub(pat, " ", text_n, flags=re.IGNORECASE)
                break

    # ── 6. Modelo — fuzzy para tokens longos (≥5 chars), threshold 85%
    #       Exclui palavras de peça para evitar lente/manopla→modelo errado
    if not modelo:
        TERMOS_PECA = {
            "lente","manopla","cabo","freio","embreagem","amortecedor",
            "pastilha","filtro","bomba","sensor","vela","disco","bucha",
            "correia","rolamento","radiador","alternador","bateria",
            "suporte","reacao","alavanca","trambulador","engate","selecao",
            "conjunto","reparo","capo","porta","tampa","abertura",
            "combustivel","direcao","suspensao","articulador","anel",
        }
        tokens = [t for t in text_n.split()
                  if t not in STOPWORDS
                  and len(t) >= 5
                  and t.lower() not in TERMOS_PECA]
        for token in tokens:
            result = process.extractOne(token, MODELOS, scorer=fuzz.ratio)
            if result and result[1] >= 85:
                modelo = result[0]
                text_n = _remover_token(text_n, token)
                break

    # ── 7. Peça = o que sobrou ─────────────────────────────
    peca = re.sub(r"\s+", " ", text_n).strip()
    peca = " ".join(
        t for t in peca.split()
        if t not in STOPWORDS and len(t) > 1
    )

    return {
        "original":   original,
        "modelo":     modelo,
        "fabricante": fabricante,
        "ano":        ano,
        "motor":      motor,
        "peca":       peca,
    }