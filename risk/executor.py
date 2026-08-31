"""Orquestração do recálculo: lê, pontua, grava o que mudou, registra o evento.

A divisão de trabalho segue a spec §17.2 no que importa — o diff de 500 mil
linhas nunca sobe para a memória do Python, ele acontece no `IS DISTINCT FROM`
do upsert. O que sobe para o Python é uma linha por vez, pelo tempo de calcular
oito notas: a regra de scoring é o produto deste projeto e muda toda semana,
e mantê-la em SQL obrigaria a sincronizar cada ajuste de peso com uma segunda
cópia das fórmulas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .scoring.motor import Achado, pontuar

log = logging.getLogger(__name__)

DIRETORIO_SQL = Path(__file__).parent / "sql"
LOTE_PADRAO = 20_000

COLUNAS_STAGING = (
    "finding_id", "product", "py", "px", "quadrant", "priority_id",
    "priority_name", "sla_status", "aging",
    "nota_bia", "nota_pci", "nota_exposure", "nota_arch",
    "nota_cvss", "nota_threat", "nota_exploit", "nota_layer",
    "sigla", "pci", "bia", "criticality_cmdb", "unidade_negocio",
    "tribo",
    "arch_type", "layer", "familia", "engine_version", "context_synced_at",
)


@dataclass(frozen=True)
class Resultado:
    calculados: int
    gravados: int
    eventos: int


@lru_cache(maxsize=None)
def carregar_sql(nome: str) -> str:
    caminho = DIRETORIO_SQL / f"{nome}.sql"
    if not caminho.is_file():
        raise FileNotFoundError(f"SQL do motor não encontrado: {caminho}")
    return caminho.read_text(encoding="utf-8")


def _idade_do_contexto(conn):
    """Quando o CMDB sincronizou pela última vez — vai gravado em cada linha."""
    with conn.cursor() as cur:
        cur.execute("SELECT synced_at FROM context_sync WHERE source = 'CMDB'")
        linha = cur.fetchone()
    return linha["synced_at"] if linha else None


def _achado(linha: dict) -> Achado:
    return Achado(
        finding_id=linha["finding_id"],
        produto=linha["product"],
        asset_name=linha["asset_name"],
        cvss3_base_score=linha["cvss3_base_score"],
        exploitability_ease=linha["exploitability_ease"],
        layer=linha["layer"],
        familia=linha["familia"],
        first_found=linha["first_found"],
        sigla=linha["sigla"],
        pci=linha["pci"],
        bia=linha["bia"],
        criticality_cmdb=linha["criticality_cmdb"],
        unidade_negocio=linha["unidade_negocio"],
        tribo=linha["tribo"],
        arquitetura=linha["arquitetura"],
        em_threat_intel=linha["em_threat_intel"],
    )


def _tupla_staging(achado: Achado, veredito, engine_version: str, context_synced_at):
    return (
        achado.finding_id, achado.produto,
        veredito.py, veredito.px, veredito.quadrant,
        veredito.priority_id, veredito.priority_name, veredito.sla_status,
        veredito.aging,
        veredito.nota_bia, veredito.nota_pci, veredito.nota_exposure,
        veredito.nota_arch, veredito.nota_cvss, veredito.nota_threat,
        veredito.nota_exploit, veredito.nota_layer,
        achado.sigla, achado.pci, achado.bia, achado.criticality_cmdb,
        achado.unidade_negocio, achado.tribo,
        veredito.arch_type, veredito.layer,
        veredito.familia,
        engine_version, context_synced_at,
    )


def recalcular(
    conn,
    engine_version: str,
    lote: int = LOTE_PADRAO,
    agora=None,
) -> Resultado:
    """Recalcula o risco de TODOS os findings não deletados.

    Sem filtro de tempo e sem filtro de estado — é o que o banco viabiliza e o
    export por janela nunca permitiu.
    """
    context_synced_at = _idade_do_contexto(conn)

    with conn.cursor() as cur:
        cur.execute(carregar_sql("20_staging"))

    leitura = carregar_sql("10_leitura")
    calculados = 0
    ultimo_id = ""

    while True:
        with conn.cursor() as cur:
            cur.execute(leitura, (ultimo_id, lote))
            linhas = cur.fetchall()

        if not linhas:
            break

        tuplas = []
        for linha in linhas:
            achado = _achado(linha)
            veredito = pontuar(achado, agora=agora)
            tuplas.append(
                _tupla_staging(achado, veredito, engine_version, context_synced_at)
            )

        with conn.cursor() as cur:
            comando = f"COPY stg_risk ({', '.join(COLUNAS_STAGING)}) FROM STDIN"
            with cur.copy(comando) as copia:
                for tupla in tuplas:
                    copia.write_row(tupla)

        calculados += len(linhas)
        ultimo_id = linhas[-1]["finding_id"]

    # Eventos antes do upsert: depois, o estado anterior já teria sumido.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(carregar_sql("30_eventos"))
        eventos = cur.rowcount
        cur.execute(carregar_sql("40_upsert"))
        gravados = cur.rowcount

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS stg_risk")

    log.info(
        "motor | recalculo | calculados=%s gravados=%s eventos=%s versao=%s",
        calculados, gravados, eventos, engine_version,
    )
    return Resultado(calculados=calculados, gravados=gravados, eventos=eventos)
