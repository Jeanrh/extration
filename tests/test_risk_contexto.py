"""Sync das fontes externas para tabelas do PostgreSQL.

O motor roda num pod efêmero: cache em disco morre com o pod. O contexto que
o scoring precisa (CMDB, arquitetura, threat intel) é materializado no banco a
cada execução, e cada fonte registra em `context_sync` quando e como veio — é
o que permite responder "qual snapshot gerou este score?".
"""

from __future__ import annotations

import pytest

from conftest import RAIZ  # noqa: F401  (garante sys.path)

pytestmark = pytest.mark.banco


class ExtratorFalso:
    """Mesma interface do CMDBExtractor, sem tocar o JSM.

    Devolve os dicionários já mapeados — o formato que o extractor produz."""

    def __init__(self, siglas=None, servidores=None, urls=None, times=None, erro=None):
        self._siglas = siglas or []
        self._servidores = servidores or []
        self._urls = urls or []
        self._times = times or []
        self._erro = erro

    def extract_acronyms(self, max_age_hours=None):
        return self._siglas

    def extract_servers(self, max_age_hours=None):
        if self._erro:
            raise self._erro
        return self._servidores

    def extract_urls(self, max_age_hours=None):
        return self._urls

    def extract_cockpits(self, max_age_hours=None):
        return self._times


SIGLA_GTEC = {
    "acronym": "GTEC",
    "name": "GTeC - Gestão de Terminais",
    "status": "Ativo",
    "PCI": "PCI",
    "BIA": "Alto",
    "criticality": "Crise",
    "team": "Time Infra",
    "domain": "Tecnologia",
    "subdomain": "Infraestrutura",
    "infrastructure": "OnPremise",
}
SERVIDOR = {
    "name": "srv-app-01",
    "ipv4": "10.0.0.7",
    "acronym": "GTeC - Gestão de Terminais",
    "status": "Ativo",
    "infrastructure": "OnPremise",
    "environment": "Produção",
}
URL = {
    "name": "https://loja.exemplo.com",
    "acronym": "GTEC",
    "status": "Ativo",
    "pci": "PCI",
    "alliance": "Varejo",
}
TIME = {"name": "Time Infra", "tribo": "Plataformas", "alianca": "Varejo", "vp": "VP Tecnologia"}


def _sincronizar(conn, **kwargs):
    from risk.contexto.cmdb import sincronizar_cmdb

    return sincronizar_cmdb(ExtratorFalso(**kwargs), conn)


def test_sigla_chega_com_os_atributos_que_movem_o_vetor_py(conn):
    _sincronizar(conn, siglas=[SIGLA_GTEC], times=[TIME])

    with conn.cursor() as cur:
        cur.execute(
            "SELECT pci, bia, criticality, equipe_sol FROM cmdb_acronym WHERE sigla = 'GTEC'"
        )
        assert cur.fetchone() == {
            "pci": "PCI",
            "bia": "Alto",
            "criticality": "Crise",
            "equipe_sol": "Time Infra",
        }


def test_servidor_guarda_a_sigla_ja_resolvida_do_nome_de_exibicao(conn):
    """Resolver no sync, não na hora do score: o score de 500 mil findings vira
    JOIN, e fica registrado qual código saiu de qual nome."""
    _sincronizar(conn, siglas=[SIGLA_GTEC], servidores=[SERVIDOR])

    with conn.cursor() as cur:
        cur.execute("SELECT hostname, ipv4, sigla FROM cmdb_server")
        assert cur.fetchone() == {
            "hostname": "SRV-APP-01",
            "ipv4": "10.0.0.7",
            "sigla": "GTEC",
        }


def test_url_do_was_tambem_resolve_sigla(conn):
    _sincronizar(conn, siglas=[SIGLA_GTEC], urls=[URL])

    with conn.cursor() as cur:
        cur.execute("SELECT url, sigla FROM cmdb_url")
        assert cur.fetchone() == {"url": "HTTPS://LOJA.EXEMPLO.COM", "sigla": "GTEC"}


