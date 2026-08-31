"""Tradução do nome de exibição de uma sigla para o seu código.

Porte de `Enricher.resolve_sigla` do extraction. O CMDB guarda no servidor e na
URL o nome como o Jira exibe ("GTeC - Gestão de Terminais"), não o código
("GTEC") — e é o código que indexa BIA, PCI e arquitetura. Errar aqui não
levanta exceção: o finding só cai nos defaults do scoring, silenciosamente.
"""

from __future__ import annotations

import unicodedata

# Ordem importa: o mais específico primeiro, senão " -" casaria antes de " - ".
_SEPARADORES = (" - ", "- ", " -")


def normalizar(texto: str) -> str:
    """Sem acento, maiúsculo, sem espaço nas pontas.

    Garante que "GTeC - Gestão de Terminais" e "GTEC - GESTAO DE TERMINAIS" —
    a mesma sigla vinda de fontes diferentes — comparem iguais."""
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c)).upper().strip()


def resolver_sigla(
    bruto: str,
    codigos: set[str] | frozenset[str],
    nome_para_sigla: dict[str, str],
) -> str:
    """Código da sigla, ou string vazia quando não há match.

    Devolver vazio é deliberado: um chute aqui vira BIA e PCI errados no score
    de todo finding daquele ativo.

    Prioridade:
      1. o próprio valor já é um código ("CTRLPREDIAL")
      2. nome de exibição normalizado ("GTeC - Gestão de..." → "GTEC")
      3. o pedaço antes do separador, se for um código conhecido
    """
    if not bruto:
        return ""

    maiusculo = bruto.strip().upper()
    if maiusculo in codigos:
        return maiusculo

    normalizado = normalizar(bruto)
    if normalizado in nome_para_sigla:
        return nome_para_sigla[normalizado]

    for separador in _SEPARADORES:
        partes = bruto.split(separador, 1)
        if len(partes) > 1:
            candidato = partes[0].strip().upper()
            if candidato in codigos:
                return candidato

    return ""


def indices_de_sigla(siglas: list[dict]) -> tuple[set[str], dict[str, str]]:
    """Os dois índices que `resolver_sigla` consome, montados uma vez por sync."""
    codigos = {s["acronym"].upper() for s in siglas if s.get("acronym")}
    nome_para_sigla = {
        normalizar(s["name"]): s["acronym"].upper()
        for s in siglas
        if s.get("acronym") and s.get("name")
    }
    return codigos, nome_para_sigla
