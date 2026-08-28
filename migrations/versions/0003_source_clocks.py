"""Relógios persistentes de plugin e recast.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parent.parent / "sql" / "0003_source_clocks.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("ALTER TABLE finding_recast DROP COLUMN IF EXISTS source_indexed")
    op.execute("ALTER TABLE plugin DROP COLUMN IF EXISTS source_indexed")
