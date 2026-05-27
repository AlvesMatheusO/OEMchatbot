SYNONYMS = {
    "cambio": "marcha",
    "capo": "capô",
    "farol": "iluminação",
    "embreagem": "embreagem",
    "amortecedor": "suspensão"
}


def apply_synonyms(text):

    for key, value in SYNONYMS.items():

        text = text.replace(key, value)

    return text