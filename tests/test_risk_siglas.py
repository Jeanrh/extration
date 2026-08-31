"""Resolução de sigla — porte de `Enricher.resolve_sigla` do extraction.

O CMDB guarda no servidor/URL o *nome de exibição* da sigla ("GTeC - Gestão de
Terminais"), não o código ("GTEC"). Todo o vetor py depende de acertar essa
tradução: sem sigla não há BIA, PCI nem arquitetura, e o finding cai nos
defaults sem que ninguém perceba.
"""

from __future__ import annotations

from conftest import RAIZ  # noqa: F401  (garante sys.path)

from risk.contexto.siglas import resolver_sigla

CODIGOS = {"GTEC", "APP-V", "CTRLPREDIAL"}
NOME_PARA_SIGLA = {"GTEC - GESTAO DE TERMINAIS": "GTEC"}


def test_codigo_direto_vence():
    assert resolver_sigla("ctrlpredial", CODIGOS, NOME_PARA_SIGLA) == "CTRLPREDIAL"


def test_nome_de_exibicao_com_acento_resolve_pelo_indice_normalizado():
    """"GTeC - Gestão de Terminais" e "GTEC - GESTAO DE TERMINAIS" são a mesma
    sigla vindo de fontes diferentes (Jira vs acronyms)."""
    assert resolver_sigla("GTeC - Gestão de Terminais", CODIGOS, NOME_PARA_SIGLA) == "GTEC"


def test_separador_e_o_ultimo_recurso():
    """Nome não cadastrado no índice, mas com o código antes do separador."""
    assert resolver_sigla("APP-V - Virtualização", CODIGOS, {}) == "APP-V"


def test_sem_match_devolve_vazio_em_vez_de_chutar():
    assert resolver_sigla("Sistema Desconhecido", CODIGOS, NOME_PARA_SIGLA) == ""
    assert resolver_sigla("", CODIGOS, NOME_PARA_SIGLA) == ""
