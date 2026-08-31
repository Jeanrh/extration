"""A cadeia de resolução do CMDB, com os payloads reais.

    servidor.acronym -> sigla -> (teamid) -> cockpit -> tribo, aliança

O cockpit é resolvido **no sync**, em memória: `teamid` da sigla casa com a
`key` do cockpit, e o resultado é gravado direto em `cmdb_acronym`. Assim a
leitura dos ~500 mil findings não precisa de mais um JOIN, e o `teamid` não
vira coluna de ninguém — ele é chave de busca, não informação de negócio.

O que sai do CMDB para o consumidor: **sigla, unidade de negócio (aliança),
tribo** — do cockpit — e **equipe solucionadora**, que vem do campo `team` da
própria sigla.

Os dicionários abaixo são os do CMDB de produção, copiados sem edição — é a
única forma de garantir que o motor casa com o vocabulário real.
"""

from __future__ import annotations

import pytest

from conftest import RAIZ  # noqa: F401  (garante sys.path)

pytestmark = pytest.mark.banco


# --- payloads de produção, verbatim ---------------------------------------

SIGLA_ACD = {
    "id": "574863", "key": "CMDB-574863", "acronym": "ACD", "name": "ACD - ARGO CD",
    "status": "Operacional", "domain": "Tecnologia da Informacao",
    "subdomain": "Infraestrutura de TI", "BIA": "Nao", "PCI": "Escopo Estendido",
    "criticality": "Alto", "created": "20/Mar/25 6:03 PM", "updated": "",
    "infrastructure": "Cloud AWS", "service": "Tecnologia",
    "squad": "Plataforma de Deploy", "squadid": "OR-345014",
    "team": "Plataforma de Deploy", "teamid": "OR-345014",
}
COCKPIT_DEPLOY = {
    "id": "345014", "key": "OR-345014", "name": "GARAGEM", "status": "Active",
    "tribo": "Garagem", "alianca": "Transformação e Governança",
    "vp": "Tecnologia e Negócios", "squad": "", "squadid": "", "team": "", "teamid": "",
    "created": "12/Jul/24 5:21 PM", "updated": "10/Aug/26 6:25 PM",
}
SERVIDOR_ACD = {
    "id": "413330", "objectKey": "CMDB-413330", "name": "ALP-D1-GTC01",
    "status": "Operacional", "ipv4": "10.50.53.101", "environment": "Produção",
    "acronym": "ACD - ARGO CD", "os": "Microsoft Windows Server 2012 (64-bit)",
    "accountname": "", "tenableid": "", "infrastructure": "OnPremise",
    "criticality": "", "platform": "", "cluster": "", "layer": "",
    "created": "", "updated": "",
}


class ExtratorFalso:
    def __init__(self, siglas=None, servidores=None, urls=None, times=None):
        self._siglas, self._servidores = siglas or [], servidores or []
        self._urls, self._times = urls or [], times or []

    def extract_acronyms(self, max_age_hours=None):
        return self._siglas

    def extract_servers(self, max_age_hours=None):
        return self._servidores

    def extract_urls(self, max_age_hours=None):
        return self._urls

    def extract_cockpits(self, max_age_hours=None):
        return self._times


def _sincronizar(conn, **kwargs):
    from risk.contexto.cmdb import sincronizar_cmdb

    return sincronizar_cmdb(ExtratorFalso(**kwargs), conn)


# --- a resolução, no sync --------------------------------------------------


def test_a_sigla_ja_chega_com_unidade_de_negocio_e_tribo(conn):
    """Resolvido na carga, não na leitura: o `teamid` some aqui dentro."""
    _sincronizar(conn, siglas=[SIGLA_ACD], times=[COCKPIT_DEPLOY])

    with conn.cursor() as cur:
        cur.execute(
            "SELECT unidade_negocio, tribo FROM cmdb_acronym WHERE sigla = 'ACD'"
        )
        assert cur.fetchone() == {
            "unidade_negocio": "Transformação e Governança",
            "tribo": "Garagem",
        }


