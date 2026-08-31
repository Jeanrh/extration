"""Registro de quando e como cada fonte de contexto sincronizou.

Sem isto não há resposta para "qual snapshot do CMDB gerou este score?" — e a
linha de risco fica com um número que ninguém consegue reconstituir.
"""

from __future__ import annotations

import datetime as dt


def registrar_sync(
    cur,
    fonte: str,
    status: str,
    row_count: int | None = None,
    detail: str | None = None,
    sincronizado_em: dt.datetime | None = None,
) -> None:
    cur.execute(
        "INSERT INTO context_sync (source, synced_at, status, row_count, detail) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (source) DO UPDATE SET "
        "  synced_at = EXCLUDED.synced_at, status = EXCLUDED.status, "
        "  row_count = EXCLUDED.row_count, detail = EXCLUDED.detail",
        (
            fonte,
            sincronizado_em or dt.datetime.now(dt.timezone.utc),
            status,
            row_count,
            detail,
        ),
    )
