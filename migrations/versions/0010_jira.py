"""Vínculo finding → ticket do Jira, e as três colunas no export.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parent.parent / "sql" / "0010_jira.sql"
SQL_ANTERIOR = Path(__file__).resolve().parent.parent / "sql" / "0009_export.sql"


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    # Recria a view sem as colunas do Jira antes de derrubar a tabela: a view
    # depende dela, e DROP TABLE falharia.
    op.execute(SQL_ANTERIOR.read_text(encoding="utf-8"))
    op.execute("DROP TABLE IF EXISTS jira_ticket")
