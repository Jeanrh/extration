"""Parse do manifest e a ordem de processamento.

O Tenable gera um manifest a cada 15 minutos listando os payloads daquela
janela **na ordem em que foram enviados**. Essa ordem é a única fonte de
sequência que o sistema tem, então ela é preservada literalmente: o array
`payloads` não é reordenado nem paralelizado (seção 6.2).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from .erros import ErroParse

log = logging.getLogger(__name__)


def epoch_ms_para_datetime(valor: Any) -> dt.datetime | None:
    """`first_record_timestamp` e `last_record_timestamp` vêm em epoch ms —
    às vezes como inteiro, às vezes como string (seção 7.5)."""
    if valor is None or valor == "":
        return None
    try:
        milissegundos = int(valor)
    except (TypeError, ValueError):
        return None
    return dt.datetime.fromtimestamp(milissegundos / 1000, tz=dt.timezone.utc)


@dataclass(frozen=True)
class EntradaPayload:
    """Uma linha do array `payloads` do manifest."""

    path: str                       # key do objeto no S3; é a PK de ingest_file
    md5: str | None
    version: int | None
    num_updates: int | None
    num_deletes: int | None
    first_record_timestamp: dt.datetime | None
    last_record_timestamp: dt.datetime | None
    scan_id: str | None

    @property
    def relogio_fallback(self) -> dt.datetime | None:
        """Relógio de versão de último recurso (seção 6.4).

        Usado quando `indexed`/`indexed_at` vierem nulos e nos registros de
        `deletes[]`, que não trazem relógio nenhum. Continua sendo dado do
        Tenable, não o relógio do job."""
        return self.last_record_timestamp or self.first_record_timestamp


@dataclass(frozen=True)
class Manifest:
    path: str                       # key do próprio manifest no S3
    tipo: str | None                # MANIFEST_FINDING, ...
    payload_type: str | None        # FINDING, ...
    payloads: tuple[EntradaPayload, ...]


def parse_manifest(path: str, doc: dict[str, Any]) -> Manifest:
    """Manifest já desempacotado (`s3.ler_documento`) → objeto tipado."""
    entradas_brutas = doc.get("payloads")
    if entradas_brutas is None:
        entradas_brutas = []
    if not isinstance(entradas_brutas, list):
        raise ErroParse(f"manifest {path}: 'payloads' não é uma lista")

    entradas: list[EntradaPayload] = []
    for posicao, bruto in enumerate(entradas_brutas):
        if not isinstance(bruto, dict):
            raise ErroParse(f"manifest {path}: payloads[{posicao}] não é um objeto")
        caminho = bruto.get("path")
        if not caminho:
            raise ErroParse(f"manifest {path}: payloads[{posicao}] sem 'path'")
        scan_id = bruto.get("scan_id")
        entradas.append(
            EntradaPayload(
                path=str(caminho),
                md5=bruto.get("md5") or None,
                version=_inteiro_ou_none(bruto.get("version")),
                num_updates=_inteiro_ou_none(bruto.get("num_updates")),
                num_deletes=_inteiro_ou_none(bruto.get("num_deletes")),
                first_record_timestamp=epoch_ms_para_datetime(
                    bruto.get("first_record_timestamp")
                ),
                last_record_timestamp=epoch_ms_para_datetime(
                    bruto.get("last_record_timestamp")
                ),
                scan_id=str(scan_id) if scan_id else None,
            )
        )

    return Manifest(
        path=path,
        tipo=doc.get("type"),
        payload_type=doc.get("payload_type"),
        payloads=tuple(entradas),
    )


def _inteiro_ou_none(valor: Any) -> int | None:
    if valor is None or valor == "":
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None
