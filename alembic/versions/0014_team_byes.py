"""core.team_byes + derived.board_rows.bye (LS-57)

One row per (season, team) with the bye week, loaded from ESPN's pro-team doc
(`lazy pull byes --load`), and a nullable `bye` on persisted board rows so `/board`
carries it without a client-side join. Existing boards keep NULL until the next regen.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "team_byes",
        sa.Column("season", sa.Integer(), primary_key=True),
        sa.Column("team", sa.String(8), primary_key=True),
        sa.Column("bye_week", sa.Integer(), nullable=False),
        sa.Column("espn_id", sa.Integer()),
        sa.Column("espn_abbrev", sa.String(8)),
        sa.Column("snapshot_id", sa.BigInteger(), sa.ForeignKey("raw.snapshots.id")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="core",
    )
    op.add_column("board_rows", sa.Column("bye", sa.Integer()), schema="derived")


def downgrade() -> None:
    op.drop_column("board_rows", "bye", schema="derived")
    op.drop_table("team_byes", schema="core")
