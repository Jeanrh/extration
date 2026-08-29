"""Motor de risco: veredito completo em finding_risk, evento RISK_CHANGED e
promoção de plugin.exploitability_ease.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parent.parent / "sql" / "0005_risk_engine.sql"

COLUNAS_RISCO = (
    "priority_id", "priority_name", "sla_status", "aging",
    "nota_bia", "nota_pci", "nota_exposure", "nota_arch",
    "nota_cvss", "nota_threat", "nota_exploit", "nota_layer",
    "sigla", "pci", "bia", "criticality_cmdb", "unidade_negocio",
    "arch_type", "layer", "familia", "context_synced_at",
)


def upgrade() -> None:
    op.execute(SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    for coluna in COLUNAS_RISCO:
        op.execute(f"ALTER TABLE finding_risk DROP COLUMN IF EXISTS {coluna}")
    op.execute("ALTER TABLE plugin DROP COLUMN IF EXISTS exploitability_ease")
    op.execute("ALTER TABLE finding_event DROP CONSTRAINT IF EXISTS finding_event_type_ck")
    op.execute(
        "ALTER TABLE finding_event ADD CONSTRAINT finding_event_type_ck CHECK ("
        "event_type IN ('OPENED', 'REOPENED', 'FIXED', 'DELETED', 'RECAST_CHANGED'))"
    )
