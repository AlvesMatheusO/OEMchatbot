import os
import requests

APIFY_TOKEN = os.getenv("APIFY_TOKEN")

ACTOR = "making-data-meaningful~tecdoc"

BASE_URL = (
    f"https://api.apify.com/v2/acts/"
    f"{ACTOR}/run-sync-get-dataset-items"
)


def external_search(
    modelo,
    ano,
    peca
):

    query = f"{peca} {modelo} {ano}"

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

            print(
                "ERRO APIFY:",
                response.text
            )

            return None

        data = response.json()

        return parse_apify_response(data)

    except Exception as e:

        print("ERRO EXTERNAL SEARCH:", e)

        return None


def parse_apify_response(data):

    if not data:
        return None

    try:

        item = data[0]

        return {
            "status": "external",
            "descricao": item.get(
                "articleName"
            ),

            "oem": item.get(
                "oemNumber"
            ),

            "marca": item.get(
                "supplierName"
            ),

            "referencia": item.get(
                "articleNo"
            ),

            "imagem": item.get(
                "imageUrl"
            ),
        }

    except Exception as e:

        print("PARSE ERROR:", e)

        return None