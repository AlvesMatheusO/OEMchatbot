import os
import secrets
import re
from flask import (
    Flask,
    request,
    jsonify,
    send_from_directory,
    session
)
from flask_cors import CORS
from pathlib import Path

from services.parser_service import parse_query
from services.catalog_service import (
    search_vehicle_fitment,
    search_products_by_vehicle,
    search_oem_parts_by_term,
    get_cross_refs,
    get_compatible_vehicles,
    search_by_oem,
)
from services.external_search import external_search

app = Flask(__name__)

# Configuração para Railway - usa variável de ambiente ou gera chave
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# CORS configurado para aceitar qualquer origem em produção
CORS(app, supports_credentials=True)

BASE_DIR = Path(__file__).resolve().parent

# =========================================================
# MEMÓRIA
# =========================================================

SESSION_MEMORY = {}

def get_memory():
    sid = session.get("sid")
    if not sid:
        sid = secrets.token_hex(16)
        session["sid"] = sid
    if sid not in SESSION_MEMORY:
        SESSION_MEMORY[sid] = {
            "step": "menu",
            "history": [],
            "results": [],
            "selected": None,
        }
    return SESSION_MEMORY[sid]

def reset_memory():
    sid = session.get("sid")
    if sid and sid in SESSION_MEMORY:
        SESSION_MEMORY[sid] = {
            "step": "menu",
            "history": [],
            "results": [],
            "selected": None,
        }

# =========================================================
# MENU
# =========================================================

def build_menu():
    return (
        "🔧 Bem-vindo ao AutoFlex OEM\n\n"
        "Sou seu assistente de catálogo de peças. "
        "Você pode buscar por:\n\n"
        "• Descrição → cabo embreagem palio 2006\n"
        "• Código SKU → 4002\n"
        "• Referência OEM → oem 55204912\n\n"
        "Use os botões acima ou digite sua busca."
    )

# =========================================================
# DETALHE
# =========================================================

def build_detalhe(produto: dict) -> str:
    oem = produto.get("codigo_oem") or produto.get("oem_montadora") or "---"
    sku = produto.get("sku_autoflex") or produto.get("ref_autoflex") or "---"
    mon = produto.get("montadora") or produto.get("fabricante") or "---"
    apl = produto.get("aplicacao") or produto.get("veiculo") or ""
    loc = produto.get("local") or ""

    txt = "✅ Produto selecionado\n\n"
    txt += f"🔧 {produto.get('descricao')}\n\n"
    txt += f"OEM      : {oem}\n"
    txt += f"SKU      : {sku}\n"
    txt += f"Montadora: {mon}\n"
    if loc:
        txt += f"Local    : {loc}\n"
    if apl:
        txt += f"\nAplicação:\n{apl[:300]}\n"

    cr = get_cross_refs(sku)
    if cr:
        marcas = [(k, v) for k, v in cr.items() 
                  if k not in ("sku_autoflex", "descricao") and v and str(v) not in ("nan", "")]
        if marcas:
            txt += "\nReferências cruzadas:\n"
            for marca, ref in marcas[:6]:
                txt += f"  • {marca}: {ref}\n"

    txt += (
        "\nEscolha:\n"
        "1️⃣ Aplicação\n"
        "2️⃣ Similares\n"
        "3️⃣ Nova busca\n"
        "4️⃣ Menu principal"
    )
    return txt

# =========================================================
# LISTA
# =========================================================

def build_lista(produtos: list) -> str:
    txt = "🔧 Peças encontradas\n\n"
    for i, p in enumerate(produtos[:5], 1):
        oem = p.get("codigo_oem") or p.get("oem_montadora") or "---"
        sku = p.get("sku_autoflex") or p.get("ref_autoflex") or "---"
        mon = p.get("montadora") or p.get("fabricante") or "---"
        txt += (
            f"\n{i}️⃣ {p.get('descricao')}\n"
            f"OEM: {oem}  SKU: {sku}  Montadora: {mon}\n"
        )
    txt += "\nDigite o número da peça desejada."
    return txt

# =========================================================
# ROTAS
# =========================================================

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/ping")
def ping():
    return jsonify({"status": "ok", "pecas": 120000})

