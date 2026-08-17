"""core.projections (per-snapshot vintages), core.actuals (facts, latest wins), core.adp

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


def _stat_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "snapshot_id", sa.BigInteger(), sa.ForeignKey("raw.snapshots.id"), nullable=False
        ),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("week", sa.Integer()),
        sa.Column("source_player_id", sa.String(32), nullable=False),
        sa.Column("sleeper_id", sa.String(16)),
        sa.Column("position", sa.String(8)),
        sa.Column("team", sa.String(8)),
        sa.Column("gp", sa.Float()),
        sa.Column("provider_points", sa.Float()),
        sa.Column("stats", postgresql.JSONB(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "projections",
        *_stat_columns(),
        sa.UniqueConstraint(
            "snapshot_id",
            "source_player_id",
            "season",
            "week",
            name="uq_projection_identity",
            postgresql_nulls_not_distinct=True,
        ),
        schema="core",
    )
    op.create_index("ix_core_projections_sleeper_id", "projections", ["sleeper_id"], schema="core")
    op.create_index(
        "ix_core_projections_snapshot_id", "projections", ["snapshot_id"], schema="core"
    )
    op.create_index(
        "ix_core_projections_lookup", "projections", ["source", "season", "week"], schema="core"
    )

    op.create_table(
        "actuals",
        *_stat_columns(),
        sa.UniqueConstraint(
            "source",
            "season",
            "week",
            "source_player_id",
            name="uq_actual_identity",
            postgresql_nulls_not_distinct=True,
        ),
        schema="core",
    )
    op.create_index("ix_core_actuals_sleeper_id", "actuals", ["sleeper_id"], schema="core")
    op.create_index(
        "ix_core_actuals_lookup", "actuals", ["source", "season", "week"], schema="core"
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
    op.drop_table("actuals", schema="core")
    op.drop_table("projections", schema="core")
