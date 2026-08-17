"""core.snap_counts (nflverse) and core.expected_points (ffverse xFP)

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "snap_counts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "snapshot_id", sa.BigInteger(), sa.ForeignKey("raw.snapshots.id"), nullable=False
        ),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("week", sa.Integer(), nullable=False),
        sa.Column("pfr_player_id", sa.String(16), nullable=False),
        sa.Column("sleeper_id", sa.String(16)),
        sa.Column("player", sa.String(128)),
        sa.Column("position", sa.String(8)),
        sa.Column("team", sa.String(8)),
        sa.Column("opponent", sa.String(8)),
        sa.Column("offense_snaps", sa.Integer()),
        sa.Column("offense_pct", sa.Float()),
        sa.Column("defense_snaps", sa.Integer()),
        sa.Column("defense_pct", sa.Float()),
        sa.Column("st_snaps", sa.Integer()),
        sa.Column("st_pct", sa.Float()),
        sa.UniqueConstraint("season", "week", "pfr_player_id", name="uq_snap_count_identity"),
        schema="core",
    )
    op.create_index("ix_core_snap_counts_sleeper_id", "snap_counts", ["sleeper_id"], schema="core")
    op.create_index(
        "ix_core_snap_counts_season_week", "snap_counts", ["season", "week"], schema="core"
    )

    op.create_table(
        "expected_points",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "snapshot_id", sa.BigInteger(), sa.ForeignKey("raw.snapshots.id"), nullable=False
        ),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("week", sa.Integer(), nullable=False),
        sa.Column("gsis_id", sa.String(16), nullable=False),
        sa.Column("sleeper_id", sa.String(16)),
        sa.Column("full_name", sa.String(128)),
        sa.Column("position", sa.String(8)),
        sa.Column("team", sa.String(8)),
        sa.Column("total_fantasy_points", sa.Float()),
        sa.Column("total_fantasy_points_exp", sa.Float()),
        sa.Column("ep", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("season", "week", "gsis_id", name="uq_expected_points_identity"),
        schema="core",
    )
    op.create_index(
        "ix_core_expected_points_sleeper_id", "expected_points", ["sleeper_id"], schema="core"
    )
    op.create_index(
        "ix_core_expected_points_season_week", "expected_points", ["season", "week"], schema="core"
    )


def downgrade() -> None:
    op.drop_table("expected_points", schema="core")
    op.drop_table("snap_counts", schema="core")