def test_falha_no_meio_do_sync_preserva_o_snapshot_anterior(conn):
    """Se o JSM cair, o motor calcula com o contexto do ciclo anterior — nunca
    com contexto vazio, que geraria score plausível e silenciosamente errado."""
    _sincronizar(conn, siglas=[SIGLA_GTEC], servidores=[SERVIDOR])

    with pytest.raises(RuntimeError):
        _sincronizar(
            conn,
            siglas=[SIGLA_GTEC],
            servidores=[SERVIDOR],
            erro=RuntimeError("JSM fora do ar"),
        )

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS total FROM cmdb_server")
        assert cur.fetchone() == {"total": 1}, "o snapshot anterior tem que sobreviver"


def test_sync_registra_a_procedencia(conn):
    resultado = _sincronizar(conn, siglas=[SIGLA_GTEC], servidores=[SERVIDOR])

    assert resultado.siglas == 1
    assert resultado.servidores == 1

    with conn.cursor() as cur:
        cur.execute("SELECT status, row_count FROM context_sync WHERE source = 'CMDB'")
        assert cur.fetchone() == {"status": "OK", "row_count": 2}


# ---------------------------------------------------------------------------
# Arquitetura — CSV mocado, mantido à mão
# ---------------------------------------------------------------------------

CSV_ARQUITETURA = "Alias/Sigla;Arquitetura\nGTEC;Infra\nLOJA;App/Web\n"


def test_arquitetura_carrega_do_csv_versionado(conn, tmp_path):
    """Continua sendo um arquivo que alguém edita num PR; vira tabela só para
    o JOIN do score."""
    from risk.contexto.arquitetura import carregar_arquitetura

    caminho = tmp_path / "arquitetura.csv"
    caminho.write_text(CSV_ARQUITETURA, encoding="utf-8")

    assert carregar_arquitetura(caminho, conn) == 2

    with conn.cursor() as cur:
        cur.execute("SELECT arquitetura FROM architecture WHERE sigla = 'GTEC'")
        assert cur.fetchone() == {"arquitetura": "Infra"}


def test_arquitetura_ausente_nao_derruba_o_motor(conn, tmp_path):
    """Espelha o extraction: loader some, o scoring cai no default 40. Falhar
    aqui pararia a priorização inteira por causa de um arquivo de referência."""
    from risk.contexto.arquitetura import carregar_arquitetura

    assert carregar_arquitetura(tmp_path / "nao-existe.csv", conn) == 0

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM context_sync WHERE source = 'ARCHITECTURE'")
        assert cur.fetchone() == {"status": "FAILED"}


# ---------------------------------------------------------------------------
# Threat intel — snapshot da API clássica
# ---------------------------------------------------------------------------


class ExtratorIntelFalso:
    def __init__(self, ids):
        self._ids = ids

    def extract_threat_intel(self):
        return [{"finding_id": i} for i in self._ids]


def test_threat_intel_substitui_o_snapshot_anterior(conn):
    """Snapshot, não acumulado: o que saiu da janela de 90 dias/OPEN volta a
    valer 10 na nota de ameaça. Foi a decisão tomada no desenho."""
    from risk.contexto.intel import sincronizar_threat_intel

    sincronizar_threat_intel(ExtratorIntelFalso(["f-1", "f-2"]), conn)
    assert sincronizar_threat_intel(ExtratorIntelFalso(["f-3"]), conn) == 1

    with conn.cursor() as cur:
        cur.execute("SELECT finding_id FROM threat_intel ORDER BY finding_id")
        assert [linha["finding_id"] for linha in cur.fetchall()] == ["f-3"]


def test_intel_vazio_preserva_o_snapshot_anterior(conn):
    """O export clássico tem timeout de ~10 min e devolve lista vazia quando
    estoura. Zerar a tabela nesse caso rebaixaria toda vulnerabilidade de
    ameaça ativa de uma vez — o extraction defende isso com `merge=True`."""
    from risk.contexto.intel import sincronizar_threat_intel

    sincronizar_threat_intel(ExtratorIntelFalso(["f-1"]), conn)
    sincronizar_threat_intel(ExtratorIntelFalso([]), conn)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS total FROM threat_intel")
        assert cur.fetchone() == {"total": 1}
