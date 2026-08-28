"""Publicação das métricas no CloudWatch (seção 13).

A métrica que mais importa é `HoursSinceLastManifest`. O stream do Tenable
quebra em silêncio se a role IAM for deletada, se a trust relationship mudar ou
se a policy do bucket for alterada — e nesse cenário o job roda, não acha
manifest novo, termina com sucesso e o dashboard mostra "zero aberturas hoje",
que parece boa notícia. É o único alarme que pega essa falha (seção 12.5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .config import Config

log = logging.getLogger(__name__)

CONTAGEM = "Count"
SEGUNDOS = "Seconds"


@dataclass(frozen=True)
class Metrica:
    nome: str
    valor: float
    unidade: str = CONTAGEM


class Publicador:
    """Publica no CloudWatch quando habilitado; senão só loga.

    Métrica que não sai nunca deve derrubar o job: a ingestão já aconteceu."""

    def __init__(self, config: Config, cliente=None):
        self.config = config
        self._cliente = cliente

    def cliente(self):
        if self._cliente is None:
            import boto3

            self._cliente = boto3.client("cloudwatch", region_name=self.config.region_name)
        return self._cliente

    def publicar(self, metricas: list[Metrica]) -> None:
        for metrica in metricas:
            log.info("métrica %s=%s %s", metrica.nome, metrica.valor, metrica.unidade)
        if not self.config.cloudwatch_habilitado:
            log.debug("CloudWatch desabilitado (CLOUDWATCH_ENABLED); métricas só em log")
            return
        try:
            self.cliente().put_metric_data(
                Namespace=self.config.cloudwatch_namespace,
                MetricData=[
                    {
                        "MetricName": m.nome,
                        "Value": float(m.valor),
                        "Unit": m.unidade,
                    }
                    for m in metricas
                ],
            )
        except Exception:  # noqa: BLE001 - observabilidade não derruba ingestão
            log.exception("falha ao publicar métricas no CloudWatch")


def coletar(conn, resultado: Any, quarentena: int, abertos: int) -> list[Metrica]:
    """Monta o conjunto da seção 13.1 a partir do resultado do ciclo."""
    metricas = [
        Metrica("PayloadsProcessed", resultado.payloads_ok),
        Metrica("RecordsIngested", resultado.registros),
        Metrica("EventsGenerated", resultado.eventos),
        Metrica("FilesQuarantined", quarentena),
        Metrica("FindingsOpen", abertos),
        Metrica("JobDurationSeconds", resultado.duracao_segundos, SEGUNDOS),
    ]
    horas = resultado.horas_desde_ultimo_manifest
    if horas is not None:
        metricas.insert(0, Metrica("HoursSinceLastManifest", round(horas, 2)))
    return metricas


def contar_quarentena(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS total FROM ingest_file WHERE status = 'QUARANTINED'")
        return int(cur.fetchone()["total"])


def contar_abertos(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS total FROM finding_current "
            "WHERE state <> 'FIXED' AND deleted_at IS NULL"
        )
        return int(cur.fetchone()["total"])
