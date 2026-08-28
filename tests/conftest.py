"""
Stubs + S3 falso para os testes rodarem sem boto3/botocore/dotenv instalados
e sem tocar a AWS. Importado automaticamente pelo pytest; o runner manual
(`python tests/test_relatorios.py`) importa via `from conftest import ...`.

A partir daqui há também as fixtures do pipeline de ingestão. As de banco só
ligam quando `TEST_PG_DSN` aponta para um PostgreSQL **descartável** — elas
truncam as tabelas entre os testes.
"""

import json
import os
import sys
import types
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

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


# ===========================================================================
# Fixtures do pipeline de ingestão
# ===========================================================================
TABELAS_DE_TESTE = (
    "finding_risk",
    "finding_event",
    "finding_recast",
    "finding_current",
    "plugin",
    "ingest_file",
)


def _dsn_de_teste():
    return os.getenv("TEST_PG_DSN") or ""


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "banco: precisa de PostgreSQL (TEST_PG_DSN)"
    )


try:
    import pytest
except ModuleNotFoundError:  # runner manual do exportador, sem pytest
    pytest = None


if pytest is not None:

    @pytest.fixture(scope="session")
    def dsn():
        if not _dsn_de_teste():
            pytest.skip(
                "TEST_PG_DSN não definido — testes de banco pulados. "
                "Ex.: docker run -e POSTGRES_PASSWORD=x -p 5432:5432 postgres:16"
            )
        pytest.importorskip("psycopg", reason="psycopg (v3) não instalado")
        return _dsn_de_teste()

    @pytest.fixture(scope="session")
    def schema(dsn):
        """Aplica as migrações uma vez por sessão."""
        from ingestion.db import aplicar_migracoes, conectar

        with conectar(dsn) as conn:
            aplicar_migracoes(conn)
        return dsn

    @pytest.fixture
    def conn(schema):
        """Conexão limpa: trunca as tabelas antes de cada teste.

        `pipeline_control` não entra no TRUNCATE — a linha única dela é parte
        do schema, não dado de teste."""
        from ingestion.db import conectar

        with conectar(schema) as conexao:
            with conexao.transaction(), conexao.cursor() as cur:
                cur.execute(
                    "TRUNCATE " + ", ".join(TABELAS_DE_TESTE) + " RESTART IDENTITY CASCADE"
                )
                cur.execute("UPDATE pipeline_control SET mode='SEED', cutoff_at=NULL WHERE id=1")
            yield conexao

    @pytest.fixture
    def config_teste(dsn):
        from ingestion.config import Config

        return Config(bucket="bucket-teste", prefixo="prod", pg_dsn=dsn, max_attempts=3)

    @pytest.fixture
    def ingestor(config_teste, conn):
        """Fábrica: recebe o `store` do S3 falso e devolve um Ingestor pronto."""
        from ingestion.loader import Ingestor
        from ingestion.s3 import ClienteS3

        from fixtures import FakeS3

        def _montar(store, modo=None):
            cliente = ClienteS3(config_teste, cliente=FakeS3(store))
            return Ingestor(config_teste, cliente, conn)

        return _montar
