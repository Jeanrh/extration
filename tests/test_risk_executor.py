"""O recálculo de ponta a ponta.

Sem filtro de tempo: todo finding não deletado entra, em qualquer estado. É o
que o CSV nunca conseguiu — lá, o export só devolve a janela de 30 dias (VM) e
7 dias (WAS), e o que ficou fora carrega para sempre o score da última execução
que o tocou, mesmo depois de a regra mudar.
"""

from __future__ import annotations

import pytest

from conftest import RAIZ  # noqa: F401  (garante sys.path)

pytestmark = pytest.mark.banco


def _plugin(cur, plugin_id=100, nome="Oracle Database 19c", family="Databases",
            cvss="9.5", ease="Exploits are available"):
    cur.execute(
        "INSERT INTO plugin (plugin_id, name, family, cvss3_base_score, "
        "exploitability_ease, raw) VALUES (%s, %s, %s, %s, %s, '{}'::jsonb) "
        "ON CONFLICT (plugin_id) DO NOTHING",
        (plugin_id, nome, family, cvss, ease),
    )


def _finding(cur, finding_id, product="VM", state="OPEN", hostname="SRV-APP-01",
             fqdn=None, url=None, plugin_id=100):
    cur.execute(
        "INSERT INTO finding_current (finding_id, product, state, plugin_id, "
        "asset_hostname, asset_fqdn, url, first_found, indexed, natural_key, raw) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, now() - interval '400 days', now(), "
        "'nk', '{}'::jsonb)",
        (finding_id, product, state, plugin_id, hostname, fqdn, url),
    )


def _contexto(cur, sigla="GTEC", criticality="Crise", pci="PCI", arquitetura="Infra"):
    cur.execute(
        "INSERT INTO cmdb_acronym (sigla, pci, bia, criticality, "
        "unidade_negocio, tribo) "
        "VALUES (%s, %s, 'Alto', %s, 'Varejo', 'Plataformas')",
        (sigla, pci, criticality),
    )
    cur.execute(
        "INSERT INTO cmdb_server (hostname, ipv4, sigla) VALUES ('SRV-APP-01', '10.0.0.7', %s)",
        (sigla,),
    )
    cur.execute("INSERT INTO architecture (sigla, arquitetura) VALUES (%s, %s)",
                (sigla, arquitetura))
    cur.execute("INSERT INTO plugin_layer (plugin_id, layer, familia, resolved_by) "
                "VALUES (100, 'banco de dados', 'oracle', 'plugin_name')")


def _recalcular(conn, **kwargs):
    from risk.executor import recalcular

    return recalcular(conn, engine_version="teste", **kwargs)


def test_fixed_antigo_tambem_ganha_prioridade(conn):
    """O ponto do projeto: no CSV de hoje um finding corrigido há um ano some
    da janela e nunca mais é recalculado."""
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _finding(cur, "f-open", state="OPEN")
        _finding(cur, "f-fixed", state="FIXED")
        _contexto(cur)

    assert _recalcular(conn).calculados == 2

    with conn.cursor() as cur:
        cur.execute("SELECT finding_id, priority_name FROM finding_risk ORDER BY finding_id")
        # py = 100(BIA) + 100(PCI) + 10(VM) + 75(Infra x1.5) = 285 -> banda 2
        # px = 100(CVSS) + 11(sem ameaca) + 110(exploit) + 40(BD) = 261 -> banda 2
        assert cur.fetchall() == [
            {"finding_id": "f-fixed", "priority_name": "Alta"},
            {"finding_id": "f-open", "priority_name": "Alta"},
        ]


def test_finding_deletado_fica_de_fora(conn):
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _finding(cur, "f-1")
        cur.execute("UPDATE finding_current SET deleted_at = now()")
        _contexto(cur)

    assert _recalcular(conn).calculados == 0


