"""Construtores de payload, manifest e S3 falso para os testes.

Os payloads de exemplo **reais** ficam em `samples_s3/` (baixados do bucket por
`gerar_exemplos_s3_datastram.py`). Este módulo os carrega e os deforma de
propósito para exercitar cada regra — em vez de duplicar os JSON aqui e
deixá-los envelhecer em separado.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parent.parent.parent
SAMPLES = RAIZ / "samples_s3"

PREFIXO = "prod"


def carregar_sample(nome: str) -> dict[str, Any]:
    return json.loads((SAMPLES / nome).read_text(encoding="utf-8"))


def finding_vm(**substituicoes: Any) -> dict[str, Any]:
    registro = copy.deepcopy(carregar_sample("exemplo_vm_finding.json")["updates"][0])
    registro.update(substituicoes)
    return registro


def finding_was(**substituicoes: Any) -> dict[str, Any]:
    registro = copy.deepcopy(carregar_sample("exemplo_was_finding.json")["updates"][0])
    registro.update(substituicoes)
    return registro


def enriched(**substituicoes: Any) -> dict[str, Any]:
    registro = copy.deepcopy(
        carregar_sample("exemplo_finding_enriched.json")["updates"][0]
    )
    propriedades = registro.setdefault("recast_properties", {})
    anotacao = propriedades.setdefault("recast_annotation", {})
    for chave, valor in substituicoes.items():
        if chave in {"finding_id", "source"}:
            propriedades[chave] = valor
        else:
            anotacao[chave] = valor
    return registro


def envelope(
    tipo: str,
    updates: list[dict[str, Any]] | None = None,
    deletes: list[dict[str, Any]] | None = None,
    version: int = 1,
) -> dict[str, Any]:
    updates = updates or []
    deletes = deletes or []
    return {
        "payload_id": f"{tipo.lower()}-teste",
        "version": version,
        "type": tipo,
        "count_updated": len(updates),
        "count_deleted": len(deletes),
        "updates": updates,
        "deletes": deletes,
        "first_ts": "1787826739356",
        "last_ts": "1787826739356",
    }


def comprimir(doc: dict[str, Any]) -> bytes:
    return gzip.compress(json.dumps(doc).encode("utf-8"))


def md5(dados: bytes) -> str:
    return hashlib.md5(dados).hexdigest()  # noqa: S324


class Bucket:
    """Acumula manifests e payloads e devolve o `store` que o S3 falso serve.

    `adicionar` já calcula md5 e contagens, então um teste só quebra por md5
    quando o teste quiser que ele quebre."""

    def __init__(self, prefixo: str = PREFIXO):
        self.prefixo = prefixo
        self.store: dict[str, bytes] = {}
        self._entradas: dict[str, list[dict[str, Any]]] = {}
        self._sequencia = 0

    def adicionar(
        self,
        payload_type: str,
        doc: dict[str, Any],
        *,
        md5_forcado: str | None = None,
        scan_id: str | None = None,
        last_record_ms: int = 1787826739356,
    ) -> str:
        diretorio = {
            "FINDING": "finding",
            "WAS_FINDING": "was_finding",
            "FINDING_ENRICHED_ATTRIBUTES": "finding_enriched_attributes",
        }[payload_type]
        self._sequencia += 1
        # o nome carrega o epoch: ordem alfabética = ordem cronológica
        path = (
            f"{self.prefixo}/{diretorio}/2026-08-27/"
            f"{diretorio}-{1787826742410 + self._sequencia:013d}.json.gz"
        )
        dados = comprimir(doc)
        self.store[path] = dados
        self._entradas.setdefault(payload_type, []).append(
            {
                "path": path,
                "md5": md5_forcado or md5(dados),
                "version": doc.get("version", 1),
                "num_updates": len(doc.get("updates") or []),
                "num_deletes": len(doc.get("deletes") or []),
                "first_record_timestamp": last_record_ms,
                "last_record_timestamp": last_record_ms,
                "scan_id": scan_id or "",
            }
        )
        return path

    def fechar_manifest(self, payload_type: str) -> str | None:
        """Fecha a janela: gera o manifest com o que foi adicionado desde o
        último fechamento, na ordem de inserção."""
        entradas = self._entradas.pop(payload_type, None)
        if not entradas:
            return None
        diretorio = {
            "FINDING": "manifest_finding",
            "WAS_FINDING": "manifest_was_finding",
            "FINDING_ENRICHED_ATTRIBUTES": "manifest_finding_enriched_attributes",
        }[payload_type]
        self._sequencia += 1
        path = (
            f"{self.prefixo}/{diretorio}/2026-08-27/"
            f"{diretorio}-{1787826742410 + self._sequencia:013d}.json"
        )
        doc = {
            "type": f"MANIFEST_{payload_type}",
            "payload_type": payload_type,
            "payloads": entradas,
        }
        self.store[path] = json.dumps(doc).encode("utf-8")
        return path

    def fechar(self) -> dict[str, bytes]:
        for payload_type in list(self._entradas):
            self.fechar_manifest(payload_type)
        return self.store


class FakeS3:
    """Serve um dict {key: bytes}, com a mesma superfície que o loader usa."""

    def __init__(self, store: dict[str, bytes]):
        self.store = store

    def get_paginator(self, _nome: str):
        return _Paginator(self.store)

    def get_object(self, Bucket: str, Key: str):  # noqa: N803 - assinatura do boto3
        if Key not in self.store:
            raise KeyError(Key)
        return {"Body": _Corpo(self.store[Key])}


class _Corpo:
    def __init__(self, dados: bytes):
        self._dados = dados

    def read(self) -> bytes:
        return self._dados


class _Paginator:
    def __init__(self, store: dict[str, bytes]):
        self._store = store

    def paginate(self, Bucket: str, Prefix: str):  # noqa: N803 - assinatura do boto3
        import datetime as dt

        agora = dt.datetime.now(dt.timezone.utc)
        yield {
            "Contents": [
                {"Key": chave, "LastModified": agora}
                for chave in sorted(self._store)
                if chave.startswith(Prefix)
            ]
        }
