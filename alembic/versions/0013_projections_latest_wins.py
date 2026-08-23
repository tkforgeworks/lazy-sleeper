"""core.projections → one row per (source, source_player_id, season, week), latest wins (LS-53)

Collapses the accumulated per-pull vintages down to the newest snapshot per (source, season,
week) — exactly the rows every consumer selected via ``max(snapshot_id)`` before this change,
so benchmark results are preserved — then swaps the unique constraint from the per-vintage key
(snapshot_id, ...) to the per-player key. The constraint keeps its name so the loader's
``on_conflict`` target is unchanged. Raw snapshots remain the full vintage history.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-23
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

COLLAPSE = """
DELETE FROM core.projections p
USING (
    SELECT source, season, week, max(snapshot_id) AS keep
    FROM core.projections
    GROUP BY source, season, week
) k
WHERE p.source = k.source
  AND p.season = k.season
  AND p.week IS NOT DISTINCT FROM k.week
  AND p.snapshot_id <> k.keep
"""


def upgrade() -> None:
    op.execute(COLLAPSE)
    op.drop_constraint("uq_projection_identity", "projections", schema="core")
    op.create_unique_constraint(
        "uq_projection_identity",
        "projections",
        ["source", "source_player_id", "season", "week"],
        schema="core",
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    # The deleted vintages are not recoverable from the DB (raw snapshots hold them); this only
    # restores the old constraint shape.
    op.drop_constraint("uq_projection_identity", "projections", schema="core")
    op.create_unique_constraint(
        "uq_projection_identity",
        "projections",
        ["snapshot_id", "source_player_id", "season", "week"],
        schema="core",
        postgresql_nulls_not_distinct=True,
    )
