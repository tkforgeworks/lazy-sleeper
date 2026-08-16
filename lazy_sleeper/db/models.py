"""SQLAlchemy models.

Schemas:
  raw   — immutable snapshot metadata. Payload bytes live in the snapshot store (local gz +
          Supabase Storage), never in Postgres. Rows are append-only.
  core  — parsed, normalized current-state tables (players, crosswalk; projections/actuals
          arrive with the scoring engine in M1).

Plain Postgres only — no local-only extensions — so the same migrations run on Supabase.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Snapshot(Base):
    """One external pull. Immutable once written."""

    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint(
            "source", "kind", "season", "week", "pulled_at", name="uq_snapshot_identity"
        ),
        {"schema": "raw"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # sleeper | espn | nflverse
    kind: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # players | projections_season | ...
    season: Mapped[int | None] = mapped_column(Integer)
    week: Mapped[int | None] = mapped_column(Integer)
    pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)  # relative to snapshot_dir
    remote_path: Mapped[str | None] = mapped_column(Text)  # Supabase Storage object path
    record_count: Mapped[int | None] = mapped_column(Integer)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    validation_notes: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # endpoint, params, headers-lite


class Player(Base):
    """Current-state Sleeper player record; upserted from the latest players snapshot."""

    __tablename__ = "players"
    __table_args__ = {"schema": "core"}

    sleeper_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String(128))
    position: Mapped[str | None] = mapped_column(String(8), index=True)
    team: Mapped[str | None] = mapped_column(String(8), index=True)
    status: Mapped[str | None] = mapped_column(String(32))
    injury_status: Mapped[str | None] = mapped_column(String(32))
    depth_chart_order: Mapped[int | None] = mapped_column(Integer)
    search_rank: Mapped[int | None] = mapped_column(Integer)
    years_exp: Mapped[int | None] = mapped_column(Integer)
    age: Mapped[int | None] = mapped_column(Integer)
    team_changed_at: Mapped[int | None] = mapped_column(BigInteger)  # epoch ms as Sleeper gives it
    sportradar_id: Mapped[str | None] = mapped_column(String(64), index=True)
    espn_id: Mapped[str | None] = mapped_column(String(32))
    gsis_id: Mapped[str | None] = mapped_column(String(32))
    yahoo_id: Mapped[str | None] = mapped_column(String(32))
    active: Mapped[bool | None] = mapped_column(Boolean)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("raw.snapshots.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Crosswalk(Base):
    """dynastyprocess playerids — the join spine, keyed on sleeper_id."""

    __tablename__ = "crosswalk"
    __table_args__ = {"schema": "core"}

    sleeper_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    sportradar_id: Mapped[str | None] = mapped_column(String(64), index=True)
    gsis_id: Mapped[str | None] = mapped_column(String(32), index=True)
    espn_id: Mapped[str | None] = mapped_column(String(32), index=True)
    pfr_id: Mapped[str | None] = mapped_column(String(32))
    mfl_id: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(128))
    merge_name: Mapped[str | None] = mapped_column(String(128))
    position: Mapped[str | None] = mapped_column(String(8))
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("raw.snapshots.id"))
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
