#!/usr/bin/env python3
"""
extrator.py – Extração em lote do AutoFlex OEM
===============================================
Popula o banco SQLite com dados de fabricantes brasileiros.
Pode ser rodado em partes (por fabricante) e retomado de onde parou.

Uso:
    python extrator.py                    # extrai tudo (pode demorar horas)
    python extrator.py --fab FIAT         # só Fiat
    python extrator.py --fab FIAT RENAULT # só Fiat e Renault
    python extrator.py --stats            # mostra estado do banco
    python extrator.py --teste            # testa 1 modelo de cada fab
    python extrator.py --limite-modelos 5 # máx 5 modelos por fabricante
    python extrator.py --limite-veiculos 10# máx 10 veículos por modelo
    python extrator.py --delay 1.0        # delay entre requests (s)

O extrator pula automaticamente o que já foi carregado (idempotente).
"""

import argparse, sys, time
from pathlib import Path
from datetime import datetime

# Garante que o db.py seja encontrado
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import DB, _get, _lista, _nome_veh, _anos, _v

# ── Categorias prioritárias para extração em lote ─────────────
# Buscar TODAS as categorias seria inviável (~800 × N veículos)
# Estas são as mais buscadas no mercado BR
CATS_PRIORITARIAS = [
    # IDs reais da TecDoc — confirmados nos endpoints desta API
    # (use python extrator.py --listar-cats para ver todos disponíveis no banco)
    # Motor
    "Oil Filter", "Air Filter", "Fuel Filter", "Spark Plug", "Ignition Coil",
    "Timing Belt", "Timing Belt Kit", "Belt Tensioner", "Water Pump",
    "Thermostat", "Radiator", "Engine Oil", "Valve Cover Gasket",
    # Freios
    "Brake Pad", "Brake Disc", "Brake Drum", "Brake Shoe",
    "Brake Caliper", "Brake Master Cylinder", "Brake Hose", "ABS Sensor",
    # Suspensão/Direção
    "Shock Absorber", "Suspension Strut", "Ball Joint", "Control Arm",
    "Tie Rod", "Wheel Bearing", "Hub", "Stabilizer Link",
    "Power Steering Pump",
    # Transmissão/Embreagem
    "Clutch Kit", "Clutch Disc", "Clutch Pressure Plate", "Clutch Cable",
    "CV Joint", "Drive Shaft",
    # Elétrica/Sensores
    "Alternator", "Starter Motor", "Oxygen Sensor",
    "Crankshaft Position Sensor", "Camshaft Position Sensor",
    "Mass Air Flow Sensor", "Throttle Body",
    # Combustível/Arrefecimento
    "Fuel Pump", "Fuel Injector", "Coolant Hose",
    "EGR Valve", "Catalytic Converter",
]

def _barra(atual, total, largura=30):
    p   = atual / max(total, 1)
    bl  = int(p * largura)
    bar = "█" * bl + "░" * (largura - bl)
    return f"[{bar}] {atual}/{total} ({p:.0%})"


