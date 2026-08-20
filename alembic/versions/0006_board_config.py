"""derived.board_config — tier/cliff thresholds, adjustable from the app (LS-28)

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "board_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cliff_gap", sa.Float(), nullable=False),
        sa.Column("gap_multiplier", sa.Float(), nullable=False),
        sa.Column("min_gap", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="derived",
    )
    op.execute(
        "INSERT INTO derived.board_config (id, cliff_gap, gap_multiplier, min_gap, updated_at) "
        "VALUES (1, 15.0, 2.0, 4.0, now())"
    )


def downgrade() -> None:
    op.drop_table("board_config", schema="derived")
