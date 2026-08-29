"""Carga do CSV de arquitetura para a tabela `architecture`.

O arquivo é mocado e mantido à mão — alguém edita e abre um PR. Ele continua
sendo a fonte de verdade; a tabela existe só para o score virar JOIN.

Ausência do arquivo não derruba o motor. É o mesmo contrato do extraction
(`get_architecture_index` devolve dicionário vazio e loga aviso): sem
arquitetura, `score_architecture` cai no default 40 e a priorização segue. Uma
exceção aqui pararia o cálculo de todo o backlog por causa de um arquivo de
referência.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

from .procedencia import registrar_sync

log = logging.getLogger(__name__)

FONTE = "ARCHITECTURE"
COLUNA_SIGLA = "Alias/Sigla"
COLUNA_ARQUITETURA = "Arquitetura"
DELIMITADOR = ";"


def _ler(caminho: Path) -> list[tuple[str, str]]:
    linhas: dict[str, tuple[str, str]] = {}
    with open(caminho, encoding="utf-8", errors="replace", newline="") as fh:
        for linha in csv.DictReader(fh, delimiter=DELIMITADOR):
            sigla = str(linha.get(COLUNA_SIGLA, "")).strip().upper()
            if sigla:
                linhas[sigla] = (sigla, str(linha.get(COLUNA_ARQUITETURA, "")).strip())
    return list(linhas.values())


def carregar_arquitetura(caminho: Path, conn) -> int:
    """Recarrega `architecture` a partir do CSV. Devolve quantas siglas entraram."""
    caminho = Path(caminho)
    if not caminho.is_file():
        log.warning("arquitetura | arquivo não encontrado: %s", caminho)
        with conn.transaction(), conn.cursor() as cur:
            registrar_sync(
                cur, FONTE, "FAILED", 0, f"arquivo não encontrado: {caminho}"
            )
        return 0

    linhas = _ler(caminho)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("TRUNCATE architecture")
        if linhas:
            cur.executemany(
                "INSERT INTO architecture (sigla, arquitetura) VALUES (%s, %s)", linhas
            )
        registrar_sync(cur, FONTE, "OK", len(linhas))

    log.info("arquitetura | carregada | siglas=%s", len(linhas))
    return len(linhas)