def test_sem_cockpit_correspondente_a_sigla_fica_sem_tribo(conn):
    """Vazio é o certo: chutar tribo é pior do que admitir que não sabe."""
    _sincronizar(conn, siglas=[SIGLA_ACD], times=[])

    with conn.cursor() as cur:
        cur.execute("SELECT unidade_negocio, tribo FROM cmdb_acronym WHERE sigla = 'ACD'")
        assert cur.fetchone() == {"unidade_negocio": "", "tribo": ""}


def test_o_contexto_carrega_so_o_que_o_negocio_pediu(conn):
    """Domínio, subdomínio e ids de junção não são informação de negócio — são
    plumbing, e plumbing não vira coluna.

    `equipe_solucionadora` é o contrário: foi cortada na 0008 e devolvida na
    0009, porque o export dos times usa. Ela vem do campo `team` da sigla."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            " WHERE table_name = 'cmdb_acronym'"
        )
        colunas = {linha["column_name"] for linha in cur.fetchall()}

    assert {"unidade_negocio", "tribo", "equipe_solucionadora"} <= colunas
    assert not colunas & {"domain", "subdomain", "equipe_sol", "equipe_id"}


def test_a_tabela_de_cockpit_nao_existe_mais(conn):
    """Ninguém lê: a resolução acontece em memória, no sync."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('cmdb_team') AS t")
        assert cur.fetchone()["t"] is None


# --- a cadeia inteira, até a linha do finding ------------------------------


def _finding_no_servidor(cur, hostname="ALP-D1-GTC01"):
    cur.execute(
        "INSERT INTO plugin (plugin_id, name, family, cvss3_base_score, "
        "exploitability_ease, raw) VALUES (100, 'Oracle', 'Databases', '9.5', "
        "'Exploits are available', '{}'::jsonb) ON CONFLICT DO NOTHING"
    )
    cur.execute(
        "INSERT INTO finding_current (finding_id, product, state, plugin_id, "
        "asset_hostname, first_found, indexed, natural_key, raw) "
        "VALUES ('f-1', 'VM', 'OPEN', 100, %s, now() - interval '10 days', now(), "
        "'nk', '{}'::jsonb)",
        (hostname,),
    )


def test_a_cadeia_completa_chega_na_linha_do_finding(conn):
    """servidor -> sigla -> cockpit, num finding real."""
    from risk.executor import recalcular

    _sincronizar(conn, siglas=[SIGLA_ACD], servidores=[SERVIDOR_ACD], times=[COCKPIT_DEPLOY])
    with conn.transaction(), conn.cursor() as cur:
        _finding_no_servidor(cur)

    recalcular(conn, engine_version="teste")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT sigla, unidade_negocio, tribo "
            "  FROM finding_risk WHERE finding_id = 'f-1'"
        )
        assert cur.fetchone() == {
            "sigla": "ACD",
            "unidade_negocio": "Transformação e Governança",
            "tribo": "Garagem",
        }


def test_cockpit_de_nome_parecido_nao_contamina_a_sigla(conn):
    """O guarda do casamento por id: um cockpit cujo `name` bate com o `team`
    da sigla não pode sequestrar a tribo. `name` é rótulo de tribo, `team` é
    rótulo de equipe — vocabulários diferentes."""
    from risk.executor import recalcular

    impostor = dict(
        COCKPIT_DEPLOY, id="9", key="OR-999", name="PLATAFORMA DE DEPLOY",
        tribo="Tribo Errada", alianca="Aliança Errada",
    )
    _sincronizar(
        conn, siglas=[SIGLA_ACD], servidores=[SERVIDOR_ACD],
        times=[impostor, COCKPIT_DEPLOY],
    )
    with conn.transaction(), conn.cursor() as cur:
        _finding_no_servidor(cur)

    recalcular(conn, engine_version="teste")

    with conn.cursor() as cur:
        cur.execute("SELECT unidade_negocio, tribo FROM finding_risk WHERE finding_id = 'f-1'")
        assert cur.fetchone() == {
            "unidade_negocio": "Transformação e Governança",
            "tribo": "Garagem",
        }
