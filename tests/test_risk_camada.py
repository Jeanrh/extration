"""Resolução de camada tecnológica — porte de `layer_resolver` do extraction.

A camada é resolvida **por plugin**, não por finding: o casamento de keywords
contra o nome do plugin é caro, e existem dezenas de milhares de plugins para
centenas de milhares de findings. Materializar em `plugin_layer` transforma a
`nota_layer` de 500 mil buscas de substring num JOIN.

A ordem de resolução espelha o DAX: classifica pela VULNERABILIDADE (nome do
plugin), não pelo tipo do ativo. Patch de kernel num servidor de banco é
"Sistema Operacional", não "Banco de Dados".
"""

from __future__ import annotations

import pytest

from conftest import RAIZ  # noqa: F401  (garante sys.path)

from risk.derivacoes.camada import indexar_keywords, resolver_camada

# Formatos que o segredo do Vault realmente usa: string direta, dict com
# "familia" e JSON serializado como string.
SEGREDO = {
    "linux_app_family": "tomcat,jboss,weblogic",
    "linux_database_family": {"familia": "oracle,postgresql"},
    "windows_so_family": '{"familia": "microsoft bulletins,kb"}',
    "linux_middleware_family": "nginx,apache http",
}


@pytest.fixture(scope="module")
def indice():
    return indexar_keywords(SEGREDO)


def test_indice_aceita_string_dict_e_json(indice):
    assert set(indice) == {
        "aplicacao",
        "banco de dados",
        "sistema operacional",
        "middleware",
    }
    assert "oracle" in indice["banco de dados"]
    assert "microsoft bulletins" in indice["sistema operacional"]


def test_keyword_curta_ou_com_dois_pontos_e_descartada():
    """Regra do extraction: keyword com menos de 2 chars ou com ':' polui o
    match por substring — "a" casaria com quase todo nome de plugin."""
    indice = indexar_keywords({"linux_app_family": "tomcat,a,web:server"})
    assert indice["aplicacao"] == ["tomcat"]


def test_nome_do_plugin_decide_e_devolve_a_familia_que_casou(indice):
    camada, familia, origem = resolver_camada(
        family="Databases", plugin_name="Oracle Database 19c < 19.18", indice=indice
    )
    assert (camada, familia, origem) == ("banco de dados", "oracle", "plugin_name")


def test_aplicacao_ganha_de_sistema_operacional_no_empate(indice):
    """A ordem de prioridade é fixa: aplicacao > banco de dados > appliance >
    middleware > sistema operacional > hardening."""
    camada, familia, _ = resolver_camada(
        family="", plugin_name="Tomcat on Microsoft Bulletins host", indice=indice
    )
    assert (camada, familia) == ("aplicacao", "tomcat")


def test_family_do_plugin_e_o_fallback_quando_nenhuma_keyword_casa(indice):
    """`asset_category` vinha das tags do Tenable, que o Data Stream não
    publica. O extraction já cai em `plugin.family` nesse caso — e é ela que
    existe no banco."""
    camada, familia, origem = resolver_camada(
        family="Web Servers", plugin_name="Plugin sem match algum", indice=indice
    )
    assert (camada, familia, origem) == ("middleware", "", "family")


def test_sem_match_nenhum_fica_vazio_para_o_scoring_aplicar_o_default(indice):
    """Vazio aqui vira nota 30 no scoring (mesmo valor de "sistema
    operacional"), que é o default do DAX."""
    assert resolver_camada(family="", plugin_name="Nada", indice=indice) == ("", "", "nenhum")


def test_sem_vault_o_fallback_por_family_continua_valendo():
    """Vault fora do ar não pode zerar a camada de todo mundo: `plugin.family`
    resolve sozinha uma boa parte."""
    camada, _, origem = resolver_camada(
        family="Databases", plugin_name="Oracle Database", indice={}
    )
    assert (camada, origem) == ("banco de dados", "family")


# ---------------------------------------------------------------------------
# Materialização em plugin_layer
# ---------------------------------------------------------------------------


def _plugin(cur, plugin_id: int, nome: str, family: str) -> None:
    cur.execute(
        "INSERT INTO plugin (plugin_id, name, family, raw) "
        "VALUES (%s, %s, %s, '{}'::jsonb)",
        (plugin_id, nome, family),
    )


@pytest.mark.banco
def test_derivacao_grava_uma_linha_por_plugin(conn, indice):
    from risk.derivacoes.camada import derivar_camadas

    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur, 1, "Oracle Database 19c < 19.18", "Databases")
        _plugin(cur, 2, "Plugin sem match", "Web Servers")
        _plugin(cur, 3, "Nada casa aqui", "Port scanners")

    assert derivar_camadas(conn, indice) == 3

    with conn.cursor() as cur:
        cur.execute(
            "SELECT plugin_id, layer, familia, resolved_by "
            "  FROM plugin_layer ORDER BY plugin_id"
        )
        assert cur.fetchall() == [
            {
                "plugin_id": 1,
                "layer": "banco de dados",
                "familia": "oracle",
                "resolved_by": "plugin_name",
            },
            {"plugin_id": 2, "layer": "middleware", "familia": "", "resolved_by": "family"},
            {"plugin_id": 3, "layer": "", "familia": "", "resolved_by": "nenhum"},
        ]


@pytest.mark.banco
def test_rederivar_nao_duplica_nem_deixa_orfao(conn, indice):
    """O motor roda todo dia; a derivação tem que ser idempotente. E plugin que
    saiu da base não pode deixar camada para trás."""
    from risk.derivacoes.camada import derivar_camadas

    with conn.transaction(), conn.cursor() as cur:
        _plugin(cur, 1, "Oracle Database", "Databases")

    derivar_camadas(conn, indice)
    derivar_camadas(conn, indice)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM plugin WHERE plugin_id = 1")

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS total FROM plugin_layer")
        assert cur.fetchone() == {"total": 0}
