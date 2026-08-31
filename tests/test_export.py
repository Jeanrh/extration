"""A view de export — o contrato que os times consomem.

O time filtra a sigla dele e exporta, ou exporta a base inteira. Por isso a
view NAO filtra estado nem tempo: FIXED e finding antigo entram, porque quem
decide o recorte e a consulta, nao o contrato. O unico corte e `deleted_at`,
que e o que o Tenable removeu na origem — nao e backlog de ninguem.

O teste que mais importa aqui e o de duplicacao. Juntar tres tabelas assusta
com razao: se qualquer um dos lados tivesse mais de uma linha por chave, o
export entregaria vulnerabilidade repetida e o time trabalharia duas vezes.
Nao acontece porque os dois lados sao chave primaria — e isso fica provado,
nao afirmado.
"""

from __future__ import annotations

import pytest

from conftest import RAIZ  # noqa: F401  (garante sys.path)

pytestmark = pytest.mark.banco

# Colunas que o time precisa ver no CSV. plugin_id e plugin_output sao
# obrigatorias: sem elas o time nao consegue rastrear a vulnerabilidade de
# volta ao Tenable nem ler a evidencia do scan.
OBRIGATORIAS = {
    "finding_id", "plugin_id", "plugin_name", "plugin_output",
    "product", "state", "severity",
    "asset_name", "asset_fqdn", "asset_ipv4",
    "sigla", "equipe_solucionadora", "tribo", "unidade_negocio",
    "pci", "bia", "criticality_cmdb",
    "cvss3_base_score", "exploitability_ease", "cve",
    "layer", "familia", "arch_type",
    "priority_id", "priority_name", "quadrant", "sla_status", "aging",
    "first_found", "last_found",
}


def _plugin(cur, plugin_id=100):
    cur.execute(
        "INSERT INTO plugin (plugin_id, name, family, cvss3_base_score, "
        "exploitability_ease, cve, raw) VALUES (%s, 'Oracle RCE', 'Databases', "
        "9.8, 'Exploits are available', ARRAY['CVE-2024-1'], '{}'::jsonb) "
        "ON CONFLICT (plugin_id) DO NOTHING",
        (plugin_id,),
    )


def _finding(cur, finding_id, state="OPEN", dias=10, hostname="SRV-01"):
    cur.execute(
        "INSERT INTO finding_current (finding_id, product, state, plugin_id, "
        "plugin_name, asset_hostname, asset_ipv4, output, first_found, last_found, "
        "indexed, natural_key, raw) "
        "VALUES (%s, 'VM', %s, 100, 'Oracle RCE', %s, '10.0.0.7', "
        "'A saida do plugin, com a evidencia do scan', "
        "now() - interval '400 days', now() - (%s * interval '1 day'), now(), "
        "%s, '{}'::jsonb)",
        (finding_id, state, hostname, dias, finding_id),
    )


def _contexto(cur, sigla="ACD"):
    cur.execute(
        "INSERT INTO cmdb_acronym (sigla, pci, bia, criticality, "
        "unidade_negocio, tribo, equipe_solucionadora) VALUES "
        "(%s, 'PCI', 'Nao', 'Alto', 'Transformacao', 'Garagem', 'Plataforma de Deploy')",
        (sigla,),
    )
    cur.execute(
        "INSERT INTO cmdb_server (hostname, ipv4, sigla) "
        "VALUES ('SRV-01', '10.0.0.7', %s)",
        (sigla,),
    )


def _recalcular(conn):
    from risk.executor import recalcular

    return recalcular(conn, engine_version="teste")


# --- o campo novo -----------------------------------------------------------


def test_equipe_solucionadora_vem_do_campo_team_da_sigla(conn):
    """No CMDB a equipe esta em `team` da sigla, nao no cockpit."""
    from risk.contexto.cmdb import sincronizar_cmdb

    class Extrator:
        def extract_acronyms(self, **_):
            return [{
                "acronym": "ACD", "name": "ACD - ARGO CD", "PCI": "PCI",
                "BIA": "Nao", "criticality": "Alto",
                "team": "Plataforma de Deploy", "teamid": "OR-345014",
            }]

        def extract_servers(self, **_):
            return []

        def extract_urls(self, **_):
            return []

        def extract_cockpits(self, **_):
            return [{"key": "OR-345014", "name": "GARAGEM",
                     "tribo": "Garagem", "alianca": "Transformacao"}]

    sincronizar_cmdb(Extrator(), conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT equipe_solucionadora, tribo, unidade_negocio "
            "  FROM cmdb_acronym WHERE sigla = 'ACD'"
        )
        assert cur.fetchone() == {
            "equipe_solucionadora": "Plataforma de Deploy",
            "tribo": "Garagem",
            "unidade_negocio": "Transformacao",
        }


