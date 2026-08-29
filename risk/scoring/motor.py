"""As oito notas e o veredito final — porte de `src/scoring/` do extraction.

    py = BIA·1.0 + PCI·1.0 + Exposição·1.0 + Arquitetura·1.5
    px = CVSS·1.0 + Ameaça·1.1 + Exploit·1.1 + Camada·0.8

O porte é **fiel ao código** do extraction, não aos testes dele: 8 dos 132
testes de scoring de lá falham contra a própria implementação. Como é o código
que gera o `tenable_full.csv` que o negócio usa hoje, é ele o parâmetro de
paridade. As três divergências estão marcadas com `DIVERGÊNCIA` abaixo, para
serem decididas depois da rodada de comparação — e não no meio do porte, onde
seria impossível separar erro de mudança deliberada.

As funções são puras e recebem valores já tipados: no banco `cvss3_base_score`
é numeric e `first_found` é timestamptz, então nada aqui faz parse de string
de CSV.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from .matriz import calcular_prioridade
from .pesos import SLA_DIAS, Pesos

VM = "VM"
WAS = "WAS"

# Arquitetura do CMDB (minúscula) → (nota, rótulo)
_ARQUITETURAS: dict[str, tuple[int, str]] = {
    "app/web": (100, "App/Web"),
    "api": (80, "API"),
    "mobile": (60, "Mobile"),
    "infra": (50, "Infra"),
    "workflow": (40, "Workflow"),
    "aplicacao enduser": (20, "Aplicacao Enduser"),
    "aplicação enduser": (20, "Aplicacao Enduser"),
    "mainframe": (10, "Mainframe"),
}
ARQUITETURA_PADRAO = (40, "Sem informacoes")

_CAMADAS: dict[str, int] = {
    "aplicacao": 100,
    "middleware": 80,
    "banco de dados": 50,
    "sistema operacional": 30,
    "appliance": 20,
    "hardening": 10,
}
# DIVERGÊNCIA 3 — camada vazia ou não mapeada vale 30, o mesmo de "sistema
# operacional". É o default do DAX segundo o comentário do extraction; os
# testes de lá esperam 10.
CAMADA_PADRAO = 30


@dataclass(frozen=True)
class Achado:
    """A entrada do scoring: o finding já cruzado com o contexto de negócio."""

    finding_id: str
    produto: str                              # VM | WAS
    asset_name: str = ""                      # no WAS é a URL
    cvss3_base_score: Decimal | float | None = None
    exploitability_ease: str | None = None
    layer: str = ""
    familia: str = ""
    first_found: dt.datetime | None = None
    # contexto externo ao Tenable
    sigla: str = ""
    pci: str = ""
    bia: str = ""
    criticality_cmdb: str = ""
    unidade_negocio: str = ""
    arquitetura: str = ""
    em_threat_intel: bool = False


@dataclass
class Veredito:
    nota_bia: int = 0
    nota_pci: int = 0
    nota_exposure: int = 0
    nota_arch: int = 0
    nota_cvss: int = 0
    nota_threat: int = 0
    nota_exploit: int = 0
    nota_layer: int = 0
    py: float = 0.0
    px: float = 0.0
    priority_id: int = 4
    priority_name: str = "Baixa"
    quadrant: str = ""
    sla_status: str = ""
    aging: int | None = None
    arch_type: str = ""
    layer: str = ""
    familia: str = field(default="")


# ---------------------------------------------------------------------------
# Vetor py — risco do ATIVO
# ---------------------------------------------------------------------------


def nota_bia(criticidade: str) -> int:
    valor = (criticidade or "").lower().strip()
    if valor == "crise":
        return 100
    if valor == "alto":
        return 50
    if valor in ("medio", "médio"):
        return 25
    if valor == "baixo":
        return 10
    return 50  # vazio ou não mapeado


def nota_pci(pci: str) -> int:
    # DIVERGÊNCIA 1 — só "PCI"/"Sim" pontuam. "Escopo Estendido" cai em 10
    # porque a regra está inativa no modelo Power BI; os testes do extraction
    # esperam 100.
    return 100 if (pci or "").lower().strip() in ("pci", "sim") else 10


def nota_exposicao(produto: str) -> int:
    """WAS é internet, VM é intranet."""
    return 100 if (produto or "").upper().strip() == WAS else 10


def nota_arquitetura(produto: str, asset_name: str, arquitetura: str) -> tuple[int, str]:
    """Devolve (nota, rótulo). WAS não consulta o CMDB: é sempre aplicação."""
    if (produto or "").upper().strip() == WAS:
        if "api" in (asset_name or "").lower():
            return 80, "API"
        return 100, "App/Web"

    return _ARQUITETURAS.get((arquitetura or "").lower().strip(), ARQUITETURA_PADRAO)


# ---------------------------------------------------------------------------
# Vetor px — risco da VULNERABILIDADE
# ---------------------------------------------------------------------------


def nota_cvss(cvss3: Decimal | float | None) -> int:
    # DIVERGÊNCIA 2 — sem CVSS3 a nota é 10, sem olhar a severidade nativa.
    # Os testes do extraction esperam fallback por severity (CRITICAL→100,
    # HIGH→80, MEDIUM→40), e `_cvss_label` do extraction FAZ esse fallback
    # para o rótulo. Ou seja: hoje um finding CRITICAL sem CVSS3 é rotulado
    # "Crítico" e pontuado 10. É a divergência mais material das três.
    valor = float(cvss3 or 0.0)
    if valor == 0.0:
        return 10
    if valor >= 9.0:
        return 100
    if valor >= 7.0:
        return 80
    if valor >= 4.0:
        return 40
    return 10


def nota_ameaca(em_threat_intel: bool) -> int:
    return 100 if em_threat_intel else 10


def nota_exploit(exploitability_ease: str | None) -> int:
    """Texto, não boolean: o Data Stream manda null onde a API manda string."""
    valor = (exploitability_ease or "").strip()
    if not valor or "no known" in valor.lower():
        return 10
    return 100


def nota_camada(produto: str, layer: str) -> int:
    """WAS é sempre camada de aplicação — o DAX não consulta nada para internet."""
    if (produto or "").upper().strip() == WAS:
        return 100
    return _CAMADAS.get((layer or "").lower().strip(), CAMADA_PADRAO)


# ---------------------------------------------------------------------------
# Derivados
# ---------------------------------------------------------------------------


def calcular_aging(first_found: dt.datetime | None, agora: dt.datetime | None = None) -> int | None:
    """Dias corridos desde `first_found`. None quando não há data."""
    if first_found is None:
        return None
    agora = agora or dt.datetime.now(dt.timezone.utc)
    if first_found.tzinfo is None:
        first_found = first_found.replace(tzinfo=dt.timezone.utc)
    if agora.tzinfo is None:
        agora = agora.replace(tzinfo=dt.timezone.utc)
    return (agora - first_found).days


def calcular_sla(quadrante: str, aging: int | None) -> str:
    """"Dentro do Prazo", "Fora do Prazo" ou "" quando falta informação."""
    if not quadrante or aging is None:
        return ""
    prazo = SLA_DIAS.get(quadrante)
    if prazo is None:
        return ""
    return "Dentro do Prazo" if aging <= prazo else "Fora do Prazo"


# ---------------------------------------------------------------------------
# Veredito
# ---------------------------------------------------------------------------


def pontuar(achado: Achado, agora: dt.datetime | None = None) -> Veredito:
    """Calcula py, px, quadrante, prioridade e SLA de um achado."""
    bia = nota_bia(achado.criticality_cmdb)
    pci = nota_pci(achado.pci)
    exposicao = nota_exposicao(achado.produto)
    arquitetura, rotulo_arq = nota_arquitetura(
        achado.produto, achado.asset_name, achado.arquitetura
    )

    cvss = nota_cvss(achado.cvss3_base_score)
    ameaca = nota_ameaca(achado.em_threat_intel)
    exploit = nota_exploit(achado.exploitability_ease)
    camada = nota_camada(achado.produto, achado.layer)

    py = (
        bia * Pesos.BIA
        + pci * Pesos.PCI
        + exposicao * Pesos.EXPOSICAO
        + arquitetura * Pesos.ARQUITETURA
    )
    px = (
        cvss * Pesos.CVSS
        + ameaca * Pesos.AMEACA
        + exploit * Pesos.EXPLOIT
        + camada * Pesos.CAMADA
    )

    priority_id, priority_name, quadrante = calcular_prioridade(py, px)
    aging = calcular_aging(achado.first_found, agora)

    return Veredito(
        nota_bia=bia,
        nota_pci=pci,
        nota_exposure=exposicao,
        nota_arch=arquitetura,
        nota_cvss=cvss,
        nota_threat=ameaca,
        nota_exploit=exploit,
        nota_layer=camada,
        py=py,
        px=px,
        priority_id=priority_id,
        priority_name=priority_name,
        quadrant=quadrante,
        sla_status=calcular_sla(quadrante, aging),
        aging=aging,
        arch_type=rotulo_arq,
        layer=achado.layer,
        familia=achado.familia,
    )
