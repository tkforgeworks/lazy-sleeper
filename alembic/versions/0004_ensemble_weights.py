"""derived.ensemble_weights (fitted), derived.weight_overrides (manual), derived.ensemble_config

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ensemble_weights",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("fitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("horizon", sa.String(8), nullable=False),
        sa.Column("position", sa.String(8), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("mae", sa.Float()),
        sa.Column("n", sa.Integer()),
        sa.Column("note", sa.Text()),
        sa.UniqueConstraint(
            "version", "horizon", "position", "provider", name="uq_ensemble_weight_identity"
        ),
        schema="derived",
    )
    op.create_index(
        "ix_derived_ensemble_weights_version", "ensemble_weights", ["version"], schema="derived"
    )
    op.create_table(
        "weight_overrides",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("horizon", sa.String(8), nullable=False),
        sa.Column("position", sa.String(8), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("horizon", "position", "provider", name="uq_weight_override_identity"),
        schema="derived",
    )
    op.create_table(
        "ensemble_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("use_overrides", sa.Boolean(), nullable=False),
        sa.Column("weights_version", sa.Integer()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema="derived",
    )
    op.execute(
        "INSERT INTO derived.ensemble_config (id, use_overrides, weights_version, updated_at) "
        "VALUES (1, false, NULL, now())"
    )


def downgrade() -> None:
    op.drop_table("ensemble_config", schema="derived")
    op.drop_table("weight_overrides", schema="derived")
    op.drop_table("ensemble_weights", schema="derived")
