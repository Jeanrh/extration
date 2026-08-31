"""Bateria de ponta a ponta com massa sintética.

Cria o schema do zero, gera massa coerente, roda o motor e mede. O alvo é o
**motor**, não a ingestão: os findings entram direto em `finding_current`,
que é onde o pipeline do S3 os deixaria. Simular o S3 é outro exercício.

O CMDB entra pelo caminho de produção — `sincronizar_cmdb` com um extrator
falso —, então a simulação exercita a resolução de sigla e o casamento com o
cockpit de verdade. Enfiar linha direto em `cmdb_acronym` pularia justamente a
parte que erra.

    python -m scripts.simular --dsn "postgresql://..." --findings 50000

Nada aqui é importado pelo motor em produção: é ferramenta de mesa.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

PCI = ("PCI", "Nao", "Escopo Estendido", "Nao")
BIA = ("Sim", "Nao")
CRITICIDADE = ("Crise", "Alto", "Medio", "Baixo")
ARQUITETURAS = ("App/Web", "API", "Mobile", "Infra", "Workflow", "Mainframe", "")
FAMILIAS = (
    "Databases", "Web Servers", "Windows", "Red Hat Local Security Checks",
    "Firewalls", "Misc.", "CGI abuses", "General",
)
EASES = ("Exploits are available", "No known exploits are available", None, None)
ESTADOS = ("OPEN", "OPEN", "OPEN", "REOPENED", "FIXED")
TRIBOS = ("Garagem", "Pagamentos", "Canais", "Dados", "Plataformas")
ALIANCAS = ("Transformação e Governança", "Negócios", "Tecnologia")


@dataclass(frozen=True)
class Massa:
    siglas: list[dict]
    servidores: list[dict]
    urls: list[dict]
    cockpits: list[dict]
    plugins: list[dict]
    findings: list[dict]
    arquiteturas: list[tuple[str, str]]
    intel: list[str]


def gerar_massa(findings: int = 50_000, siglas: int = 200, semente: int = 1234) -> Massa:
    """Massa coerente: todo finding chega a uma sigla por uma cadeia real.

    Determinística pela semente — duas execuções comparáveis entre si."""
    rnd = random.Random(semente)

    cockpits = [
        {
            "id": str(900_000 + i), "key": f"OR-{900_000 + i}",
            "name": TRIBOS[i % len(TRIBOS)].upper(), "status": "Active",
            "tribo": TRIBOS[i % len(TRIBOS)],
            "alianca": ALIANCAS[i % len(ALIANCAS)],
            "vp": "Tecnologia e Negócios", "squad": "", "squadid": "",
            "team": "", "teamid": "", "created": "", "updated": "",
        }
        for i in range(max(1, siglas // 10))
    ]

    lista_siglas = []
    for i in range(siglas):
        codigo = f"SIG{i:03d}"
        lista_siglas.append({
            "id": str(500_000 + i), "key": f"CMDB-{500_000 + i}",
            "acronym": codigo, "name": f"{codigo} - Sistema {i:03d}",
            "status": "Operacional", "domain": "TI", "subdomain": "Infra",
            "BIA": BIA[i % len(BIA)], "PCI": PCI[i % len(PCI)],
            "criticality": CRITICIDADE[i % len(CRITICIDADE)],
            "created": "", "updated": "", "infrastructure": "Cloud AWS",
            "service": "Aplicacao", "squad": "", "squadid": "",
            "team": cockpits[i % len(cockpits)]["name"].title(),
            "teamid": cockpits[i % len(cockpits)]["key"],
        })

    # O CMDB guarda o *nome de exibição* no servidor/url, não o código: é o que
    # obriga `resolver_sigla` a trabalhar, como em produção.
    servidores, urls = [], []
    for i, s in enumerate(lista_siglas):
        for j in range(3):
            servidores.append({
                "id": str(400_000 + i * 3 + j),
                "objectKey": f"CMDB-{400_000 + i * 3 + j}",
                "name": f"SRV-{i:03d}-{j:02d}", "status": "Operacional",
                "ipv4": f"10.{i % 256}.{j}.{(i * 7 + j) % 254 + 1}",
                "environment": "Produção", "acronym": s["name"],
                "os": "Linux", "accountname": "", "tenableid": "",
                "infrastructure": "OnPremise", "criticality": "",
                "platform": "", "cluster": "", "layer": "",
                "created": "", "updated": "",
            })
        urls.append({
            "id": str(600_000 + i), "objectKey": f"CMDB-{600_000 + i}",
            "name": f"app{i:03d}.exemplo.com.br", "status": "Operacional",
            "environment": "", "domain": "", "squad": "", "squadid": "",
            "acronym": s["name"], "acronymid": s["key"], "pci": "",
            "alliance": "", "allianceid": "", "created": "", "updated": "",
        })

    plugins = [
        {
            "plugin_id": 10_000 + i,
            "name": f"Plugin sintetico {i}",
            "family": FAMILIAS[i % len(FAMILIAS)],
            # sem CVSS em parte das linhas: é a DIVERGENCIA 2 em dado vivo
            "cvss3_base_score": None if i % 11 == 0 else round(rnd.uniform(1.0, 10.0), 1),
            "exploitability_ease": EASES[i % len(EASES)],
        }
        for i in range(2_000)
    ]

    agora = dt.datetime.now(dt.timezone.utc)
    lista_findings = []
    for i in range(findings):
        was = i % 4 == 0
        indice = i % len(lista_siglas)
        plugin = plugins[i % len(plugins)]
        if was:
            url = urls[indice]
            alvo = {"asset_fqdn": url["name"], "url": f"https://{url['name']}/p{i}",
                    "asset_hostname": None, "asset_ipv4": None}
        else:
            servidor = servidores[(i * 3) % len(servidores)]
            alvo = {"asset_hostname": servidor["name"], "asset_ipv4": servidor["ipv4"],
                    "asset_fqdn": None, "url": None}
        lista_findings.append({
            "finding_id": f"f-{i:08d}",
            "product": "WAS" if was else "VM",
            "state": ESTADOS[i % len(ESTADOS)],
            "plugin_id": plugin["plugin_id"],
            "first_found": agora - dt.timedelta(days=rnd.randint(1, 730)),
            **alvo,
        })

    arquiteturas = [
        (s["acronym"], ARQUITETURAS[i % len(ARQUITETURAS)])
        for i, s in enumerate(lista_siglas)
    ]
    # ~8% em ameaça ativa, a ordem de grandeza que o export clássico devolve
    intel = [f["finding_id"] for f in lista_findings if rnd.random() < 0.08]

    return Massa(lista_siglas, servidores, urls, cockpits, plugins,
                 lista_findings, arquiteturas, intel)


# ---------------------------------------------------------------------------
# Carga
# ---------------------------------------------------------------------------


class _ExtratorFalso:
    """Contrato do CMDBExtractor, servindo massa em vez de HTTP."""

    def __init__(self, massa: Massa) -> None:
        self._m = massa

    # **_ em vez de `max_age_hours=None`: o sync passa o argumento por nome e
    # aqui ele nao tem uso — a massa ja esta em memoria.
    def extract_acronyms(self, **_):
        return self._m.siglas

    def extract_servers(self, **_):
        return self._m.servidores

    def extract_urls(self, **_):
        return self._m.urls

    def extract_cockpits(self, **_):
        return self._m.cockpits


class _ExtratorIntelFalso:
    def __init__(self, ids: list[str]) -> None:
        self._ids = ids

    def extract_threat_intel(self):
        return [{"finding_id": i} for i in self._ids]


def _carregar_plugins_e_findings(conn, massa: Massa) -> None:
    """COPY em vez de INSERT: 50 mil linhas por statement não escala."""
    with conn.transaction(), conn.cursor() as cur:
        with cur.copy(
            "COPY plugin (plugin_id, name, family, cvss3_base_score, "
            "exploitability_ease, raw) FROM STDIN"
        ) as copy:
            for p in massa.plugins:
                copy.write_row((p["plugin_id"], p["name"], p["family"],
                                p["cvss3_base_score"], p["exploitability_ease"], "{}"))

        with cur.copy(
            "COPY finding_current (finding_id, product, state, plugin_id, "
            "asset_hostname, asset_ipv4, asset_fqdn, url, first_found, "
            "indexed, natural_key, raw) FROM STDIN"
        ) as copy:
            agora = dt.datetime.now(dt.timezone.utc)
            for f in massa.findings:
                copy.write_row((
                    f["finding_id"], f["product"], f["state"], f["plugin_id"],
                    f["asset_hostname"], f["asset_ipv4"], f["asset_fqdn"], f["url"],
                    f["first_found"], agora, f["finding_id"], "{}",
                ))


def _carregar_arquitetura(conn, massa: Massa) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("TRUNCATE architecture")
        cur.executemany(
            "INSERT INTO architecture (sigla, arquitetura) VALUES (%s, %s)",
            massa.arquiteturas,
        )


# ---------------------------------------------------------------------------
# Relatório
# ---------------------------------------------------------------------------


def _consultar(conn, sql: str):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def _secao(titulo: str) -> None:
    print(f"\n\033[1m{titulo}\033[0m" if sys.stdout.isatty() else f"\n{titulo}")
    print("-" * len(titulo))


def executar(dsn: str, findings: int, siglas: int, semente: int) -> int:
    from ingestion.db import aplicar_migracoes, conectar
    from risk.contexto.cmdb import sincronizar_cmdb
    from risk.contexto.intel import sincronizar_threat_intel
    from risk.derivacoes.camada import derivar_camadas
    from risk.executor import recalcular

    massa = gerar_massa(findings=findings, siglas=siglas, semente=semente)

    with conectar(dsn) as conn:
        _secao("1. schema")
        marca = time.perf_counter()
        aplicar_migracoes(conn)
        versoes = _consultar(conn, "SELECT version FROM schema_migration ORDER BY version")
        print(f"   {len(versoes)} migrações aplicadas em {time.perf_counter() - marca:.2f}s")
        print(f"   última: {versoes[-1]['version']}")

        _secao("2. contexto externo")
        marca = time.perf_counter()
        r = sincronizar_cmdb(_ExtratorFalso(massa), conn)
        _carregar_arquitetura(conn, massa)
        n_intel = sincronizar_threat_intel(_ExtratorIntelFalso(massa.intel), conn)
        print(f"   siglas={r.siglas} servidores={r.servidores} urls={r.urls} "
              f"cockpits={r.times}")
        print(f"   arquitetura={len(massa.arquiteturas)} threat_intel={n_intel}")
        print(f"   {time.perf_counter() - marca:.2f}s")

        resolvidas = _consultar(conn, "SELECT count(*) AS n FROM cmdb_server WHERE sigla <> ''")
        com_tribo = _consultar(conn, "SELECT count(*) AS n FROM cmdb_acronym WHERE tribo <> ''")
        print(f"   servidores com sigla resolvida: {resolvidas[0]['n']}/{r.servidores}")
        print(f"   siglas com tribo resolvida:     {com_tribo[0]['n']}/{r.siglas}")

        _secao("3. massa do Tenable")
        marca = time.perf_counter()
        _carregar_plugins_e_findings(conn, massa)
        print(f"   {len(massa.plugins)} plugins + {len(massa.findings)} findings "
              f"em {time.perf_counter() - marca:.2f}s")

        _secao("4. camada por plugin")
        marca = time.perf_counter()
        # Sem Vault: cai no fallback por plugin.family, o caminho degradado
        n = derivar_camadas(conn, {})
        print(f"   {n} plugins com camada derivada em {time.perf_counter() - marca:.2f}s")
        for linha in _consultar(conn, "SELECT resolved_by, count(*) AS n FROM plugin_layer "
                                      "GROUP BY resolved_by ORDER BY n DESC"):
            print(f"     {linha['resolved_by']:12} {linha['n']}")

        _secao("5. recálculo")
        marca = time.perf_counter()
        primeira = recalcular(conn, engine_version="simulacao")
        t1 = time.perf_counter() - marca
        print(f"   calculados={primeira.calculados} gravados={primeira.gravados} "
              f"eventos={primeira.eventos}")
        print(f"   {t1:.2f}s  ({primeira.calculados / t1:,.0f} findings/s)")

        _secao("6. idempotência")
        marca = time.perf_counter()
        segunda = recalcular(conn, engine_version="simulacao")
        t2 = time.perf_counter() - marca
        ok = segunda.gravados == 0 and segunda.eventos == 0
        print(f"   calculados={segunda.calculados} gravados={segunda.gravados} "
              f"eventos={segunda.eventos}  ({t2:.2f}s)")
        print(f"   {'OK — nada reescrito' if ok else 'FALHOU — reescreveu sem mudança'}")

        _secao("7. distribuição")
        for linha in _consultar(conn, "SELECT priority_name, count(*) AS n FROM finding_risk "
                                      "GROUP BY priority_name ORDER BY n DESC"):
            print(f"   {linha['priority_name']:12} {linha['n']:>8}")
        for linha in _consultar(conn, "SELECT COALESCE(NULLIF(sla_status,''),'(sem data)') AS s, "
                                      "count(*) AS n FROM finding_risk GROUP BY s ORDER BY n DESC"):
            print(f"   {linha['s']:16} {linha['n']:>8}")

        _secao("8. o que a paridade vai querer saber")
        for rotulo, sql in [
            ("findings sem sigla", "SELECT count(*) AS n FROM finding_risk WHERE COALESCE(sigla,'') = ''"),
            ("sem unidade de negócio", "SELECT count(*) AS n FROM finding_risk WHERE COALESCE(unidade_negocio,'') = ''"),
            ("sem tribo", "SELECT count(*) AS n FROM finding_risk WHERE COALESCE(tribo,'') = ''"),
            ("com exploitability_ease", "SELECT count(*) AS n FROM finding_current fc "
                                        "JOIN plugin p USING (plugin_id) "
                                        "WHERE COALESCE(p.exploitability_ease,'') <> ''"),
            ("nota_exploit = 100", "SELECT count(*) AS n FROM finding_risk WHERE nota_exploit = 100"),
            # Separados de proposito: nota_cvss=10 cobre "sem CVSS3" E "CVSS<4".
            # Somar os dois esconderia o tamanho real da DIVERGENCIA 2.
            ("sem CVSS3 no plugin", "SELECT count(*) AS n FROM finding_current fc "
                                    "JOIN plugin p USING (plugin_id) "
                                    "WHERE p.cvss3_base_score IS NULL"),
            ("nota_cvss = 10 (inclui CVSS<4)", "SELECT count(*) AS n FROM finding_risk WHERE nota_cvss = 10"),
            ("em ameaça ativa", "SELECT count(*) AS n FROM finding_risk WHERE nota_threat = 100"),
        ]:
            print(f"   {rotulo:28} {_consultar(conn, sql)[0]['n']:>8}")

        return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Bateria de ponta a ponta com massa sintética")
    p.add_argument("--dsn", required=True)
    p.add_argument("--findings", type=int, default=50_000)
    p.add_argument("--siglas", type=int, default=200)
    p.add_argument("--semente", type=int, default=1234)
    args = p.parse_args(argv)
    return executar(args.dsn, args.findings, args.siglas, args.semente)


if __name__ == "__main__":
    raise SystemExit(main())
