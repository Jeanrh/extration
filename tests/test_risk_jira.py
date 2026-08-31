"""Sincronização dos tickets do Jira Service Desk com os findings.

O vínculo é frágil por natureza e vale entender antes de ler os testes: o Jira
não sabe que o finding existe. A chave do card (`GVUL-123`) não tem relação com
o `finding_id` (UUID v5 do Tenable) — o que liga os dois é a string
`Finding ID: <uuid>` escrita **dentro do corpo da descrição**, que vem em ADF
(árvore de nós JSON, não texto).

Se alguém editar a descrição, mudar o template da automação ou trocar o rótulo
para `Finding-ID`, o regex para de casar e o vínculo some **sem erro nenhum**.
Por isso o sync registra quantos cards da fila ficaram sem finding_id: é o
sinal de saúde que transforma um sumiço silencioso em número visível.

Nenhum teste toca a rede — a sessão HTTP é injetada.
"""

from __future__ import annotations

import pytest

from conftest import RAIZ  # noqa: F401  (garante sys.path)

from risk.contexto.jira import (
    ClienteJira,
    ExtratorJira,
    extrair_finding_id,
    texto_adf,
)

UUID_A = "e1f2a3b4-c5d6-4789-a012-3456789abcde"
UUID_B = "aaaabbbb-cccc-4ddd-8eee-ffff00001111"


# ---------------------------------------------------------------------------
# ADF e regex — o elo frágil
# ---------------------------------------------------------------------------


def _adf(*textos: str) -> dict:
    """Descrição no formato do Atlassian: parágrafos de nós de texto."""
    return {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": t}]}
            for t in textos
        ],
    }


def test_texto_adf_achata_a_arvore_inteira():
    assert texto_adf(_adf("Servidor comprometido.", "Prioridade alta.")) == (
        "Servidor comprometido.Prioridade alta."
    )


def test_texto_adf_desce_em_nos_aninhados():
    """A descrição real tem listas, negrito e links — o texto vive em folhas."""
    doc = {"type": "doc", "content": [
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "Finding ID: "},
                    {"type": "text", "text": UUID_A, "marks": [{"type": "strong"}]},
                ]},
            ]},
        ]},
    ]}
    assert extrair_finding_id(doc) == UUID_A


def test_extrai_o_finding_id_da_descricao():
    assert extrair_finding_id(_adf(f"Finding ID: {UUID_A}")) == UUID_A


def test_aceita_variacao_de_espaco_e_caixa():
    assert extrair_finding_id(_adf(f"finding id:   {UUID_A}")) == UUID_A
    assert extrair_finding_id(_adf(f"FINDING ID {UUID_A}")) == UUID_A


def test_descricao_sem_finding_id_devolve_vazio():
    """O caso comum: card aberto à mão, sem o carimbo da automação."""
    assert extrair_finding_id(_adf("Chamado aberto pelo time de infra.")) == ""


def test_descricao_nula_nao_estoura():
    assert extrair_finding_id(None) == ""
    assert extrair_finding_id({}) == ""


def test_rotulo_diferente_nao_casa():
    """`Finding-ID` com hífen não é o formato — e o teste fixa isso de propósito.
    Se um dia o template mudar, a métrica de saúde é que vai avisar."""
    assert extrair_finding_id(_adf(f"Finding-ID: {UUID_A}")) == ""


# ---------------------------------------------------------------------------
# Cliente HTTP — sessão falsa, sem rede
# ---------------------------------------------------------------------------


class RespostaFalsa:
    def __init__(self, corpo=None, status=200):
        self._corpo = corpo if corpo is not None else {}
        self.status_code = status

    def json(self):
        return self._corpo


class SessaoFalsa:
    def __init__(self, get=None, post=None):
        self._get = list(get or [])
        self._post = list(post or [])
        self.chamadas: list[tuple] = []

    def get(self, url, **kwargs):
        self.chamadas.append(("GET", url, kwargs))
        return self._get.pop(0)

    def post(self, url, **kwargs):
        self.chamadas.append(("POST", url, kwargs))
        return self._post.pop(0)


def _cliente(sessao) -> ClienteJira:
    return ClienteJira(
        base_url="https://jira.exemplo", email="a@b.c", token="t", sessao=sessao
    )


