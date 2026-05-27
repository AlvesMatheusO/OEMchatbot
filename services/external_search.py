import os
import requests
from rapidfuzz import fuzz

APIFY_TOKEN = os.getenv("APIFY_TOKEN")

ACTOR = "making-data-meaningful~tecdoc"

BASE_URL = (
    f"https://api.apify.com/v2/acts/"
    f"{ACTOR}/run-sync-get-dataset-items"
)


def calculate_external_score(query, item):
    texto = " ".join([
        item.get("articleName", ""),
        item.get("supplierName", ""),
        item.get("oemNumber", ""),
        item.get("articleNo", ""),
    ]).lower()

    return fuzz.token_set_ratio(query.lower(), texto)


def external_search(modelo, ano, peca):

    query = f"{peca} {modelo or ''} {ano or ''}".strip()

    payload = {
        "search_query": query,
        "country": "BR"
    }

    headers = {
        "Authorization": f"Bearer {APIFY_TOKEN}"
    }

    try:

        response = requests.post(
            BASE_URL,
            json=payload,
            headers=headers,
            timeout=60
        )

        if response.status_code != 200:

            print("ERRO APIFY:", response.text)

            return []

        data = response.json()

        return parse_apify_response(data, query)

    except Exception as e:

        print("ERRO EXTERNAL SEARCH:", e)

        return []


def parse_apify_response(data, query):

    if not data:
        return []

    results = []

    for item in data[:20]:

        score = calculate_external_score(query, item)

        results.append({
            "status": "external",

            "descricao": item.get("articleName"),

            "oem": item.get("oemNumber"),

            "marca": item.get("supplierName"),

            "referencia": item.get("articleNo"),

            "imagem": item.get("imageUrl"),

            "_score": score,
        })

    results.sort(
        key=lambda x: x["_score"],
        reverse=True
    )

    return results[:5]