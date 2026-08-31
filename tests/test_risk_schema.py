"""Contrato de schema que o motor de risco consome.

O Tenable não é a fonte de verdade de risco desta empresa: a severidade que o
negócio monitora é a que o motor calcula. Estes testes fixam o que o banco
precisa saber guardar para que essa prioridade seja consultável — e auditável
sem reexecutar o motor.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.banco


def _finding(cur, finding_id: str = "risk-1", product: str = "VM") -> None:
    """Linha mínima em finding_current — finding_risk tem FK para ela."""
    cur.execute(
        "INSERT INTO finding_current "
        "(finding_id, product, state, indexed, natural_key, raw) "
        "VALUES (%s, %s, 'OPEN', now(), 'nk', '{}'::jsonb)",
        (finding_id, product),
    )


def test_finding_risk_guarda_o_veredito_completo(conn):
    """A linha de risco explica sozinha por que o finding tem aquela prioridade.

    Sem as oito notas gravadas, responder "por que isto é Muito Alta?" exige
    reexecutar o motor — que a essa altura já pode estar com outros pesos.
    """
    with conn.transaction(), conn.cursor() as cur:
        _finding(cur)
        cur.execute(
            "INSERT INTO finding_risk ("
            "  finding_id, py, px, quadrant, priority_id, priority_name,"
            "  sla_status, aging,"
            "  nota_bia, nota_pci, nota_exposure, nota_arch,"
            "  nota_cvss, nota_threat, nota_exploit, nota_layer,"
            "  sigla, pci, bia, criticality_cmdb, unidade_negocio,"
            "  arch_type, layer, familia,"
            "  engine_version, context_synced_at"
            ") VALUES ("
            "  'risk-1', 325, 340, 'Q16', 1, 'Muito Alta',"
            "  'Fora do Prazo', 412,"
            "  100, 100, 10, 100,"
            "  100, 100, 100, 30,"
            "  'GTEC', 'PCI', 'Alto', 'Crise', 'Varejo',"
            "  'Infra', 'sistema operacional', 'oracle',"
            "  'v1', now()"
            ")"
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT priority_name, quadrant, sla_status, aging, nota_threat,"
            "       sigla, unidade_negocio, familia, engine_version"
            "  FROM finding_risk WHERE finding_id = 'risk-1'"
        )
        assert cur.fetchone() == {
            "priority_name": "Muito Alta",
            "quadrant": "Q16",
            "sla_status": "Fora do Prazo",
            "aging": 412,
            "nota_threat": 100,
            "sigla": "GTEC",
            "unidade_negocio": "Varejo",
            "familia": "oracle",
            "engine_version": "v1",
        }


def test_finding_event_aceita_risk_changed(conn):
    """Mudança de prioridade é evento de primeira classe.

    Reaproveita o particionamento e a retenção que finding_event já tem, em vez
    de uma tabela de histórico paralela."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO finding_event "
            "(finding_id, product, event_type, occurred_at, old_value, new_value) "
            "VALUES ('risk-1', 'VM', 'RISK_CHANGED', now(),"
            "        '{\"priority_name\": \"Média\"}'::jsonb,"
            "        '{\"priority_name\": \"Muito Alta\"}'::jsonb)"
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT new_value ->> 'priority_name' AS prioridade "
            "  FROM finding_event WHERE event_type = 'RISK_CHANGED'"
        )
        assert cur.fetchone() == {"prioridade": "Muito Alta"}


def test_risco_some_com_o_finding(conn):
    """O ON DELETE CASCADE já existia; a expansão não pode tê-lo perdido."""
    with conn.transaction(), conn.cursor() as cur:
        _finding(cur, "risk-2")
        cur.execute(
            "INSERT INTO finding_risk (finding_id, engine_version) "
            "VALUES ('risk-2', 'v1')"
        )

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM finding_current WHERE finding_id = 'risk-2'")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS total FROM finding_risk")
        assert cur.fetchone() == {"total": 0}