def test_pagina_da_fila_devolve_os_values():
    sessao = SessaoFalsa(get=[RespostaFalsa({"values": [{"key": "GVUL-1"}]})])
    assert _cliente(sessao).pagina_da_fila("2599", "4302", 0, 50) == [{"key": "GVUL-1"}]


def test_erro_http_na_fila_devolve_lista_vazia():
    """Fila indisponível não pode derrubar o sync das outras fontes."""
    sessao = SessaoFalsa(get=[RespostaFalsa({}, status=500)])
    assert _cliente(sessao).pagina_da_fila("2599", "4302", 0, 50) == []


def test_campos_em_lote_monta_jql_com_as_chaves():
    sessao = SessaoFalsa(post=[RespostaFalsa({"issues": [
        {"key": "GVUL-1", "fields": {"description": _adf("x")}},
    ]})])
    resultado = _cliente(sessao).campos_em_lote(["GVUL-1", "GVUL-2"], ["description"])

    _metodo, _url, kwargs = sessao.chamadas[0]
    assert kwargs["json"]["jql"] == "key in (GVUL-1,GVUL-2)"
    assert "GVUL-1" in resultado


def test_credenciais_obrigatorias():
    with pytest.raises(ValueError):
        ClienteJira(base_url="https://x", email="", token="t", sessao=SessaoFalsa())


# ---------------------------------------------------------------------------
# Extrator — paginação e parada
# ---------------------------------------------------------------------------


def _issue(chave, status="Em andamento", updated="2026-08-01T10:00:00.000-0300"):
    return {"key": chave, "fields": {"status": {"name": status}, "updated": updated}}


def test_pagina_ate_a_fila_acabar():
    """Página incompleta significa última página — sem isso, laço infinito."""
    sessao = SessaoFalsa(get=[
        RespostaFalsa({"values": [_issue("GVUL-1"), _issue("GVUL-2")]}),
        RespostaFalsa({"values": [_issue("GVUL-3")]}),
    ])
    extrator = ExtratorJira(_cliente(sessao), "2599", "4302", pagina=2)

    assert [t["ticket_id"] for t in extrator.tickets_da_fila()] == [
        "GVUL-1", "GVUL-2", "GVUL-3"
    ]


def test_o_ticket_traz_status_e_updated_sem_custo_extra():
    """Vêm na listagem da fila — não precisam da busca cara de description."""
    sessao = SessaoFalsa(get=[RespostaFalsa({"values": [
        _issue("GVUL-9", status="Aguardando", updated="2026-08-05T09:00:00.000-0300")
    ]})])
    extrator = ExtratorJira(_cliente(sessao), "2599", "4302", pagina=50)

    assert extrator.tickets_da_fila() == [{
        "ticket_id": "GVUL-9",
        "status": "Aguardando",
        "updated": "2026-08-05T09:00:00.000-0300",
    }]


# ---------------------------------------------------------------------------
# Sync contra o banco
# ---------------------------------------------------------------------------


class ExtratorFalso:
    """Contrato do ExtratorJira, servindo dados em memória.

    Registra quais chaves tiveram description buscada — é como os testes
    provam que o cache funcionou."""

    def __init__(self, tickets, descricoes=None):
        self._tickets = tickets
        self._descricoes = descricoes or {}
        self.buscou: list[str] = []

    def tickets_da_fila(self):
        return self._tickets

    def descricoes(self, chaves):
        self.buscou.extend(chaves)
        return {k: self._descricoes[k] for k in chaves if k in self._descricoes}


def _campos(finding_id="", plano=""):
    return {
        "description": _adf(f"Finding ID: {finding_id}") if finding_id else _adf("sem id"),
        "customfield_12627": _adf(plano) if plano else None,
    }


def _sincronizar(conn, extrator):
    from risk.contexto.jira import sincronizar_jira

    return sincronizar_jira(extrator, conn)


@pytest.mark.banco
def test_o_ticket_chega_no_banco_com_o_finding_id_extraido(conn):
    extrator = ExtratorFalso(
        [{"ticket_id": "GVUL-1", "status": "Em andamento", "updated": "2026-08-01"}],
        {"GVUL-1": _campos(UUID_A, "Aplicar patch 19.21")},
    )
    resultado = _sincronizar(conn, extrator)

    with conn.cursor() as cur:
        cur.execute("SELECT ticket_id, finding_id, status, action_plan FROM jira_ticket")
        assert cur.fetchone() == {
            "ticket_id": "GVUL-1",
            "finding_id": UUID_A,
            "status": "Em andamento",
            "action_plan": "Aplicar patch 19.21",
        }
    assert resultado.tickets == 1
    assert resultado.sem_finding_id == 0