@app.route("/chat", methods=["POST"])
def chat():
    memory = get_memory()
    data = request.form
    message = data.get("message", "").strip()
    msg_low = message.lower()

    print(f"\n{'='*40}")
    print(f"BUSCA: {message!r}")
    print(f"{'='*40}")

    # COMANDOS
    if msg_low in ("/menu", "menu", "0"):
        memory["step"] = "menu"
        return jsonify({"response": build_menu()})

    if msg_low in ("/reiniciar", "reiniciar"):
        reset_memory()
        return jsonify({"response": "🔄 Conversa reiniciada.\n\n" + build_menu()})

    if msg_low in ("/voltar", "voltar"):
        if memory["history"]:
            memory["history"].pop()
        return jsonify({"response": "↩️ Voltando.\n\n" + build_menu()})

    if msg_low in ("oi", "ola", "olá", "inicio", "início", "hello", "hi"):
        return jsonify({"response": build_menu()})

    # SELEÇÃO NUMÉRICA
    if msg_low.isdigit():
        idx = int(msg_low) - 1
        results = memory.get("results", [])
        step = memory.get("step", "menu")

        if step == "results" and results and 0 <= idx < len(results):
            produto = results[idx]
            memory["selected"] = produto
            memory["step"] = "detalhe"
            return jsonify({"response": build_detalhe(produto)})

        if step == "detalhe" and memory.get("selected"):
            produto = memory["selected"]
            sku = produto.get("sku_autoflex") or produto.get("ref_autoflex", "")

            if msg_low == "1":  # Aplicação
                veics = get_compatible_vehicles(sku)
                if veics:
                    txt = f"🚗 Veículos compatíveis — SKU {sku}:\n\n"
                    for v in veics[:15]:
                        ini = v.get("ano_ini", "")
                        fim = v.get("ano_fim", "")
                        anos = f" ({ini}–{fim})" if ini else ""
                        motor = f" {v.get('motor', '')}" if v.get("motor") else ""
                        txt += f"• {v.get('montadora', '')} {v.get('modelo', '')}{anos}{motor}\n"
                else:
                    apl = produto.get("aplicacao") or produto.get("veiculo", "")
                    txt = f"📋 Aplicação:\n\n{apl}" if apl else "Aplicação não disponível."
                txt += "\n\n1️⃣ Aplicação\n2️⃣ Similares\n3️⃣ Nova busca\n4️⃣ Menu principal"
                return jsonify({"response": txt})

            if msg_low == "2":  # Similares
                desc = produto.get("descricao", "")
                modelo = memory.get("last_modelo", "")
                simil = search_oem_parts_by_term(desc, modelo)
                simil = [s for s in simil if s.get("ref_autoflex") != sku and s.get("sku_autoflex") != sku]
                if simil:
                    memory["results"] = simil
                    memory["step"] = "results"
                    return jsonify({"response": "🔍 Similares:\n\n" + build_lista(simil)})
                return jsonify({"response": "Não encontrei similares.\n\n3️⃣ Nova busca"})

            if msg_low == "3":  # Nova busca
                memory["step"] = "menu"
                return jsonify({"response": "🔍 Nova busca.\n\n" + build_menu()})

            if msg_low == "4":  # Menu principal
                memory["step"] = "menu"
                return jsonify({"response": build_menu()})

    # OEM DIRETA
    oem_match = re.search(r'\boem\s*:?\s*([A-Za-z0-9\-\.]{5,})', msg_low, re.I)
    if oem_match:
        oem_num = oem_match.group(1).strip()
        produto = search_by_oem(oem_num)
        if produto:
            memory["selected"] = produto
            memory["step"] = "detalhe"
            return jsonify({"response": build_detalhe(produto)})
        return jsonify({"response": f"❌ OEM {oem_num} não encontrada no catálogo."})

    # PARSER
    parsed = parse_query(message)
    modelo = parsed.get("modelo")
    ano = parsed.get("ano")
    motor = parsed.get("motor")
    peca = parsed.get("peca")
    fabricante = parsed.get("fabricante")

    print(f"parsed → {parsed}")

    if modelo:
        memory["last_modelo"] = modelo

    if not modelo and not peca:
        return jsonify({
            "response": (
                "❌ Não consegui identificar o que você precisa.\n\n"
                "Exemplos:\n"
                "• cabo embreagem palio 2006\n"
                "• trambulador celta\n"
                "• amortecedor gol 1.0\n"
                "• oem 55204912"
            )
        })

    # BUSCA INTERNA
    produtos = []
    if modelo:
        fitments = search_vehicle_fitment(modelo, ano)
        if fitments:
            produtos = search_products_by_vehicle(fitments, peca or modelo, motor)

    if not produtos:
        termo = peca or modelo or message
        produtos = search_oem_parts_by_term(termo, modelo, fabricante)

    if produtos:
        if len(produtos) == 1:
            memory["selected"] = produtos[0]
            memory["results"] = produtos
            memory["step"] = "detalhe"
            memory["history"].append({"message": message})
            return jsonify({"response": build_detalhe(produtos[0])})
        
        memory["results"] = produtos
        memory["step"] = "results"
        memory["history"].append({"message": message})
        return jsonify({"response": build_lista(produtos)})

    # FALLBACK EXTERNO
    externos = external_search(modelo=modelo, ano=ano, peca=peca or message)
    if externos:
        txt = "🌎 Resultado encontrado via catálogo externo\n\n"
        for i, item in enumerate(externos[:5], 1):
            txt += (
                f"{i}️⃣ {item.get('descricao')}\n"
                f"OEM: {item.get('oem') or '-'}\n"
                f"Marca: {item.get('marca') or '-'}\n"
                f"Referência: {item.get('referencia') or '-'}\n\n"
            )
        txt += "\n⚠️ Resultado vindo de catálogo externo TecDoc."
        return jsonify({"response": txt})

    sugestao = f"\nModelo reconhecido: *{modelo}*\nTente variar o nome da peça." if modelo else ""
    return jsonify({
        "response": (
            f"❌ Nenhuma peça encontrada para: *{message}*"
            f"{sugestao}\n\n"
            "Dicas:\n"
            "• Seja mais específico\n"
            "• Use OEM\n"
            "• Use SKU\n"
            "• Informe o veículo"
        )
    })

# =========================================================
# MAIN - Configurado para Railway
# =========================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)  # debug=False em produção