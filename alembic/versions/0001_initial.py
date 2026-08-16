"""initial: raw.snapshots, core.players, core.crosswalk

Revision ID: 0001
Revises:
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS raw")
    op.execute("CREATE SCHEMA IF NOT EXISTS core")
    op.execute("CREATE SCHEMA IF NOT EXISTS derived")

    op.create_table(
        "snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("season", sa.Integer()),
        sa.Column("week", sa.Integer()),
        sa.Column("pulled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("remote_path", sa.Text()),
        sa.Column("record_count", sa.Integer()),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
        sa.Column("valid", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("validation_notes", sa.Text()),
        sa.Column("meta", postgresql.JSONB()),
        sa.UniqueConstraint(
            "source", "kind", "season", "week", "pulled_at", name="uq_snapshot_identity"
        ),
        schema="raw",
    )
    op.create_index("ix_raw_snapshots_sha256", "snapshots", ["sha256"], schema="raw")
    op.create_index(
        "ix_raw_snapshots_lookup", "snapshots", ["source", "kind", "season", "week"], schema="raw"
    )

    op.create_table(
        "players",
        sa.Column("sleeper_id", sa.String(16), primary_key=True),
        sa.Column("full_name", sa.String(128)),
        sa.Column("position", sa.String(8)),
        sa.Column("team", sa.String(8)),
        sa.Column("status", sa.String(32)),
        sa.Column("injury_status", sa.String(32)),
        sa.Column("depth_chart_order", sa.Integer()),
        sa.Column("search_rank", sa.Integer()),
        sa.Column("years_exp", sa.Integer()),
        sa.Column("age", sa.Integer()),
        sa.Column("team_changed_at", sa.BigInteger()),
        sa.Column("sportradar_id", sa.String(64)),
        sa.Column("espn_id", sa.String(32)),
        sa.Column("gsis_id", sa.String(32)),
        sa.Column("yahoo_id", sa.String(32)),
        sa.Column("active", sa.Boolean()),
        sa.Column("snapshot_id", sa.BigInteger(), sa.ForeignKey("raw.snapshots.id")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="core",
    )
    op.create_index("ix_core_players_position", "players", ["position"], schema="core")
    op.create_index("ix_core_players_team", "players", ["team"], schema="core")
    op.create_index("ix_core_players_sportradar_id", "players", ["sportradar_id"], schema="core")

    op.create_table(
        "crosswalk",
        sa.Column("sleeper_id", sa.String(16), primary_key=True),
        sa.Column("sportradar_id", sa.String(64)),
        sa.Column("gsis_id", sa.String(32)),
        sa.Column("espn_id", sa.String(32)),
        sa.Column("pfr_id", sa.String(32)),
        sa.Column("mfl_id", sa.String(32)),
        sa.Column("name", sa.String(128)),
        sa.Column("merge_name", sa.String(128)),
        sa.Column("position", sa.String(8)),
        sa.Column("snapshot_id", sa.BigInteger(), sa.ForeignKey("raw.snapshots.id")),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        schema="core",
    )
    op.create_index(
        "ix_core_crosswalk_sportradar_id", "crosswalk", ["sportradar_id"], schema="core"
    )
    op.create_index("ix_core_crosswalk_gsis_id", "crosswalk", ["gsis_id"], schema="core")
    op.create_index("ix_core_crosswalk_espn_id", "crosswalk", ["espn_id"], schema="core")


def downgrade() -> None:
    op.drop_table("crosswalk", schema="core")
    op.drop_table("players", schema="core")
    op.drop_table("snapshots", schema="raw")
    op.execute("DROP SCHEMA IF EXISTS derived")
    op.execute("DROP SCHEMA IF EXISTS core")
    op.execute("DROP SCHEMA IF EXISTS raw")
