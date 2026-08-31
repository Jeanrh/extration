"""Equipe solucionadora e a view de export dos times.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parent.parent / "sql" / "0009_export.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS vw_finding_export")
    op.execute("ALTER TABLE finding_risk DROP COLUMN IF EXISTS equipe_solucionadora")
    op.execute("ALTER TABLE cmdb_acronym DROP COLUMN IF EXISTS equipe_solucionadora")
