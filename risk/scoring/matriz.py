"""Matriz de prioridade: (py, px) → quadrante e prioridade.

Porte de `src/scoring/matrix.py` do extraction, com o mesmo encadeamento de
condições. A ordem das comparações é significativa e foi mantida na íntegra:
reescrevê-la numa forma "mais limpa" muda silenciosamente a fronteira de
alguns quadrantes.

Grade (calculo.md §4.5):

               px<100  100-200  200-300  px>=300
    py>=300:     Q7     Q12      Q14      Q16
    200-300:     Q5     Q10      Q13      Q15
    100-200:     Q2     Q4       Q9       Q11
    py<100:      Q1     Q3       Q6       Q8
"""

from __future__ import annotations

from .pesos import Limiares

# (banda_py, banda_px) → quadrante
_QUADRANTE: dict[tuple[int, int], str] = {
    (3, 3): "Q16", (3, 2): "Q14", (3, 1): "Q12", (3, 0): "Q7",
    (2, 3): "Q15", (2, 2): "Q13", (2, 1): "Q10", (2, 0): "Q5",
    (1, 3): "Q11", (1, 2): "Q9", (1, 1): "Q4", (1, 0): "Q2",
    (0, 3): "Q8", (0, 2): "Q6", (0, 1): "Q3", (0, 0): "Q1",
}

MUITO_ALTA = (1, "Muito Alta")
ALTA = (2, "Alta")
MEDIA = (3, "Média")
BAIXA = (4, "Baixa")


def _banda(pontuacao: float) -> int:
    if pontuacao >= Limiares.ALTO:
        return 3
    if pontuacao >= Limiares.MEDIO:
        return 2
    if pontuacao >= Limiares.BAIXO:
        return 1
    return 0


def calcular_prioridade(py: float, px: float) -> tuple[int, str, str]:
    """Devolve (priority_id, priority_name, quadrante)."""
    # vermelho
    if px >= 300 and py >= 300:
        prioridade = MUITO_ALTA
    # laranja
    elif (100 <= px < 200) and py >= 300:
        prioridade = ALTA
    elif (200 <= px < 300) and py >= 300:
        prioridade = ALTA
    elif (200 <= px < 300) and (200 <= py < 300):
        prioridade = ALTA
    elif px >= 300 and (100 <= py < 300):
        prioridade = ALTA
    # amarelo
    elif (100 <= px < 200) and (100 <= py < 300):
        prioridade = MEDIA
    elif (200 <= px < 300) and py < 200:
        prioridade = MEDIA
    elif px >= 200 and py < 100:
        prioridade = MEDIA
    elif px < 100 and py >= 200:
        prioridade = MEDIA
    # verde
    else:
        prioridade = BAIXA

    return prioridade[0], prioridade[1], _QUADRANTE[(_banda(py), _banda(px))]
