"""CLI do motor e degradação das fontes externas.

A regra que estes testes fixam: nenhuma fonte externa indisponível pode parar a
priorização. O motor prefere contexto de um ciclo atrás — ou o fallback — a não
calcular nada.
"""

from __future__ import annotations

import pytest

from conftest import RAIZ  # noqa: F401  (garante sys.path)

from risk.cli import montar_parser
from risk.config import ConfigMotor


def _config(**kwargs) -> ConfigMotor:
    return ConfigMotor(pg_dsn="host=x dbname=y", **kwargs)


def test_parser_expoe_os_tres_comandos():
    for comando in ("sync-context", "run", "status"):
        assert montar_parser().parse_args([comando]).comando == comando


def test_comando_obrigatorio():
    with pytest.raises(SystemExit):
        montar_parser().parse_args([])


def test_sem_credenciais_o_motor_sabe_que_nao_pode_sincronizar():
    """`sync-context` usa isto para pular a fonte em vez de estourar — e o
    snapshot anterior daquela fonte continua valendo."""
    vazio = _config()
    assert vazio.tem_cmdb is False
    assert vazio.tem_intel is False

    completo = _config(
        jira_email="a@b.c", jira_token="t", jira_base_url="https://jira",
        tenable_access_key="ak", tenable_secret_key="sk",
    )
    assert completo.tem_cmdb is True
    assert completo.tem_intel is True


def test_vault_sem_variaveis_devolve_indice_vazio(monkeypatch):
    """Sem Vault, `resolver_camada` cai no fallback por plugin.family. Derrubar
    o motor aqui pararia a priorização inteira por causa de um segredo."""
    from risk.contexto.vault import keywords_de_camada

    for variavel in ("VAULT_ADDR", "VAULT_ROLE_ID", "VAULT_SECRET_ID"):
        monkeypatch.delenv(variavel, raising=False)

    assert keywords_de_camada(_config()) == {}


def test_vault_fora_do_ar_devolve_indice_vazio(monkeypatch):
    from risk.contexto import vault

    monkeypatch.setenv("VAULT_ADDR", "https://vault.invalido")
    monkeypatch.setenv("VAULT_ROLE_ID", "r")
    monkeypatch.setenv("VAULT_SECRET_ID", "s")

    def _explode(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(vault, "ler_segredo", _explode)

    assert vault.keywords_de_camada(_config()) == {}


def test_pg_dsn_ausente_e_erro_de_configuracao(monkeypatch):
    """Aqui, sim, falhar é o certo: sem banco não há o que recalcular."""
    from ingestion.erros import ErroConfiguracao
    from risk.config import carregar_config

    monkeypatch.setenv("PG_DSN", "")
    with pytest.raises(ErroConfiguracao):
        carregar_config()
