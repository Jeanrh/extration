"""Contexto externo do motor de risco: CMDB, arquitetura e threat intel.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parent.parent / "sql" / "0006_risk_context.sql"

TABELAS = (
    "context_sync", "threat_intel", "architecture",
    "cmdb_team", "cmdb_url", "cmdb_server", "cmdb_acronym",
)


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    for tabela in TABELAS:
        op.execute(f"DROP TABLE IF EXISTS {tabela}")
