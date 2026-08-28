"""Schema inicial do pipeline de ingestão (SPEC seção 5.2).

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parent.parent / "sql" / "0001_schema.sql"

# Ordem inversa da criação: as dependentes primeiro.
TABELAS = (
    "finding_risk",
    "finding_recast",
    "finding_event",       # derruba as partições junto
    "finding_current",
    "plugin",
    "pipeline_control",
    "ingest_file",
)


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    for tabela in TABELAS:
        op.execute(f"DROP TABLE IF EXISTS {tabela} CASCADE")
