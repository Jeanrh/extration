"""Views de consumo — o contrato de leitura (SPEC seção 16).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parent.parent / "sql" / "0002_views.sql"

VIEWS = (
    "vw_duplicata_natural_key",
    "vw_pipeline_saude",
    "vw_tendencia_mensal",
    "vw_evento_diario",
    "vw_finding_ativo",
)


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    for view in VIEWS:
        op.execute(f"DROP VIEW IF EXISTS {view}")
