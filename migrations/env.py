"""Ambiente do Alembic.

Não há metadata declarativa aqui e isso é proposital: o schema é escrito à mão
em migrations/sql/*.sql, porque o DDL da SPEC usa particionamento declarativo,
partição DEFAULT e índice único parcial — coisas que autogeração de ORM não
reproduz fielmente. SQLAlchemy/Alembic ficam só no schema, nunca no caminho
quente da carga (seção 17.2).
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _url() -> str:
    """PG_DSN é a mesma variável que o job usa; alembic.ini não guarda senha."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ModuleNotFoundError:
        pass

    dsn = os.getenv("PG_DSN")
    if not dsn:
        raise RuntimeError("PG_DSN não definido — o Alembic precisa dele para conectar")
    if dsn.startswith("postgresql://"):
        # SQLAlchemy usa psycopg2 por padrão para `postgresql://`, mas este
        # projeto instala e usa psycopg v3 em todos os caminhos.
        return "postgresql+psycopg://" + dsn.removeprefix("postgresql://")
    if dsn.startswith("postgres://"):
        return "postgresql+psycopg://" + dsn.removeprefix("postgres://")
    if "://" in dsn:
        return dsn
    # DSN em formato libpq ("host=... dbname=...") → URL do SQLAlchemy
    return "postgresql+psycopg://?" + "&".join(
        parte.replace("=", "=", 1) for parte in dsn.split()
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_url(), target_metadata=None, literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    secao = config.get_section(config.config_ini_section) or {}
    secao["sqlalchemy.url"] = _url()
    engine = engine_from_config(secao, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as conexao:
        context.configure(connection=conexao, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
