"""core.drafts / draft_picks / rosters / league_users — parsed live-draft state (LS-16)

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _provenance() -> list[sa.Column]:
    return [
        sa.Column("snapshot_id", sa.BigInteger(), sa.ForeignKey("raw.snapshots.id")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "drafts",
        sa.Column("draft_id", sa.String(32), primary_key=True),
        sa.Column("league_id", sa.String(32), index=True),
        sa.Column("season", sa.Integer()),
        sa.Column("type", sa.String(16)),
        sa.Column("status", sa.String(16)),
        sa.Column("start_time", sa.BigInteger()),
        sa.Column("last_picked", sa.BigInteger()),
        sa.Column("rounds", sa.Integer()),
        sa.Column("teams", sa.Integer()),
        sa.Column("pick_timer", sa.Integer()),
        sa.Column("settings", JSONB),
        sa.Column("metadata", JSONB),
        sa.Column("slot_to_roster_id", JSONB),
        sa.Column("draft_order", JSONB),
        *_provenance(),
        schema="core",
    )
    op.create_table(
        "draft_picks",
        sa.Column("draft_id", sa.String(32), primary_key=True),
        sa.Column("pick_no", sa.Integer(), primary_key=True),
        sa.Column("round", sa.Integer()),
        sa.Column("draft_slot", sa.Integer()),
        sa.Column("roster_id", sa.Integer()),
        sa.Column("picked_by", sa.String(32)),
        sa.Column("sleeper_id", sa.String(16)),
        sa.Column("is_keeper", sa.Boolean()),
        sa.Column("metadata", JSONB),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        *_provenance(),
        schema="core",
    )
    op.create_index(
        "ix_draft_picks_draft_player", "draft_picks", ["draft_id", "sleeper_id"], schema="core"
    )
    op.create_table(
        "rosters",
        sa.Column("league_id", sa.String(32), primary_key=True),
        sa.Column("roster_id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.String(32), index=True),
        sa.Column("co_owners", JSONB),
        sa.Column("players", JSONB),
        sa.Column("starters", JSONB),
        sa.Column("reserve", JSONB),
        sa.Column("taxi", JSONB),
        sa.Column("keepers", JSONB),
        sa.Column("settings", JSONB),
        *_provenance(),
        schema="core",
    )
    op.create_table(
        "league_users",
        sa.Column("league_id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), primary_key=True),
        sa.Column("display_name", sa.String(64)),
        sa.Column("team_name", sa.String(128)),
        sa.Column("avatar", sa.String(64)),
        sa.Column("is_owner", sa.Boolean()),
        *_provenance(),
        schema="core",
    )


def downgrade() -> None:
    op.drop_table("league_users", schema="core")
    op.drop_table("rosters", schema="core")
    op.drop_index("ix_draft_picks_draft_player", table_name="draft_picks", schema="core")
    op.drop_table("draft_picks", schema="core")
    op.drop_table("drafts", schema="core")
