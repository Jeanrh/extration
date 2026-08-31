"""Unidade de negócio e tribo no lugar de domínio/subdomínio.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parent.parent / "sql" / "0008_unidade_negocio.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("ALTER TABLE finding_risk DROP COLUMN IF EXISTS tribo")
    op.execute("ALTER TABLE cmdb_acronym DROP COLUMN IF EXISTS tribo")
    op.execute("ALTER TABLE cmdb_acronym DROP COLUMN IF EXISTS unidade_negocio")
    op.execute("ALTER TABLE cmdb_acronym ADD COLUMN IF NOT EXISTS equipe_sol text")
    op.execute("ALTER TABLE cmdb_acronym ADD COLUMN IF NOT EXISTS subdomain text")
    op.execute("ALTER TABLE cmdb_acronym ADD COLUMN IF NOT EXISTS domain text")
    op.execute(
        "CREATE TABLE IF NOT EXISTS cmdb_team ("
        " nome text PRIMARY KEY, tribo text, alianca text, vp text)"
    )
