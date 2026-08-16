"""core.stat_lines (projections + actuals, Sleeper stat vocabulary) and core.adp

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stat_lines",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "snapshot_id", sa.BigInteger(), sa.ForeignKey("raw.snapshots.id"), nullable=False
        ),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("category", sa.String(8), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("week", sa.Integer()),
        sa.Column("source_player_id", sa.String(32), nullable=False),
        sa.Column("sleeper_id", sa.String(16)),
        sa.Column("position", sa.String(8)),
        sa.Column("team", sa.String(8)),
        sa.Column("gp", sa.Float()),
        sa.Column("provider_points", sa.Float()),
        sa.Column("stats", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint(
            "snapshot_id",
            "source_player_id",
            "category",
            "season",
            "week",
            name="uq_stat_line_identity",
            postgresql_nulls_not_distinct=True,
        ),
        schema="core",
    )
    op.create_index("ix_core_stat_lines_sleeper_id", "stat_lines", ["sleeper_id"], schema="core")
    op.create_index("ix_core_stat_lines_snapshot_id", "stat_lines", ["snapshot_id"], schema="core")
    op.create_index(
        "ix_core_stat_lines_lookup",
        "stat_lines",
        ["source", "category", "season", "week"],
        schema="core",
    )

    op.create_table(
        "adp",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "snapshot_id", sa.BigInteger(), sa.ForeignKey("raw.snapshots.id"), nullable=False
        ),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("sleeper_id", sa.String(16), nullable=False),
        sa.Column("position", sa.String(8)),
        sa.Column("adp_ppr", sa.Float()),
        sa.Column("adp_half_ppr", sa.Float()),
        sa.Column("adp_std", sa.Float()),
        sa.Column("adp_2qb", sa.Float()),
        sa.Column("adp_dynasty", sa.Float()),
        sa.Column("adp_dynasty_ppr", sa.Float()),
        sa.Column("adp_rookie", sa.Float()),
        sa.Column("adp_idp", sa.Float()),
        sa.UniqueConstraint("snapshot_id", "sleeper_id", name="uq_adp_identity"),
        schema="core",
    )
    op.create_index("ix_core_adp_sleeper_id", "adp", ["sleeper_id"], schema="core")
    op.create_index("ix_core_adp_season", "adp", ["season", "snapshot_id"], schema="core")


def downgrade() -> None:
    op.drop_table("adp", schema="core")
    op.drop_table("stat_lines", schema="core")
