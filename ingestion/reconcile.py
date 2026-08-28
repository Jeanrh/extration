"""Reconciliação semanal local, sem inventar acesso ao console Tenable."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, TextIO

NOT_PROVIDED = "NOT_PROVIDED"


def _validar_console(vm: int | None, was: int | None) -> None:
    for produto, valor in (("VM", vm), ("WAS", was)):
        if valor is not None and valor < 0:
            raise ValueError(f"contagem do console {produto} não pode ser negativa")


def _comparacao(
    abertos: dict[str, int], vm: int | None, was: int | None
) -> str | dict[str, Any]:
    if vm is None and was is None:
        return NOT_PROVIDED
    comparacao: dict[str, Any] = {}
    for produto, valor in (("VM", vm), ("WAS", was)):
        if valor is None:
            comparacao[produto] = NOT_PROVIDED
        else:
            comparacao[produto] = {
                "database": abertos[produto],
                "console": valor,
                "database_minus_console": abertos[produto] - valor,
            }
    return comparacao


def gerar_relatorio(
    conn,
    console_vm_open: int | None = None,
    console_was_open: int | None = None,
    agora: dt.datetime | None = None,
) -> dict[str, Any]:
    _validar_console(console_vm_open, console_was_open)
    instante = agora or dt.datetime.now(dt.timezone.utc)
    if instante.tzinfo is None:
        raise ValueError("agora deve ter timezone")
    instante = instante.astimezone(dt.timezone.utc)

    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cur.execute(
            "SELECT "
            "count(*) FILTER (WHERE product = 'VM') AS vm, "
            "count(*) FILTER (WHERE product = 'WAS') AS was "
            "FROM finding_current WHERE state <> 'FIXED' AND deleted_at IS NULL"
        )
        linha = cur.fetchone()
        abertos = {
            "VM": int(linha["vm"]),
            "WAS": int(linha["was"]),
        }
        abertos["total"] = abertos["VM"] + abertos["WAS"]

        cur.execute(
            "SELECT natural_key, count(*) AS ids_distintos, "
            "array_agg(finding_id ORDER BY finding_id) AS finding_ids "
            "FROM finding_current "
            "WHERE deleted_at IS NULL AND state <> 'FIXED' "
            "GROUP BY natural_key HAVING count(*) > 1 ORDER BY natural_key"
        )
        duplicatas = [
            {
                "natural_key": linha["natural_key"],
                "ids_distintos": int(linha["ids_distintos"]),
                "finding_ids": list(linha["finding_ids"]),
            }
            for linha in cur.fetchall()
        ]

        cur.execute(
            "SELECT count(*) AS total FROM finding_current f "
            "LEFT JOIN plugin p ON p.plugin_id = f.plugin_id "
            "WHERE f.deleted_at IS NULL AND f.state <> 'FIXED' "
            "AND p.plugin_id IS NULL"
        )
        sem_plugin = int(cur.fetchone()["total"])

        cur.execute(
            "SELECT path, payload_type, attempt_count, error_message, processed_at "
            "FROM ingest_file WHERE status = 'QUARANTINED' "
            "ORDER BY processed_at DESC, path"
        )
        quarentenas = [
            {
                "path": linha["path"],
                "payload_type": linha["payload_type"],
                "attempt_count": int(linha["attempt_count"]),
                "error_message": linha["error_message"],
                "processed_at": linha["processed_at"].astimezone(
                    dt.timezone.utc
                ).isoformat(),
            }
            for linha in cur.fetchall()
        ]

    return {
        "generated_at": instante.isoformat(),
        "database": {
            "open_findings": abertos,
            "duplicate_natural_keys": duplicatas,
            "findings_without_plugin": sem_plugin,
            "quarantine": {"count": len(quarentenas), "files": quarentenas},
        },
        "console_comparison": _comparacao(
            abertos, console_vm_open, console_was_open
        ),
    }


def serializar_relatorio(relatorio: dict[str, Any]) -> str:
    return json.dumps(
        relatorio, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"


def escrever_relatorio(
    relatorio: dict[str, Any], output: str | os.PathLike[str], stdout: TextIO | None = None
) -> None:
    conteudo = serializar_relatorio(relatorio)
    if os.fspath(output) == "-":
        (stdout or sys.stdout).write(conteudo)
        return

    destino = Path(output)
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=destino.parent,
            prefix=f".{destino.name}.",
            suffix=".tmp",
            delete=False,
        ) as arquivo:
            temporario = Path(arquivo.name)
            arquivo.write(conteudo)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        os.replace(temporario, destino)
        temporario = None
    finally:
        if temporario is not None:
            temporario.unlink(missing_ok=True)
