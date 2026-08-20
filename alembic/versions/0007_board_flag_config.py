"""derived.board_config — ADP-delta and disagreement thresholds (LS-29)

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

COLUMNS = (
    ("adp_min_delta", sa.Float(), "12.0"),
    ("adp_pct", sa.Float(), "0.25"),
    ("disagree_min_pts", sa.Float(), "20.0"),
    ("disagree_pct", sa.Float(), "0.15"),
    ("debias_disagreement", sa.Boolean(), "true"),
)


def upgrade() -> None:
    for name, type_, default in COLUMNS:
        op.add_column(
            "board_config",
            sa.Column(name, type_, nullable=False, server_default=sa.text(default)),
            schema="derived",
        )


def downgrade() -> None:
    for name, _, _ in reversed(COLUMNS):
        op.drop_column("board_config", name, schema="derived")
