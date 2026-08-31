"""Pesos, limiares e SLA — porte de `src/core/settings.py` do extraction.

Este é o arquivo que muda com mais frequência em todo o projeto: mexer num peso
é o ajuste rotineiro do motor. Por isso ele fica isolado, sem nenhuma lógica —
o diff de uma mudança de peso tem que caber em uma linha e ser óbvio na revisão.
"""

from __future__ import annotations


class Pesos:
    """Multiplicadores de cada dimensão nas fórmulas py e px."""

    BIA = 1.0
    PCI = 1.0
    EXPOSICAO = 1.0
    ARQUITETURA = 1.5

    CVSS = 1.0
    AMEACA = 1.1
    EXPLOIT = 1.1
    CAMADA = 0.8


class Limiares:
    """Fronteiras das quatro bandas da matriz de prioridade."""

    BAIXO = 100
    MEDIO = 200
    ALTO = 300


# Prazo em dias corridos desde `first_found`, por quadrante.
SLA_DIAS: dict[str, int] = {
    "Q16": 30,
    "Q15": 90, "Q14": 90, "Q13": 90, "Q12": 90, "Q11": 90,
    "Q10": 180, "Q9": 180, "Q8": 180, "Q7": 180, "Q6": 180, "Q5": 180, "Q4": 180,
    "Q3": 270, "Q2": 270, "Q1": 270,
}