def test_o_contexto_do_cmdb_chega_gravado_na_linha(conn):
    """A linha explica sozinha a prioridade — inclusive de onde veio o py."""
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _finding(cur, "f-1")
        _contexto(cur)

    _recalcular(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT sigla, criticality_cmdb, unidade_negocio, arch_type, layer, "
            "       familia, nota_bia, nota_arch, nota_layer, sla_status "
            "  FROM finding_risk WHERE finding_id = 'f-1'"
        )
        assert cur.fetchone() == {
            "sigla": "GTEC",
            "criticality_cmdb": "Crise",
            "unidade_negocio": "Varejo",   # aliança, não VP
            "arch_type": "Infra",
            "layer": "banco de dados",
            "familia": "oracle",
            "nota_bia": 100,
            "nota_arch": 50,
            "nota_layer": 50,
            "sla_status": "Fora do Prazo",  # 400 dias no Q13 (prazo 90)
        }


def test_sem_contexto_o_finding_cai_nos_defaults_e_a_sigla_fica_nula(conn):
    """Não inventar sigla é deliberado: um chute vira BIA e PCI errados. A
    ausência tem que ficar visível na query."""
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _finding(cur, "f-1", hostname="HOST-DESCONHECIDO")

    _recalcular(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT sigla, nota_bia, nota_pci, nota_arch FROM finding_risk"
        )
        assert cur.fetchone() == {
            "sigla": "",
            "nota_bia": 50,   # default
            "nota_pci": 10,
            "nota_arch": 40,  # sigla sem cadastro
        }


def test_threat_intel_muda_a_nota_de_ameaca(conn):
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _finding(cur, "f-1")
        _contexto(cur)
        cur.execute("INSERT INTO threat_intel (finding_id) VALUES ('f-1')")

    _recalcular(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT nota_threat FROM finding_risk")
        assert cur.fetchone() == {"nota_threat": 100}


def test_segunda_execucao_nao_reescreve_nem_gera_evento(conn):
    """Idempotência é o que protege a tabela de 500 mil tuplas mortas por dia."""
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _finding(cur, "f-1")
        _contexto(cur)

    primeira = _recalcular(conn)
    segunda = _recalcular(conn)

    assert primeira.gravados == 1
    assert segunda.calculados == 1, "recalcula tudo"
    assert segunda.gravados == 0, "mas não escreve o que não mudou"
    assert segunda.eventos == 0

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS total FROM finding_event")
        assert cur.fetchone() == {"total": 0}


def test_mudanca_de_prioridade_vira_evento(conn):
    """A severidade que o negócio monitora é a do motor. Quando ela muda, tem
    que sobrar registro."""
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _finding(cur, "f-1")
        _contexto(cur)

    _recalcular(conn)

    # Rebaixa o ativo no CMDB: py cai e o finding muda de quadrante.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("UPDATE cmdb_acronym SET criticality = 'Baixo', pci = 'Nao'")
        cur.execute("UPDATE architecture SET arquitetura = 'Mainframe'")

    resultado = _recalcular(conn)
    assert resultado.eventos == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT old_value ->> 'priority_name' AS antes, "
            "       new_value ->> 'priority_name' AS depois "
            "  FROM finding_event WHERE event_type = 'RISK_CHANGED'"
        )
        # py despenca de 285 (banda 2) para 45 (banda 0); px segue 261.
        assert cur.fetchone() == {"antes": "Alta", "depois": "Média"}


def test_was_resolve_sigla_pelo_fqdn_e_usa_a_url_na_arquitetura(conn):
    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur)
        _finding(
            cur, "w-1", product="WAS", hostname=None,
            fqdn="api.exemplo.com", url="https://api.exemplo.com/v1",
        )
        cur.execute("INSERT INTO cmdb_acronym (sigla, pci, criticality) "
                    "VALUES ('LOJA', 'PCI', 'Alto')")
        cur.execute("INSERT INTO cmdb_url (url, sigla) "
                    "VALUES ('API.EXEMPLO.COM', 'LOJA')")

    _recalcular(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT sigla, arch_type, nota_exposure, nota_layer FROM finding_risk")
        assert cur.fetchone() == {
            "sigla": "LOJA",
            "arch_type": "API",       # "api" na URL
            "nota_exposure": 100,     # WAS é internet
            "nota_layer": 100,        # WAS é sempre camada de aplicação
        }
