"""
Stubs + S3 falso para os testes rodarem sem boto3/botocore/dotenv instalados
e sem tocar a AWS. Importado automaticamente pelo pytest; o runner manual
(`python tests/test_relatorios.py`) importa via `from conftest import ...`.
"""

import json
import os
import sys
import types

# --- stubs (antes de qualquer import de tenable_core) ----------------------
if "boto3" not in sys.modules:
    _b = types.ModuleType("boto3")
    _b.client = lambda *a, **k: None
    sys.modules["boto3"] = _b

if "botocore" not in sys.modules:
    _bc = types.ModuleType("botocore")
    _cfg = types.ModuleType("botocore.config")
    _cfg.Config = object
    _exc = types.ModuleType("botocore.exceptions")

    class _BotoCoreError(Exception):
        pass

    class _ClientError(Exception):
        pass

    _exc.BotoCoreError = _BotoCoreError
    _exc.ClientError = _ClientError
    _bc.config = _cfg
    _bc.exceptions = _exc
    sys.modules["botocore"] = _bc
    sys.modules["botocore.config"] = _cfg
    sys.modules["botocore.exceptions"] = _exc

if "dotenv" not in sys.modules:
    _d = types.ModuleType("dotenv")
    _d.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = _d

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --- S3 falso -------------------------------------------------------------
class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakePaginator:
    def __init__(self, store: dict):
        self._store = store

    def paginate(self, Bucket, Prefix):
        yield {
            "Contents": [
                {"Key": k, "LastModified": None}
                for k in self._store
                if k.startswith(Prefix)
            ]
        }


class FakeS3:
    """Serve um dict {key: bytes}."""

    def __init__(self, store: dict):
        self.store = store

    def get_paginator(self, _):
        return _FakePaginator(self.store)

    def get_object(self, Bucket, Key):
        return {"Body": _FakeBody(self.store[Key])}


def payload(tipo: str, updates: list) -> bytes:
    return json.dumps(
        {
            "payload_id": "x",
            "type": tipo,
            "count_updated": len(updates),
            "count_deleted": 0,
            "updates": updates,
            "deletes": [],
        }
    ).encode("utf-8")