@pytest.mark.banco
def test_card_sem_finding_id_fica_gravado_para_nao_ser_rebuscado(conn):
    """Contra-intuitivo mas é o ponto do cache: se o card sem carimbo não
    ficasse na tabela, ele seria rebuscado em toda execução para sempre."""
    extrator = ExtratorFalso(
        [{"ticket_id": "GVUL-2", "status": "Aberto", "updated": "2026-08-01"}],
        {"GVUL-2": _campos()},
    )
    resultado = _sincronizar(conn, extrator)

    with conn.cursor() as cur:
        cur.execute("SELECT ticket_id, finding_id FROM jira_ticket")
        assert cur.fetchone() == {"ticket_id": "GVUL-2", "finding_id": ""}
    assert resultado.sem_finding_id == 1


@pytest.mark.banco
def test_ticket_inalterado_nao_rebusca_a_description(conn):
    """A chamada cara é 1 requisição JQL por 100 cards. O `updated` do Jira é
    o que decide se vale pagar de novo."""
    ticket = {"ticket_id": "GVUL-1", "status": "Em andamento", "updated": "2026-08-01"}
    _sincronizar(conn, ExtratorFalso([ticket], {"GVUL-1": _campos(UUID_A, "plano")}))

    segundo = ExtratorFalso([ticket], {"GVUL-1": _campos(UUID_A, "plano")})
    _sincronizar(conn, segundo)

    assert segundo.buscou == [], "rebuscou description de ticket inalterado"
    with conn.cursor() as cur:
        cur.execute("SELECT finding_id, action_plan FROM jira_ticket")
        assert cur.fetchone() == {"finding_id": UUID_A, "action_plan": "plano"}


@pytest.mark.banco
def test_ticket_alterado_e_rebuscado(conn):
    _sincronizar(conn, ExtratorFalso(
        [{"ticket_id": "GVUL-1", "status": "Aberto", "updated": "2026-08-01"}],
        {"GVUL-1": _campos(UUID_A, "plano antigo")},
    ))

    segundo = ExtratorFalso(
        [{"ticket_id": "GVUL-1", "status": "Resolvido", "updated": "2026-08-09"}],
        {"GVUL-1": _campos(UUID_A, "plano novo")},
    )
    _sincronizar(conn, segundo)

    assert segundo.buscou == ["GVUL-1"]
    with conn.cursor() as cur:
        cur.execute("SELECT status, action_plan FROM jira_ticket")
        assert cur.fetchone() == {"status": "Resolvido", "action_plan": "plano novo"}


@pytest.mark.banco
def test_ticket_que_saiu_da_fila_desaparece(conn):
    """Snapshot: você decidiu que o vínculo some quando o card sai da fila."""
    _sincronizar(conn, ExtratorFalso(
        [{"ticket_id": "GVUL-1", "status": "Aberto", "updated": "2026-08-01"},
         {"ticket_id": "GVUL-2", "status": "Aberto", "updated": "2026-08-01"}],
        {"GVUL-1": _campos(UUID_A), "GVUL-2": _campos(UUID_B)},
    ))

    _sincronizar(conn, ExtratorFalso(
        [{"ticket_id": "GVUL-1", "status": "Aberto", "updated": "2026-08-01"}],
    ))

    with conn.cursor() as cur:
        cur.execute("SELECT ticket_id FROM jira_ticket ORDER BY ticket_id")
        assert [l["ticket_id"] for l in cur.fetchall()] == ["GVUL-1"]


@pytest.mark.banco
def test_fila_vazia_preserva_o_snapshot_anterior(conn):
    """Jira fora do ar devolve fila vazia. Apagar tudo faria o export perder
    todos os tickets de uma vez — pior que ficar um ciclo defasado."""
    _sincronizar(conn, ExtratorFalso(
        [{"ticket_id": "GVUL-1", "status": "Aberto", "updated": "2026-08-01"}],
        {"GVUL-1": _campos(UUID_A)},
    ))

    resultado = _sincronizar(conn, ExtratorFalso([]))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM jira_ticket")
        assert cur.fetchone()["n"] == 1
    assert resultado.tickets == 0


