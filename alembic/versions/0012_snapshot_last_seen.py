"""raw.snapshots.last_seen_at — duplicate-content pulls stamp the existing row (LS-52)

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "snapshots",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        schema="raw",
    )


def downgrade() -> None:
    op.drop_column("snapshots", "last_seen_at", schema="raw")