def test_a_equipe_viaja_ate_a_linha_do_finding(conn):
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _finding(cur, "f-1")
        _contexto(cur)

    _recalcular(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT equipe_solucionadora FROM finding_risk WHERE finding_id = 'f-1'"
        )
        assert cur.fetchone()["equipe_solucionadora"] == "Plataforma de Deploy"


# --- a view -----------------------------------------------------------------


def test_a_view_entrega_todas_as_colunas_que_o_time_precisa(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            " WHERE table_name = 'vw_finding_export'"
        )
        colunas = {linha["column_name"] for linha in cur.fetchall()}

    faltando = OBRIGATORIAS - colunas
    assert faltando == set(), f"faltam no export: {sorted(faltando)}"


def test_juntar_tres_tabelas_nao_duplica_finding(conn):
    """O medo legitimo de qualquer JOIN. Nao acontece porque os dois lados
    sao chave primaria — mas isso tem que ficar provado, nao prometido."""
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _contexto(cur)
        for i in range(20):
            _finding(cur, f"f-{i:02d}")

    _recalcular(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM finding_current WHERE deleted_at IS NULL) AS base,"
            "       (SELECT count(*) FROM vw_finding_export)                        AS view_,"
            "       (SELECT count(DISTINCT finding_id) FROM vw_finding_export)      AS unicos"
        )
        linha = cur.fetchone()

    assert linha["view_"] == linha["base"] == 20
    assert linha["unicos"] == 20, "finding repetido no export"


def test_o_export_carrega_plugin_id_e_plugin_output(conn):
    """Os dois campos obrigatorios: sem eles o time nao rastreia a
    vulnerabilidade de volta ao Tenable nem le a evidencia."""
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _finding(cur, "f-1")
        _contexto(cur)

    _recalcular(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT plugin_id, plugin_output, cve, cvss3_base_score "
            "  FROM vw_finding_export WHERE finding_id = 'f-1'"
        )
        linha = cur.fetchone()

    assert linha["plugin_id"] == 100
    assert linha["plugin_output"] == "A saida do plugin, com a evidencia do scan"
    assert linha["cve"] == "CVE-2024-1"


def test_fixed_e_finding_antigo_entram_no_export(conn):
    """E o ponto do motor: a view antiga cortava FIXED e aplicava janela de
    30/7 dias, jogando fora justamente o que o motor recalcula."""
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _contexto(cur)
        _finding(cur, "f-open", state="OPEN", dias=1)
        _finding(cur, "f-fixed", state="FIXED", dias=1)
        _finding(cur, "f-antigo", state="OPEN", dias=900)

    _recalcular(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT finding_id, state FROM vw_finding_export ORDER BY finding_id")
        assert [l["finding_id"] for l in cur.fetchall()] == [
            "f-antigo", "f-fixed", "f-open"
        ]


def test_finding_deletado_fica_de_fora(conn):
    """Deletado e o que o Tenable removeu na origem — nao e backlog de ninguem."""
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _contexto(cur)
        _finding(cur, "f-1")
        _finding(cur, "f-2")
        cur.execute("UPDATE finding_current SET deleted_at = now() WHERE finding_id = 'f-2'")

    _recalcular(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT finding_id FROM vw_finding_export")
        assert [l["finding_id"] for l in cur.fetchall()] == ["f-1"]


def test_finding_sem_veredito_do_motor_ainda_aparece(conn):
    """Se o motor ainda nao rodou, o finding nao pode sumir do export: ele
    aparece com prioridade nula, e a ausencia fica visivel."""
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _contexto(cur)
        _finding(cur, "f-1")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT finding_id, priority_name, sigla FROM vw_finding_export"
        )
        assert cur.fetchone() == {
            "finding_id": "f-1", "priority_name": None, "sigla": None
        }


def test_filtrar_por_sigla_e_o_caso_de_uso_do_time(conn):
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _contexto(cur, sigla="ACD")
        cur.execute(
            "INSERT INTO cmdb_acronym (sigla, criticality, equipe_solucionadora) "
            "VALUES ('XPT', 'Baixo', 'Outro Time')"
        )
        cur.execute(
            "INSERT INTO cmdb_server (hostname, ipv4, sigla) "
            "VALUES ('SRV-99', '10.0.0.99', 'XPT')"
        )
        _finding(cur, "f-acd", hostname="SRV-01")
        _finding(cur, "f-xpt", hostname="SRV-99")

    _recalcular(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT finding_id, equipe_solucionadora "
                    "  FROM vw_finding_export WHERE sigla = 'ACD'")
        assert cur.fetchall() == [
            {"finding_id": "f-acd", "equipe_solucionadora": "Plataforma de Deploy"}
        ]
