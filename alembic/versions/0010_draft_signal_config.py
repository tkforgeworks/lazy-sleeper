"""derived.board_config — survival / demand / run dials for draft-time signals (LS-33)

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

COLUMNS = (
    ("survival_sigma_min", sa.Float(), "4.0"),
    ("survival_sigma_pct", sa.Float(), "0.12"),
    ("demand_shift", sa.Float(), "0.5"),
    ("need_bonus", sa.Float(), "8.0"),
    ("run_window", sa.Integer(), "8"),
    ("run_threshold", sa.Integer(), "4"),
    ("run_streak", sa.Integer(), "3"),
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
