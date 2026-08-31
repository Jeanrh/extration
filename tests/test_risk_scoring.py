"""As oito notas, a matriz e o SLA.

Os casos vêm dos testes de scoring do extraction — uma especificação escrita
contra outra implementação, o que lhes dá poder de falsificação real sobre este
porte. Três grupos foram ajustados de propósito, e estão marcados com
DIVERGÊNCIA: nesses pontos os testes de lá falham contra o código de lá, e é o
código que gera o CSV que o negócio usa hoje.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from conftest import RAIZ  # noqa: F401  (garante sys.path)

from risk.scoring.matriz import _banda, calcular_prioridade
from risk.scoring.motor import (
    Achado,
    calcular_aging,
    calcular_sla,
    nota_ameaca,
    nota_arquitetura,
    nota_bia,
    nota_camada,
    nota_cvss,
    nota_exploit,
    nota_exposicao,
    nota_pci,
    pontuar,
)

# ---------------------------------------------------------------------------
# Matriz
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pontuacao,banda",
    [(0, 0), (99, 0), (100, 1), (199, 1), (200, 2), (299, 2), (300, 3), (1000, 3)],
)
def test_bandas_da_matriz(pontuacao, banda):
    assert _banda(pontuacao) == banda


@pytest.mark.parametrize(
    "py,px,priority_id,priority_name",
    [
        (350, 350, 1, "Muito Alta"),
        (350, 250, 2, "Alta"),
        (350, 150, 2, "Alta"),
        (250, 350, 2, "Alta"),
        (250, 250, 2, "Alta"),
        (150, 350, 2, "Alta"),
        # Q8 — impacto baixo com probabilidade alta continua Média, não Alta.
        (50, 350, 3, "Média"),
        (350, 50, 3, "Média"),
        (250, 150, 3, "Média"),
        (250, 50, 3, "Média"),
        (150, 250, 3, "Média"),
        (150, 150, 3, "Média"),
        (50, 250, 3, "Média"),
        (150, 50, 4, "Baixa"),
        (50, 150, 4, "Baixa"),
        (50, 50, 4, "Baixa"),
    ],
)
def test_os_dezesseis_quadrantes(py, px, priority_id, priority_name):
    assert calcular_prioridade(py, px)[:2] == (priority_id, priority_name)


def test_limiar_exato_de_300_sobe_de_banda():
    assert calcular_prioridade(300, 300) == (1, "Muito Alta", "Q16")
    assert calcular_prioridade(299, 299)[1] == "Alta"


# ---------------------------------------------------------------------------
# Vetor py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "criticidade,nota",
    [
        ("crise", 100), ("Crise", 100), ("CRISE", 100),
        ("alto", 50), ("Alto", 50),
        ("medio", 25), ("médio", 25), ("Medio", 25),
        ("baixo", 10), ("Baixo", 10),
        ("", 50), ("desconhecido", 50),
    ],
)
def test_nota_bia(criticidade, nota):
    assert nota_bia(criticidade) == nota


@pytest.mark.parametrize(
    "pci,nota",
    [
        ("PCI", 100), ("pci", 100), ("Sim", 100),
        ("Nao", 10), ("", 10), ("outro", 10),
        # DIVERGÊNCIA 1 — a regra de "Escopo Estendido" está inativa no modelo
        # Power BI, então o código do extraction devolve 10. Os testes de lá
        # esperam 100 e falham.
        ("Escopo Estendido", 10),
    ],
)
def test_nota_pci(pci, nota):
    assert nota_pci(pci) == nota


@pytest.mark.parametrize(
    "produto,nota", [("WAS", 100), ("was", 100), ("VM", 10), ("", 10)]
)
def test_nota_exposicao(produto, nota):
    assert nota_exposicao(produto) == nota


@pytest.mark.parametrize(
    "arquitetura,nota,rotulo",
    [
        ("App/Web", 100, "App/Web"),
        ("API", 80, "API"),
        ("Mobile", 60, "Mobile"),
        ("Infra", 50, "Infra"),
        ("Workflow", 40, "Workflow"),
        ("Aplicacao Enduser", 20, "Aplicacao Enduser"),
        ("Mainframe", 10, "Mainframe"),
        ("nao cadastrada", 40, "Sem informacoes"),
        ("", 40, "Sem informacoes"),
    ],
)
def test_nota_arquitetura_de_vm_vem_do_cmdb(arquitetura, nota, rotulo):
    assert nota_arquitetura("VM", "srv-01", arquitetura) == (nota, rotulo)


def test_was_nao_consulta_cmdb_para_arquitetura():
    """Todo achado de internet é aplicação; "api" na URL rebaixa para API."""
    assert nota_arquitetura("WAS", "https://loja.exemplo.com", "") == (100, "App/Web")
    assert nota_arquitetura("WAS", "https://api.exemplo.com", "Mainframe") == (80, "API")


# ---------------------------------------------------------------------------
# Vetor px
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cvss,nota",
    [
        (Decimal("9.8"), 100), (Decimal("9.0"), 100),
        (Decimal("8.9"), 80), (Decimal("7.0"), 80),
        (Decimal("6.9"), 40), (Decimal("4.0"), 40),
        (Decimal("3.9"), 10), (Decimal("0.1"), 10),
    ],
)
def test_nota_cvss_por_faixa(cvss, nota):
    assert nota_cvss(cvss) == nota


@pytest.mark.parametrize("ausente", [None, Decimal("0.0"), 0])
def test_sem_cvss3_a_nota_e_dez(ausente):
    """DIVERGÊNCIA 2 — a mais material das três.

    O código do extraction devolve 10 sem olhar a severidade nativa, enquanto
    `_cvss_label` do mesmo arquivo FAZ o fallback por severity. Hoje um finding
    CRITICAL sem CVSS3 é rotulado "Crítico" e pontuado 10."""
    assert nota_cvss(ausente) == 10


@pytest.mark.parametrize(
    "ease,nota",
    [
        ("Exploits are available", 100),
        ("Exploit exists", 100),
        ("Exploit framework", 100),
        ("No known exploits", 10),
        ("no known exploit", 10),
        ("No Known Exploits", 10),
        ("", 10),
        # O Data Stream manda null onde a API clássica manda string.
        (None, 10),
    ],
)
def test_nota_exploit(ease, nota):
    assert nota_exploit(ease) == nota


@pytest.mark.parametrize(
    "camada,nota",
    [
        ("aplicacao", 100), ("Aplicacao", 100), ("APLICACAO", 100),
        ("middleware", 80),
        ("banco de dados", 50),
        ("sistema operacional", 30),
        ("appliance", 20),
        ("hardening", 10),
        # DIVERGÊNCIA 3 — vazio e não mapeado valem 30 (o mesmo de "sistema
        # operacional"), que é o default do DAX. Os testes do extraction
        # esperam 10.
        ("", 30), ("desconhecido", 30),
    ],
)
def test_nota_camada_de_vm(camada, nota):
    assert nota_camada("VM", camada) == nota


def test_was_e_sempre_camada_de_aplicacao():
    assert nota_camada("WAS", "hardening") == 100


@pytest.mark.parametrize("presente,nota", [(True, 100), (False, 10)])
def test_nota_ameaca(presente, nota):
    assert nota_ameaca(presente) == nota


# ---------------------------------------------------------------------------
# Aging e SLA
# ---------------------------------------------------------------------------


def test_aging_conta_dias_desde_o_primeiro_avistamento():
    agora = dt.datetime(2026, 8, 28, tzinfo=dt.timezone.utc)
    assert calcular_aging(dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc), agora) == 27


def test_sem_first_found_nao_ha_aging_nem_sla():
    assert calcular_aging(None) is None
    assert calcular_sla("Q16", None) == ""


@pytest.mark.parametrize(
    "quadrante,aging,situacao",
    [
        ("Q16", 30, "Dentro do Prazo"),
        ("Q16", 31, "Fora do Prazo"),
        ("Q11", 90, "Dentro do Prazo"),
        ("Q7", 181, "Fora do Prazo"),
        ("Q1", 270, "Dentro do Prazo"),
    ],
)
def test_sla_por_quadrante(quadrante, aging, situacao):
    assert calcular_sla(quadrante, aging) == situacao


# ---------------------------------------------------------------------------
# Veredito completo
# ---------------------------------------------------------------------------


def test_maximo_impacto_e_maxima_probabilidade_dao_q16():
    veredito = pontuar(
        Achado(
            finding_id="f-1",
            produto="WAS",                              # exposicao=100
            asset_name="https://loja.exemplo.com",      # arquitetura=100
            criticality_cmdb="crise",                   # bia=100
            pci="PCI",                                  # pci=100
            cvss3_base_score=Decimal("9.5"),            # cvss=100
            em_threat_intel=True,                       # ameaca=100
            exploitability_ease="Exploits are available",  # exploit=100
            layer="aplicacao",                          # camada=100 (WAS)
        )
    )
    assert veredito.py == 450.0
    assert veredito.px == 400.0
    assert (veredito.priority_name, veredito.quadrant) == ("Muito Alta", "Q16")


def test_minimo_impacto_e_minima_probabilidade_dao_q1():
    veredito = pontuar(
        Achado(
            finding_id="f-2",
            produto="VM",                    # exposicao=10
            criticality_cmdb="baixo",        # bia=10
            pci="Nao",                       # pci=10
            arquitetura="Mainframe",         # arquitetura=10
            cvss3_base_score=Decimal("3.0"),  # cvss=10
            em_threat_intel=False,           # ameaca=10
            exploitability_ease="No known exploits",  # exploit=10
            layer="hardening",               # camada=10
        )
    )
    assert veredito.py == 45.0
    assert (veredito.priority_name, veredito.quadrant) == ("Baixa", "Q1")


def test_veredito_carrega_as_oito_notas_para_auditoria():
    """A linha tem que explicar sozinha a prioridade — é o que dispensa
    reexecutar o motor para responder "por que esta nota?"."""
    veredito = pontuar(Achado(finding_id="f-3", produto="VM"))
    assert (
        veredito.nota_bia,
        veredito.nota_pci,
        veredito.nota_exposure,
        veredito.nota_arch,
        veredito.nota_cvss,
        veredito.nota_threat,
        veredito.nota_exploit,
        veredito.nota_layer,
    ) == (50, 10, 10, 40, 10, 10, 10, 30)