class Extrator:
    def __init__(self, db: DB, delay: float = 0.5):
        self.db    = db
        self.delay = delay  # segundos entre requests à API

    def _sleep(self):
        time.sleep(self.delay)

    # ── Extração principal ─────────────────────────────────────
    def extrair(self,
                fabricantes_filtro: list[str] | None = None,
                limite_modelos:     int | None = None,
                limite_veiculos:    int | None = None,
                so_prioritarias:    bool = True,
                modo_teste:         bool = False):

        if modo_teste:
            limite_modelos  = 1
            limite_veiculos = 2
            so_prioritarias = True
            print("🧪 Modo teste: 1 modelo × 2 veículos por fabricante\n")

        fabs = self.db.fabricantes()
        if fabricantes_filtro:
            filtro_upper = [f.upper() for f in fabricantes_filtro]
            fabs = [f for f in fabs if f["nome"].upper() in filtro_upper]

        if not fabs:
            print("❌ Nenhum fabricante encontrado. Rode db.boot() primeiro.")
            return

        # Resolve categorias prioritárias
        cats_alvo = self._resolver_cats(so_prioritarias)
        if not cats_alvo:
            print("❌ Nenhuma categoria encontrada no banco.")
            return

        print(f"📦 Fabricantes: {len(fabs)}")
        print(f"📂 Categorias alvo: {len(cats_alvo)}")
        print(f"⏱  Delay: {self.delay}s por request")
        print(f"{'─'*60}\n")

        inicio = time.time()
        total_artigos = 0

        for fab in fabs:
            fab_id   = fab["id"]
            fab_nome = fab["nome"]
            print(f"\n🏭 {fab_nome}")

            modelos = self._carregar_modelos(fab_id)
            if limite_modelos:
                modelos = modelos[:limite_modelos]

            for i_mod, mod in enumerate(modelos, 1):
                mod_id   = mod["id"]
                mod_nome = mod["nome"]
                print(f"  📋 [{i_mod}/{len(modelos)}] {mod_nome}")

                veiculos = self._carregar_veiculos(mod_id)
                if limite_veiculos:
                    veiculos = veiculos[:limite_veiculos]

                for i_veh, veh in enumerate(veiculos, 1):
                    veh_id   = veh["id"]
                    veh_desc = veh["descricao"] or f"ID {veh_id}"

                    # Pula se já foi totalmente carregado
                    if veh["carregado"]:
                        print(f"    ✅ {veh_desc} (já carregado)")
                        continue

                    n_arts = 0
                    for cat_id, cat_nome in cats_alvo:
                        arts = self._carregar_artigos_cat(veh_id, cat_id)
                        n_arts += arts
                        if arts:
                            print(f"    🔧 {veh_desc} | {cat_nome}: {arts} peças")
                        self._sleep()

                    # Marca veículo como carregado
                    self.db.con.execute(
                        "UPDATE veiculos SET carregado=1 WHERE id=?", (veh_id,))
                    self.db.con.commit()
                    total_artigos += n_arts

                    pct = _barra(i_veh, len(veiculos))
                    print(f"    {pct} | +{n_arts} artigos")

        elapsed = time.time() - inicio
        print(f"\n{'='*60}")
        print(f"✅ Extração concluída em {elapsed/60:.1f} min")
        print(f"   Artigos salvos nesta sessão: {total_artigos:,}")
        self._print_stats()

    # ── Helpers internos ───────────────────────────────────────
    def _resolver_cats(self, so_prioritarias: bool) -> list[tuple[int, str]]:
        """Retorna lista de (id, nome) das categorias a extrair."""
        todas = self.db.categorias()
        if not so_prioritarias:
            return [(c["id"], c["nome"]) for c in todas]

        resultado = []
        nomes_prio = [n.lower() for n in CATS_PRIORITARIAS]
        for cat in todas:
            nome_norm = cat["nome"].lower()
            if any(p in nome_norm for p in nomes_prio):
                resultado.append((cat["id"], cat["nome"]))
        return resultado

    def _carregar_modelos(self, fab_id: int) -> list:
        """Carrega modelos (lazy via DB) e retorna como lista de dicts."""
        rows = self.db.modelos_do_fabricante(fab_id)
        self._sleep()
        return [dict(r) for r in rows]

    def _carregar_veiculos(self, mod_id: int) -> list:
        rows = self.db.veiculos_do_modelo(mod_id)
        self._sleep()
        return [dict(r) for r in rows]

    def _carregar_artigos_cat(self, veh_id: int, cat_id: int) -> int:
        """Carrega artigos de um veículo+categoria. Retorna qtd salva."""
        antes = self.db.con.execute(
            "SELECT COUNT(*) FROM artigo_veiculo WHERE veiculo_id=? AND cat_id=?",
            (veh_id, cat_id)
        ).fetchone()[0]
        if antes > 0:
            return 0  # já carregado

        self.db.artigos(veh_id, cat_id)  # faz o lazy-load

        depois = self.db.con.execute(
            "SELECT COUNT(*) FROM artigo_veiculo WHERE veiculo_id=? AND cat_id=?",
            (veh_id, cat_id)
        ).fetchone()[0]
        return depois - antes

    def _print_stats(self):
        s = self.db.stats()
        print(f"\n📊 Estado do banco ({self.db.path.name}):")
        for k, v in s.items():
            print(f"   {k:<15}: {v:>8,}")


