"""derived.boards + derived.board_rows — persisted, dated draft boards (LS-30)

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "boards",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("season", sa.Integer(), nullable=False, index=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("baseline", sa.String(16), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        schema="derived",
    )
    op.create_table(
        "board_rows",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "board_id",
            sa.BigInteger(),
            sa.ForeignKey("derived.boards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("sleeper_id", sa.String(16), nullable=False),
        sa.Column("name", sa.String(128)),
        sa.Column("position", sa.String(8), nullable=False),
        sa.Column("team", sa.String(8)),
        sa.Column("injury_status", sa.String(32)),
        sa.Column("points", sa.Float(), nullable=False),
        sa.Column("baseline", sa.Float(), nullable=False),
        sa.Column("vorp", sa.Float(), nullable=False),
        sa.Column("pos_rank", sa.Integer(), nullable=False),
        sa.Column("tier", sa.Integer()),
        sa.Column("cliff", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("gap_to_next", sa.Float()),
        sa.Column("adp", sa.Float()),
        sa.Column("adp_delta", sa.Float()),
        sa.Column("adp_flag", sa.String(8)),
        sa.Column("spread", sa.Float()),
        sa.Column("disagree", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("components", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.UniqueConstraint("board_id", "rank", name="uq_board_rows_rank"),
        schema="derived",
    )
    op.create_index(
        "ix_board_rows_board_position", "board_rows", ["board_id", "position"], schema="derived"
    )


def downgrade() -> None:
    op.drop_index("ix_board_rows_board_position", table_name="board_rows", schema="derived")
    op.drop_table("board_rows", schema="derived")
    op.drop_table("boards", schema="derived")