@pytest.mark.banco
def test_o_sync_registra_quantos_cards_ficaram_sem_vinculo(conn):
    """Sinal de saúde: se o template mudar e o regex parar de casar, esse
    número dispara em vez de a coluna esvaziar em silêncio."""
    _sincronizar(conn, ExtratorFalso(
        [{"ticket_id": f"GVUL-{i}", "status": "Aberto", "updated": "2026-08-01"}
         for i in range(4)],
        {"GVUL-0": _campos(UUID_A), "GVUL-1": _campos(), "GVUL-2": _campos(),
         "GVUL-3": _campos()},
    ))

    with conn.cursor() as cur:
        cur.execute("SELECT status, row_count, detail FROM context_sync WHERE source = 'JIRA'")
        linha = cur.fetchone()

    assert linha["status"] == "OK"
    assert linha["row_count"] == 4
    assert "3" in linha["detail"], f"detail deveria contar os sem vinculo: {linha['detail']}"


# ---------------------------------------------------------------------------
# A view de export
# ---------------------------------------------------------------------------


def _finding(cur, finding_id):
    cur.execute(
        "INSERT INTO plugin (plugin_id, name, raw) VALUES (100, 'p', '{}'::jsonb) "
        "ON CONFLICT (plugin_id) DO NOTHING"
    )
    cur.execute(
        "INSERT INTO finding_current (finding_id, product, state, plugin_id, "
        "first_found, indexed, natural_key, raw) VALUES (%s, 'VM', 'OPEN', 100, "
        "now(), now(), %s, '{}'::jsonb)",
        (finding_id, finding_id),
    )


@pytest.mark.banco
def test_o_export_traz_o_ticket_do_finding(conn):
    with conn.transaction(), conn.cursor() as cur:
        _finding(cur, UUID_A)
    _sincronizar(conn, ExtratorFalso(
        [{"ticket_id": "GVUL-7", "status": "Em andamento", "updated": "2026-08-01"}],
        {"GVUL-7": _campos(UUID_A, "Aplicar patch")},
    ))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT jira_ticket_id, jira_status, jira_action_plan "
            "  FROM vw_finding_export WHERE finding_id = %s",
            (UUID_A,),
        )
        assert cur.fetchone() == {
            "jira_ticket_id": "GVUL-7",
            "jira_status": "Em andamento",
            "jira_action_plan": "Aplicar patch",
        }


@pytest.mark.banco
def test_dois_tickets_no_mesmo_finding_nao_duplicam_a_linha(conn):
    """O risco real de juntar mais uma tabela. Vence o alterado mais
    recentemente — é onde o plano de ação está atualizado."""
    with conn.transaction(), conn.cursor() as cur:
        _finding(cur, UUID_A)
    _sincronizar(conn, ExtratorFalso(
        [{"ticket_id": "GVUL-99", "status": "Fechado", "updated": "2026-08-01T10:00:00.000-0300"},
         {"ticket_id": "GVUL-100", "status": "Em andamento", "updated": "2026-08-20T10:00:00.000-0300"}],
        {"GVUL-99": _campos(UUID_A, "plano velho"),
         "GVUL-100": _campos(UUID_A, "plano novo")},
    ))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM vw_finding_export")
        assert cur.fetchone()["n"] == 1, "finding duplicado por ter dois tickets"

        cur.execute("SELECT jira_ticket_id, jira_action_plan FROM vw_finding_export")
        assert cur.fetchone() == {
            "jira_ticket_id": "GVUL-100",
            "jira_action_plan": "plano novo",
        }


@pytest.mark.banco
def test_finding_sem_ticket_aparece_com_coluna_vazia(conn):
    with conn.transaction(), conn.cursor() as cur:
        _finding(cur, UUID_B)

    with conn.cursor() as cur:
        cur.execute("SELECT finding_id, jira_ticket_id FROM vw_finding_export")
        assert cur.fetchone() == {"finding_id": UUID_B, "jira_ticket_id": None}


def test_sem_credencial_o_motor_sabe_que_nao_pode_sincronizar():
    from risk.config import ConfigMotor

    vazio = ConfigMotor(pg_dsn="host=x")
    assert vazio.tem_jira is False

    completo = ConfigMotor(
        pg_dsn="host=x", jira_email="a@b.c", jira_token="t",
        jira_base_url="https://jira", jira_sd_service_desk_id="2599",
        jira_sd_queue_id="4302",
    )
    assert completo.tem_jira is True