# ── Extração de OEMs em lote ───────────────────────────────────
def extrair_oems_de_artigos(db: DB, delay: float = 0.5):
    """
    Para artigos que já estão no banco mas sem OEMs,
    tenta buscar OEMs pelo número de referência.
    Útil para rodar depois da extração principal.
    """
    arts_sem_oem = db.con.execute("""
        SELECT a.id, a.ref, a.nome
        FROM artigos a
        WHERE a.ref != ''
          AND NOT EXISTS (SELECT 1 FROM oems o WHERE o.artigo_id = a.id)
        ORDER BY a.id
    """).fetchall()

    print(f"Artigos sem OEM: {len(arts_sem_oem)}")
    for i, art in enumerate(arts_sem_oem, 1):
        if i % 50 == 0:
            print(f"  {_barra(i, len(arts_sem_oem))}")
        db.busca_oem(art["ref"])  # tenta buscar e salva
        time.sleep(delay)

    print(f"\n✅ OEMs processados: {len(arts_sem_oem)}")


# ── CLI ────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Extrator em lote – AutoFlex OEM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    ap.add_argument("--fab",            nargs="+", metavar="NOME",
                    help="Fabricante(s) a extrair (ex: FIAT RENAULT)")
    ap.add_argument("--limite-modelos", type=int,  metavar="N",
                    help="Máx modelos por fabricante")
    ap.add_argument("--limite-veiculos",type=int,  metavar="N",
                    help="Máx veículos por modelo")
    ap.add_argument("--todas-cats",     action="store_true",
                    help="Extrai TODAS as categorias (lento, não recomendado)")
    ap.add_argument("--delay",          type=float, default=0.5,
                    help="Delay entre requests em segundos (padrão: 0.5)")
    ap.add_argument("--stats",          action="store_true",
                    help="Mostra estado atual do banco e sai")
    ap.add_argument("--teste",          action="store_true",
                    help="Modo teste: 1 modelo × 2 veículos por fabricante")
    ap.add_argument("--listar-cats",    action="store_true",
                    help="Lista categorias disponíveis no banco")
    ap.add_argument("--oems",           action="store_true",
                    help="Busca OEMs para artigos que ainda não têm")
    ap.add_argument("--db",            default=None, metavar="CAMINHO",
                    help="Caminho para o arquivo .db (padrão: autoflex_oem.db)")
    args = ap.parse_args()

    from db import DB_PATH
    db_path = args.db or DB_PATH
    db = DB(db_path)

    print(f"🗄  Banco: {db_path}")
    print(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # Boot sempre (rápido se já populado)
    print("🔄 Boot…")
    db.boot()

    if args.stats:
        ext = Extrator(db)
        ext._print_stats()
        db.close()
        return

    if args.listar_cats:
        cats = db.categorias()
        print(f"\n{len(cats)} categorias:\n")
        for c in cats:
            print(f"  {c['id']:>6}  {c['nome']}")
        db.close()
        return

    if args.oems:
        extrair_oems_de_artigos(db, delay=args.delay)
        db.close()
        return

    ext = Extrator(db, delay=args.delay)
    ext.extrair(
        fabricantes_filtro = args.fab,
        limite_modelos     = args.limite_modelos,
        limite_veiculos    = args.limite_veiculos,
        so_prioritarias    = not args.todas_cats,
        modo_teste         = args.teste,
    )
    db.close()


if __name__ == "__main__":
    main()