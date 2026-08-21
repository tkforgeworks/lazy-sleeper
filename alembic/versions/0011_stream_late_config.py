"""derived.board_config — waiver-aware K/DEF dials: stream_depth, late_rounds (LS-33)

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

COLUMNS = (
    ("stream_depth", sa.Integer(), "6"),
    ("late_rounds", sa.Integer(), "3"),
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
