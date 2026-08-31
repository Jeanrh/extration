"""O gerador de massa sintética.

Uma simulação só vale se a massa for coerente: finding que aponta para um
servidor que existe, servidor cuja sigla resolve, sigla cujo time existe no
cockpit. Sem isso o motor cai nos defaults em 100% das linhas e a simulação
vira número bonito sobre lixo — pior que não medir, porque parece medição.

Estes testes não precisam de banco: checam a massa antes de ela chegar lá.
"""

from __future__ import annotations

import pytest

from conftest import RAIZ  # noqa: F401  (garante sys.path)

from scripts.simular import gerar_massa

ESTADOS = {"OPEN", "REOPENED", "FIXED"}
PRODUTOS = {"VM", "WAS"}


@pytest.fixture(scope="module")
def massa():
    return gerar_massa(findings=300, siglas=12, semente=7)


def test_a_massa_respeita_o_tamanho_pedido(massa):
    assert len(massa.findings) == 300
    assert len(massa.siglas) == 12


def test_todo_finding_de_vm_aponta_para_um_servidor_que_existe(massa):
    hostnames = {s["name"].upper() for s in massa.servidores}
    orfaos = [
        f for f in massa.findings
        if f["product"] == "VM" and (f["asset_hostname"] or "").upper() not in hostnames
    ]
    assert orfaos == []


def test_todo_finding_de_was_aponta_para_uma_url_que_existe(massa):
    urls = {u["name"].upper() for u in massa.urls}
    orfaos = [
        f for f in massa.findings
        if f["product"] == "WAS" and (f["asset_fqdn"] or "").upper() not in urls
    ]
    assert orfaos == []


def test_o_acronimo_do_servidor_resolve_para_uma_sigla_real(massa):
    """É o primeiro salto da cadeia; se ele falha, py inteiro cai no default."""
    from risk.contexto.siglas import indices_de_sigla, resolver_sigla

    codigos, nomes = indices_de_sigla(massa.siglas)
    nao_resolvem = [
        s["name"] for s in massa.servidores
        if not resolver_sigla(s["acronym"], codigos, nomes)
    ]
    assert nao_resolvem == []


def test_o_teamid_da_sigla_existe_no_cockpit(massa):
    """Último salto: sem ele, unidade de negócio e tribo chegam vazias."""
    chaves = {c["key"] for c in massa.cockpits}
    assert {s["teamid"] for s in massa.siglas} <= chaves


def test_todo_finding_referencia_um_plugin_gerado(massa):
    ids = {p["plugin_id"] for p in massa.plugins}
    assert {f["plugin_id"] for f in massa.findings} <= ids


def test_produtos_e_estados_sao_validos(massa):
    assert {f["product"] for f in massa.findings} <= PRODUTOS
    assert {f["state"] for f in massa.findings} <= ESTADOS


def test_a_massa_tem_variedade_que_move_o_score(massa):
    """Massa uniforme não testa nada: todas as linhas cairiam no mesmo
    quadrante e o relatório não distinguiria motor certo de motor quebrado."""
    assert len({f["product"] for f in massa.findings}) == 2
    assert len({f["state"] for f in massa.findings}) == 3
    assert len({p["cvss3_base_score"] for p in massa.plugins}) > 3
    # exploitability_ease nulo em parte das linhas é o caso real do Data Stream
    eases = {p["exploitability_ease"] for p in massa.plugins}
    assert None in eases and len(eases) > 1
    # "Escopo Estendido" precisa aparecer: é a DIVERGENCIA 1 em dado vivo
    assert "Escopo Estendido" in {s["PCI"] for s in massa.siglas}


def test_a_semente_torna_a_massa_reprodutivel():
    """Sem isso, duas execuções não são comparáveis entre si."""
    a = gerar_massa(findings=50, siglas=4, semente=99)
    b = gerar_massa(findings=50, siglas=4, semente=99)
    assert [f["finding_id"] for f in a.findings] == [f["finding_id"] for f in b.findings]
    assert [p["cvss3_base_score"] for p in a.plugins] == [
        p["cvss3_base_score"] for p in b.plugins
    ]
