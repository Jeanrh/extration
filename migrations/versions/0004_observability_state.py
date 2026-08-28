"""Estado persistente para métricas de observabilidade.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parent.parent / "sql" / "0004_observability_state.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute(
        "ALTER TABLE pipeline_control DROP COLUMN IF EXISTS last_findings_open"
    )
