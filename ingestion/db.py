"""Conexão, lock de concorrência e carregamento dos .sql.

Regra de divisão de trabalho do projeto (seção 17.2): **Python orquestra, SQL
move dado.** O diff nunca vive em Python — não se carrega 500 mil findings em
memória para comparar. Por isso o SQL fica em arquivo, versionado e legível no
diff do git, e não em f-string no meio do código.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

from .erros import ErroConfiguracao

log = logging.getLogger(__name__)

DIRETORIO_SQL = Path(__file__).parent / "sql"
DIRETORIO_MIGRACOES = Path(__file__).parent.parent / "migrations" / "sql"

# Nome do lock global do pipeline (seção 11).
NOME_LOCK = "tenable_ingestion"


def _psycopg():
    """Import tardio: os testes de parsing não precisam de driver de banco, e
    o pacote precisa importar numa máquina sem PostgreSQL."""
    try:
        import psycopg
    except ModuleNotFoundError as erro:  # pragma: no cover - ambiente sem driver
        raise ErroConfiguracao(
            "psycopg (v3) não está instalado — `pip install -r requirements.txt`"
        ) from erro
    return psycopg


@lru_cache(maxsize=None)
def carregar_sql(nome: str) -> str:
    """Conteúdo de ingestion/sql/<nome>.sql."""
    caminho = DIRETORIO_SQL / f"{nome}.sql"
    if not caminho.is_file():
        raise ErroConfiguracao(f"SQL não encontrado: {caminho}")
    return caminho.read_text(encoding="utf-8")


def conectar(dsn: str, autocommit: bool = True):
    """Conexão psycopg3.

    `autocommit=True` por padrão e transação SEMPRE explícita via
    `conn.transaction()`. Com autocommit desligado o psycopg abre uma transação
    implícita na primeira consulta, e aí `conn.transaction()` viraria um
    SAVEPOINT aninhado: as temporárias `ON COMMIT DROP` sobreviveriam de um
    payload para o outro e nada seria confirmado no meio do ciclo. A fronteira
    "uma transação por payload" (seção 12.1) depende deste ajuste.

    `row_factory=dict_row` porque quase toda leitura aqui é para log ou
    métrica, e nome de coluna é mais legível que índice."""
    psycopg = _psycopg()
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, autocommit=autocommit, row_factory=dict_row)


def jsonb(valor: Any):
    """Embrulha um dict para ir a uma coluna jsonb via COPY."""
    from psycopg.types.json import Jsonb

    return Jsonb(valor)


@contextmanager
def travar_pipeline(conn) -> Iterator[bool]:
    """Advisory lock de sessão — a rede de segurança do `concurrencyPolicy:
    Forbid` do CronJob (seção 11).

    Se a carga demorar mais que o intervalo do cron, duas execuções tentariam
    processar o mesmo manifest. Devolve False quando outra execução já tem o
    lock; o chamador sai com código 0 e um log, não com erro."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS obtido", (NOME_LOCK,))
        obtido = bool(cur.fetchone()["obtido"])
    try:
        yield obtido
    finally:
        if obtido:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (NOME_LOCK,))


# ===========================================================================
# Migrações
# ===========================================================================
DDL_CONTROLE_MIGRACAO = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def arquivos_de_migracao() -> list[Path]:
    if not DIRETORIO_MIGRACOES.is_dir():
        raise ErroConfiguracao(f"diretório de migrações não encontrado: {DIRETORIO_MIGRACOES}")
    return sorted(DIRETORIO_MIGRACOES.glob("*.sql"))


def aplicar_migracoes(conn) -> list[str]:
    """Aplica os .sql de migrations/sql/ que ainda não rodaram.

    Mesmo conteúdo que as revisões Alembic executam — os .sql são a única fonte
    de verdade do DDL. Este caminho existe para subir banco local e para os
    testes; produção usa Alembic."""
    aplicadas: list[str] = []
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(DDL_CONTROLE_MIGRACAO)
            cur.execute("SELECT version FROM schema_migration")
            ja_aplicadas = {linha["version"] for linha in cur.fetchall()}

            for caminho in arquivos_de_migracao():
                versao = caminho.stem
                if versao in ja_aplicadas:
                    continue
                log.info("aplicando migração %s", versao)
                cur.execute(caminho.read_text(encoding="utf-8"))
                cur.execute(
                    "INSERT INTO schema_migration (version) VALUES (%s)", (versao,)
                )
                aplicadas.append(versao)
    return aplicadas
