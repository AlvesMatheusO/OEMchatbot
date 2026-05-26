#!/usr/bin/env python3
"""
db.py – Camada SQLite para o AutoFlex OEM
==========================================
Banco local que cresce por demanda (lazy-loading):
  • Fabricantes e categorias: carregados no boot.
  • Modelos de um fabricante: carregados na primeira consulta.
  • Versões de um modelo: carregadas na primeira consulta.
  • Artigos de um veículo+categoria: carregados na primeira consulta.
  • OEMs de um artigo: carregados na primeira consulta.

Após carregado uma vez, tudo sai do SQLite (µs) sem tocar a API.

Uso:
    from db import DB
    db = DB()                  # abre/cria autoflex_oem.db
    db.boot()                  # carrega fabricantes + categorias (rápido)

    veiculos = db.veiculos_do_modelo(mod_id)
    artigos  = db.artigos(veh_id, cat_id)
    resultado= db.busca_oem("7700115294")
"""

import sqlite3, json, os, re, time
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

RAPIDAPI_KEY  = os.getenv("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "auto-parts-catalog.p.rapidapi.com"
BASE_URL      = f"https://{RAPIDAPI_HOST}"
LANG_ID       = 4
COUNTRY_ID    = 63
TYPE_ID       = 1

DB_PATH = BASE_DIR / "autoflex_oem.db"

# ── Fabricantes BR que interessam (nome exato como vem da API) ─
FABRICANTES_BR = {
    "FIAT", "VOLKSWAGEN", "CHEVROLET", "FORD", "RENAULT",
    "HYUNDAI", "TOYOTA", "HONDA", "NISSAN", "KIA",
    "PEUGEOT", "CITROËN", "MITSUBISHI", "JEEP",
    "BMW", "MERCEDES-BENZ", "AUDI", "VOLVO",
    "SUBARU", "SUZUKI", "LAND ROVER",
}

# ── HTTP ───────────────────────────────────────────────────────
_HTTP_CACHE: dict = {}

def _get(path: str, params: dict | None = None, retries: int = 3):
    ck = path + str(sorted((params or {}).items()))
    if ck in _HTTP_CACHE:
        return _HTTP_CACHE[ck]
    if not RAPIDAPI_KEY:
        raise RuntimeError("RAPIDAPI_KEY não configurada no .env")
    hdrs = {"x-rapidapi-host": RAPIDAPI_HOST, "x-rapidapi-key": RAPIDAPI_KEY}
    url  = f"{BASE_URL}/{path.lstrip('/')}"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=hdrs, params=params or {}, timeout=20)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 10))
                print(f"  ⏳ rate-limit, aguardando {wait}s…")
                time.sleep(wait)
                continue
            r.raise_for_status()
            d = r.json()
            _HTTP_CACHE[ck] = d
            return d
        except requests.HTTPError as e:
            print(f"  ❌ HTTP {e.response.status_code} {path}")
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                print(f"  ❌ {e}")
                return None
    return None

# ── Helpers de parse da API ────────────────────────────────────

def _v(val) -> str:
    s = str(val).strip() if val is not None else ""
    return "" if s.lower() in ("nan", "none", "null", "") else s

def _lista(d, *chaves):
    """Extrai a primeira lista encontrada em d pelas chaves dadas."""
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        for k in chaves:
            if k in d and isinstance(d[k], list):
                return d[k]
        for v in d.values():
            if isinstance(v, list):
                return v
    return []

def _nome_veh(it: dict) -> str:
    full = _v(it.get("fulldescription") or it.get("description") or
               it.get("vehicleName") or it.get("name", ""))
    if full:
        return full
    partes = []
    for c in ("mfrName", "manufacturerName", "modelName",
               "vehicleModelSeriesName", "typeName", "engineName"):
        v = _v(it.get(c, ""))
        if v and v not in partes:
            partes.append(v)
    return " ".join(partes)

