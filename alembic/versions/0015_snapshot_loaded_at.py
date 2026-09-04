"""raw.snapshots.loaded_at — explicit "this snapshot has been loaded" stamp

`lazy load stats` used to treat a snapshot as loaded when some core.* row still carried its
`snapshot_id`. Since latest-wins projections (0013) and the post-kickoff freeze, a pull that
changed nothing leaves no row pointing at it, so ~40 snapshots were re-downloaded from Storage
and re-processed on every daily run (11 min, and IO the nano instance could not spare).
Snapshots referenced by a core table are back-filled as loaded; the rest are loaded one more
time by the next `lazy load stats` and stamped then.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

BACKFILL = """
UPDATE raw.snapshots s
SET loaded_at = coalesce(s.last_seen_at, s.pulled_at)
WHERE s.id IN (
    SELECT snapshot_id FROM core.projections
    UNION SELECT snapshot_id FROM core.actuals
    UNION SELECT snapshot_id FROM core.snap_counts
    UNION SELECT snapshot_id FROM core.expected_points
)
"""


def upgrade() -> None:
    op.add_column("snapshots", sa.Column("loaded_at", sa.DateTime(timezone=True)), schema="raw")
    op.execute(BACKFILL)


def downgrade() -> None:
    op.drop_column("snapshots", "loaded_at", schema="raw")
