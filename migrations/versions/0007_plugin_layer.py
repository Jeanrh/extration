"""Camada tecnológica derivada por plugin.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parent.parent / "sql" / "0007_plugin_layer.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS plugin_layer")