def _anos(it: dict) -> tuple[str, str]:
    ini = str(it.get("modelYearFrom") or it.get("yearOfConstrFrom") or
              it.get("constructionYearFrom") or "")[:4]
    fim = str(it.get("modelYearTo") or it.get("yearOfConstrTo") or
              it.get("constructionYearTo") or "")[:4]
    return ini, fim

def _atributos_json(artigo: dict) -> str:
    crit = artigo.get("criteria") or artigo.get("attributes") or []
    if not isinstance(crit, list):
        return "[]"
    attrs = []
    for c in crit:
        cn = _v(c.get("criteriaDescription") or c.get("name", ""))
        cv = _v(c.get("rawValue") or c.get("value", ""))
        un = _v(c.get("criteriaUnitDescription") or c.get("unit", ""))
        if cn and cv:
            attrs.append({"nome": cn, "valor": cv, "unidade": un})
    return json.dumps(attrs, ensure_ascii=False)

def _oems_do_artigo(artigo: dict) -> list[dict]:
    """Extrai OEMs embutidos na resposta de artigo (se existirem)."""
    oems = []
    for campo in ("oemNumbers", "oems", "oeNumbers", "crossReferences"):
        lst = artigo.get(campo, [])
        if isinstance(lst, list):
            for o in lst:
                if isinstance(o, dict):
                    num = _v(o.get("oemNumber") or o.get("number") or o.get("articleNo", ""))
                    fab = _v(o.get("mfrName") or o.get("manufacturer", ""))
                    if num:
                        oems.append({"oem_numero": num, "fabricante": fab})
                elif isinstance(o, str) and o.strip():
                    oems.append({"oem_numero": o.strip(), "fabricante": ""})
    return oems


# ══════════════════════════════════════════════════════════════
class DB:
    """Banco SQLite com lazy-loading da API."""

    def __init__(self, path: str | Path = DB_PATH):
        self.path = Path(path)
        self.con  = sqlite3.connect(str(self.path), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA foreign_keys=ON")
        self._criar_schema()

    # ── Schema ─────────────────────────────────────────────────
    def _criar_schema(self):
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS fabricantes (
            id      INTEGER PRIMARY KEY,
            nome    TEXT NOT NULL UNIQUE,
            ativo   INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS modelos (
            id          INTEGER PRIMARY KEY,
            fab_id      INTEGER REFERENCES fabricantes(id),
            nome        TEXT NOT NULL,
            ano_ini     TEXT DEFAULT '',
            ano_fim     TEXT DEFAULT '',
            carregado   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS veiculos (
            id          INTEGER PRIMARY KEY,
            modelo_id   INTEGER REFERENCES modelos(id),
            descricao   TEXT NOT NULL DEFAULT '',
            motor       TEXT DEFAULT '',
            ano_ini     TEXT DEFAULT '',
            ano_fim     TEXT DEFAULT '',
            carregado   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS categorias (
            id      INTEGER PRIMARY KEY,
            nome    TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS artigos (
            id          INTEGER PRIMARY KEY,
            ref         TEXT DEFAULT '',
            nome        TEXT NOT NULL DEFAULT '',
            marca       TEXT DEFAULT '',
            supplier_id INTEGER DEFAULT 0,
            atributos   TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS artigo_veiculo (
            artigo_id   INTEGER REFERENCES artigos(id),
            veiculo_id  INTEGER REFERENCES veiculos(id),
            cat_id      INTEGER REFERENCES categorias(id),
            PRIMARY KEY (artigo_id, veiculo_id, cat_id)
        );

        CREATE TABLE IF NOT EXISTS oems (
            id          INTEGER PRIMARY KEY,
            artigo_id   INTEGER REFERENCES artigos(id),
            oem_numero  TEXT NOT NULL,
            fabricante  TEXT DEFAULT '',
            UNIQUE(artigo_id, oem_numero)
        );

        CREATE TABLE IF NOT EXISTS oem_carregado (
            oem_numero  TEXT PRIMARY KEY,
            quando      TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_artigos_ref  ON artigos(ref);
        CREATE INDEX IF NOT EXISTS idx_oems_numero  ON oems(oem_numero);
        CREATE INDEX IF NOT EXISTS idx_av_veiculo   ON artigo_veiculo(veiculo_id);
        CREATE INDEX IF NOT EXISTS idx_av_cat       ON artigo_veiculo(cat_id);
        CREATE INDEX IF NOT EXISTS idx_mod_fab      ON modelos(fab_id);
        CREATE INDEX IF NOT EXISTS idx_veh_mod      ON veiculos(modelo_id);
        """)
        self.con.commit()

    # ── Boot ────────────────────────────────────────────────────
    def boot(self, so_br: bool = True):
        """Carrega fabricantes e categorias se ainda não estiverem no banco."""
        self._boot_fabricantes(so_br)
        self._boot_categorias()

    def _boot_fabricantes(self, so_br: bool):
        n = self.con.execute("SELECT COUNT(*) FROM fabricantes").fetchone()[0]
        if n > 0:
            print(f"  ✅ {n} fabricantes já no banco.")
            return
        print("  🔄 Carregando fabricantes da API…")
        d = _get(f"manufacturers/list/type-id/{TYPE_ID}")
        if not d:
            print("  ❌ Falha ao carregar fabricantes.")
            return
        mfrs = _lista(d, "manufacturers")
        rows = []
        for m in mfrs:
            fid  = m.get("manufacturerId") or m.get("mfrId") or m.get("id")
            nome = _v(m.get("manufacturerName") or m.get("mfrName") or m.get("name", ""))
            if not fid or not nome:
                continue
            if so_br and nome.upper() not in FABRICANTES_BR:
                continue
            rows.append((fid, nome))
        self.con.executemany(
            "INSERT OR IGNORE INTO fabricantes(id, nome) VALUES (?,?)", rows)
        self.con.commit()
        print(f"  ✅ {len(rows)} fabricantes salvos.")

    def _boot_categorias(self):
        n = self.con.execute("SELECT COUNT(*) FROM categorias").fetchone()[0]
        if n > 0:
            print(f"  ✅ {n} categorias já no banco.")
            return
        print("  🔄 Carregando categorias da API…")
        d = _get(f"category/type-id/{TYPE_ID}/list-category-tree-structure/lang-id/{LANG_ID}")
        if not d:
            print("  ❌ Falha ao carregar categorias.")
            return
        rows = []
        def _flat(obj):
            if not isinstance(obj, dict):
                return
            cid  = obj.get("categoryId")
            nome = _v(obj.get("categoryName", ""))
            if cid and nome:
                rows.append((cid, nome))
            ch = obj.get("children", {})
            for c in (ch.values() if isinstance(ch, dict) else ch):
                _flat(c)
        for top in (d.values() if isinstance(d, dict) else d):
            _flat(top)
        self.con.executemany(
            "INSERT OR IGNORE INTO categorias(id, nome) VALUES (?,?)", rows)
        self.con.commit()
        print(f"  ✅ {len(rows)} categorias salvas.")

    # ── Fabricantes ─────────────────────────────────────────────
    def fabricantes(self) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT id, nome FROM fabricantes WHERE ativo=1 ORDER BY nome"
        ).fetchall()

    # ── Modelos ─────────────────────────────────────────────────
    def modelos_do_fabricante(self, fab_id: int) -> list[sqlite3.Row]:
        """Retorna modelos do banco; carrega da API se ainda não foram importados."""
        rows = self.con.execute(
            "SELECT id, nome, ano_ini, ano_fim FROM modelos WHERE fab_id=?",
            (fab_id,)
        ).fetchall()
        if rows:
            return rows
        # Lazy-load
        self._carregar_modelos(fab_id)
        return self.con.execute(
            "SELECT id, nome, ano_ini, ano_fim FROM modelos WHERE fab_id=?",
            (fab_id,)
        ).fetchall()

    def _carregar_modelos(self, fab_id: int):
        d = _get(
            f"models/list/type-id/{TYPE_ID}/manufacturer-id/{fab_id}"
            f"/lang-id/{LANG_ID}/country-filter-id/{COUNTRY_ID}"
        )
        if not d:
            return
        mods = _lista(d, "models", "modelSeries", "vehicleModels", "data", "result", "items")
        rows = []
        for m in mods:
            mid  = m.get("modelId") or m.get("vehicleModelSeriesId") or m.get("id")
            nome = _v(m.get("modelName") or m.get("vehicleModelSeriesName") or
                      m.get("name") or m.get("description", ""))
            ini, fim = _anos(m)
            if mid and nome:
                rows.append((mid, fab_id, nome, ini, fim))
        self.con.executemany(
            "INSERT OR IGNORE INTO modelos(id, fab_id, nome, ano_ini, ano_fim) VALUES (?,?,?,?,?)",
            rows
        )
        self.con.commit()

    # ── Veículos ────────────────────────────────────────────────
    def veiculos_do_modelo(self, mod_id: int) -> list[sqlite3.Row]:
        rows = self.con.execute(
            "SELECT id, descricao, motor, ano_ini, ano_fim, carregado "
            "FROM veiculos WHERE modelo_id=?", (mod_id,)
        ).fetchall()
        if rows:
            return rows
        self._carregar_veiculos(mod_id)
        return self.con.execute(
            "SELECT id, descricao, motor, ano_ini, ano_fim, carregado "
            "FROM veiculos WHERE modelo_id=?", (mod_id,)
        ).fetchall()

    def _carregar_veiculos(self, mod_id: int):
        d = _get(
            f"types/type-id/{TYPE_ID}/list-vehicles-types/{mod_id}"
            f"/lang-id/{LANG_ID}/country-filter-id/{COUNTRY_ID}"
        )
        if not d:
            return
        vehs = _lista(d, "vehicles", "types", "vehicleTypes", "data", "result", "items")
        rows = []
        for v in vehs:
            vid  = v.get("vehicleId") or v.get("carId") or v.get("id")
            desc = _nome_veh(v)
            motor = _v(v.get("engineDescription") or v.get("powerDescription", ""))
            ini, fim = _anos(v)
            if vid and desc:
                rows.append((vid, mod_id, desc, motor, ini, fim))
        self.con.executemany(
            "INSERT OR IGNORE INTO veiculos(id, modelo_id, descricao, motor, ano_ini, ano_fim)"
            " VALUES (?,?,?,?,?,?)",
            rows
        )
        self.con.commit()

    # ── Artigos ─────────────────────────────────────────────────
    def artigos(self, veh_id: int, cat_id: int) -> list[sqlite3.Row]:
        """Retorna artigos do banco; carrega da API se ainda não foram importados."""
        # Verifica se já carregou esta combinação
        ja = self.con.execute(
            "SELECT 1 FROM artigo_veiculo WHERE veiculo_id=? AND cat_id=? LIMIT 1",
            (veh_id, cat_id)
        ).fetchone()

        veh_carregado = self.con.execute(
            "SELECT carregado FROM veiculos WHERE id=?", (veh_id,)
        ).fetchone()

        if not ja and (not veh_carregado or not veh_carregado["carregado"]):
            self._carregar_artigos(veh_id, cat_id)

        return self.con.execute("""
            SELECT a.id, a.ref, a.nome, a.marca, a.supplier_id, a.atributos
            FROM artigos a
            JOIN artigo_veiculo av ON av.artigo_id = a.id
            WHERE av.veiculo_id = ? AND av.cat_id = ?
        """, (veh_id, cat_id)).fetchall()

    def _carregar_artigos(self, veh_id: int, cat_id: int):
        d = _get(
            f"articles/list/type-id/{TYPE_ID}/vehicle-id/{veh_id}"
            f"/category-id/{cat_id}/lang-id/{LANG_ID}"
        )
        if not d:
            # Marca como carregado mesmo sem resultados (evita re-request)
            self.con.execute(
                "UPDATE veiculos SET carregado=1 WHERE id=?", (veh_id,))
            self.con.commit()
            return

        arts = _lista(d, "articles", "data", "result", "items")
        for a in arts:
            aid  = a.get("articleId") or a.get("id")
            ref  = _v(a.get("articleNo") or a.get("articleNumber") or a.get("articleSearchNo", ""))
            nome = _v(a.get("articleProductName") or a.get("articleName") or
                      a.get("description") or a.get("name", ""))
            marca = _v(a.get("supplierName") or a.get("brandName", ""))
            sid   = a.get("supplierId", 0) or 0
            attrs = _atributos_json(a)

            if not aid or not nome:
                continue

            self.con.execute(
                "INSERT OR IGNORE INTO artigos(id, ref, nome, marca, supplier_id, atributos)"
                " VALUES (?,?,?,?,?,?)",
                (aid, ref, nome, marca, sid, attrs)
            )
            self.con.execute(
                "INSERT OR IGNORE INTO artigo_veiculo(artigo_id, veiculo_id, cat_id)"
                " VALUES (?,?,?)",
                (aid, veh_id, cat_id)
            )

            # Salva OEMs embutidos no artigo
            for oem in _oems_do_artigo(a):
                self.con.execute(
                    "INSERT OR IGNORE INTO oems(artigo_id, oem_numero, fabricante)"
                    " VALUES (?,?,?)",
                    (aid, oem["oem_numero"], oem["fabricante"])
                )

        self.con.commit()

    # ── Busca OEM ───────────────────────────────────────────────
    def busca_oem(self, numero: str) -> list[sqlite3.Row]:
        """
        Busca artigos pelo número OEM.
        1) Tenta no banco local primeiro.
        2) Se não achar e ainda não consultou essa OEM, vai à API.
        """
        numero = numero.strip()
        # Busca local
        rows = self._oem_local(numero)
        if rows:
            return rows

        # Verifica se já tentou buscar esse OEM antes
        ja = self.con.execute(
            "SELECT 1 FROM oem_carregado WHERE oem_numero=?", (numero,)
        ).fetchone()
        if ja:
            return []  # já tentou, não tem

        # Busca na API
        self._carregar_oem(numero)
        return self._oem_local(numero)

    def _oem_local(self, numero: str) -> list[sqlite3.Row]:
        return self.con.execute("""
            SELECT DISTINCT a.id, a.ref, a.nome, a.marca, a.supplier_id, a.atributos,
                   o.oem_numero
            FROM artigos a
            JOIN oems o ON o.artigo_id = a.id
            WHERE o.oem_numero = ?
        """, (numero,)).fetchall()

    def _carregar_oem(self, numero: str):
        from datetime import datetime
        # Tenta ArticleNumber e OENumber
        for tipo in ("ArticleNumber", "OENumber"):
            d = _get("artlookup/search-articles-by-article-no", {
                "langId": LANG_ID, "articleNo": numero, "articleType": tipo
            })
            if not d:
                continue
            arts = _lista(d, "articles", "data", "result", "items")
            for a in arts:
                aid  = a.get("articleId") or a.get("id")
                ref  = _v(a.get("articleNo") or a.get("articleNumber", ""))
                nome = _v(a.get("articleProductName") or a.get("articleName") or
                          a.get("description") or a.get("name", ""))
                marca = _v(a.get("supplierName") or a.get("brandName", ""))
                sid   = a.get("supplierId", 0) or 0
                attrs = _atributos_json(a)

                if not aid or not nome:
                    continue

                self.con.execute(
                    "INSERT OR IGNORE INTO artigos(id, ref, nome, marca, supplier_id, atributos)"
                    " VALUES (?,?,?,?,?,?)",
                    (aid, ref, nome, marca, sid, attrs)
                )
                # OEM pesquisado
                self.con.execute(
                    "INSERT OR IGNORE INTO oems(artigo_id, oem_numero, fabricante)"
                    " VALUES (?,?,?)", (aid, numero, "")
                )
                # OEMs embutidos
                for oem in _oems_do_artigo(a):
                    self.con.execute(
                        "INSERT OR IGNORE INTO oems(artigo_id, oem_numero, fabricante)"
                        " VALUES (?,?,?)",
                        (aid, oem["oem_numero"], oem["fabricante"])
                    )
            if arts:
                break

        # Marca que esse OEM já foi consultado
        self.con.execute(
            "INSERT OR IGNORE INTO oem_carregado(oem_numero, quando) VALUES (?,?)",
            (numero, datetime.now().isoformat())
        )
        self.con.commit()

    # ── Busca por ref interna ────────────────────────────────────
    def busca_ref(self, ref: str) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT id, ref, nome, marca, supplier_id, atributos "
            "FROM artigos WHERE ref=?", (ref.strip(),)
        ).fetchall()

    # ── Veículos compatíveis ─────────────────────────────────────
    def veiculos_do_artigo(self, artigo_id: int) -> list[sqlite3.Row]:
        """Retorna veículos que têm este artigo no banco local."""
        return self.con.execute("""
            SELECT DISTINCT v.id, v.descricao, v.motor, v.ano_ini, v.ano_fim,
                   m.nome as modelo, f.nome as fabricante
            FROM veiculos v
            JOIN artigo_veiculo av ON av.veiculo_id = v.id
            JOIN modelos m ON v.modelo_id = m.id
            JOIN fabricantes f ON m.fab_id = f.id
            WHERE av.artigo_id = ?
            ORDER BY f.nome, m.nome, v.descricao
        """, (artigo_id,)).fetchall()

    def veiculos_compativeis_api(self, ref: str, supplier_id: int) -> list[dict]:
        """Consulta veículos compatíveis direto na API (para complementar o banco local)."""
        d = _get(
            f"articles/get-compatible-cars-by-article-number/type-id/{TYPE_ID}",
            {"articleNo": ref, "supplierId": supplier_id,
             "langId": LANG_ID, "countryFilterId": COUNTRY_ID}
        )
        if not d:
            return []
        vehs = _lista(d, "vehicles", "cars", "data", "result", "items")
        resultado = []
        for v in vehs:
            desc = _nome_veh(v)
            ini, fim = _anos(v)
            resultado.append({
                "id":        v.get("vehicleId") or v.get("carId") or v.get("id"),
                "descricao": desc,
                "ano_ini":   ini,
                "ano_fim":   fim,
            })
        return resultado

    # ── Busca textual no banco ───────────────────────────────────
    def busca_texto(self, texto: str, limite: int = 20) -> list[sqlite3.Row]:
        """Busca artigos por nome/referência (LIKE) no banco local."""
        t = f"%{texto.strip()}%"
        return self.con.execute("""
            SELECT id, ref, nome, marca, supplier_id, atributos
            FROM artigos
            WHERE nome LIKE ? OR ref LIKE ?
            ORDER BY nome
            LIMIT ?
        """, (t, t, limite)).fetchall()

    # ── Categorias ──────────────────────────────────────────────
    def categorias(self) -> list[sqlite3.Row]:
        return self.con.execute(
            "SELECT id, nome FROM categorias ORDER BY nome"
        ).fetchall()

    def categoria_por_nome(self, texto: str) -> Optional[sqlite3.Row]:
        """Busca categoria por nome parcial (case-insensitive)."""
        t = f"%{texto.strip()}%"
        return self.con.execute(
            "SELECT id, nome FROM categorias WHERE nome LIKE ? LIMIT 1", (t,)
        ).fetchone()

    # ── Stats ────────────────────────────────────────────────────
    def stats(self) -> dict:
        return {
            "fabricantes": self.con.execute("SELECT COUNT(*) FROM fabricantes").fetchone()[0],
            "modelos":     self.con.execute("SELECT COUNT(*) FROM modelos").fetchone()[0],
            "veiculos":    self.con.execute("SELECT COUNT(*) FROM veiculos").fetchone()[0],
            "artigos":     self.con.execute("SELECT COUNT(*) FROM artigos").fetchone()[0],
            "oems":        self.con.execute("SELECT COUNT(*) FROM oems").fetchone()[0],
            "categorias":  self.con.execute("SELECT COUNT(*) FROM categorias").fetchone()[0],
        }

    def close(self):
        self.con.close()