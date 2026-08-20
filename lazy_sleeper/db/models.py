"""SQLAlchemy models.

Schemas:
  raw   — immutable snapshot metadata. Payload bytes live in the snapshot store (local gz +
          Supabase Storage), never in Postgres. Rows are append-only.
  core  — parsed tables: players, crosswalk (current state); projections (per-snapshot vintages);
          actuals (facts, latest wins); adp (market data per season snapshot).

Plain Postgres only — no local-only extensions — so the same migrations run on Supabase.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
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


class Projection(Base):
    """One projection stat line for one player, one season/week, from one snapshot (a *vintage*).

    `stats` is a JSONB dict in the Sleeper stat vocabulary (which is also the vocabulary of the
    league's `scoring_settings` map) — ESPN stat ids are decoded into it on load. Season totals
    have week NULL. Rows are per-snapshot so provider revisions stay visible over time.
    """

    __tablename__ = "projections"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "source_player_id",
            "season",
            "week",
            name="uq_projection_identity",
            postgresql_nulls_not_distinct=True,
        ),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("raw.snapshots.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # sleeper | espn | forge
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int | None] = mapped_column(Integer)  # NULL = season total
    source_player_id: Mapped[str] = mapped_column(String(32), nullable=False)
    sleeper_id: Mapped[str | None] = mapped_column(String(16), index=True)  # resolved; NULL if not
    position: Mapped[str | None] = mapped_column(String(8))
    team: Mapped[str | None] = mapped_column(String(8))
    gp: Mapped[float | None] = mapped_column(Float)
    provider_points: Mapped[float | None] = mapped_column(Float)  # provider PPR pts, x-check
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class Actual(Base):
    """One actual stat line — a *fact*: one row per (source, season, week, player); latest wins.

    Same `stats` vocabulary as projections. `snapshot_id` is provenance only (not identity):
    providers revise actuals slightly after the fact and we keep the latest, never two versions.
    """

    __tablename__ = "actuals"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "season",
            "week",
            "source_player_id",
            name="uq_actual_identity",
            postgresql_nulls_not_distinct=True,
        ),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("raw.snapshots.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # espn | nflverse | sleeper
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int | None] = mapped_column(Integer)  # NULL = season total
    source_player_id: Mapped[str] = mapped_column(String(32), nullable=False)
    sleeper_id: Mapped[str | None] = mapped_column(String(16), index=True)
    position: Mapped[str | None] = mapped_column(String(8))
    team: Mapped[str | None] = mapped_column(String(8))
    gp: Mapped[float | None] = mapped_column(Float)
    provider_points: Mapped[float | None] = mapped_column(Float)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class Adp(Base):
    """Sleeper platform ADP per season snapshot. Market data, kept apart from projections."""

    __tablename__ = "adp"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "sleeper_id", name="uq_adp_identity"),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("raw.snapshots.id"), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    sleeper_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    position: Mapped[str | None] = mapped_column(String(8))
    adp_ppr: Mapped[float | None] = mapped_column(Float)
    adp_half_ppr: Mapped[float | None] = mapped_column(Float)
    adp_std: Mapped[float | None] = mapped_column(Float)
    adp_2qb: Mapped[float | None] = mapped_column(Float)
    adp_dynasty: Mapped[float | None] = mapped_column(Float)
    adp_dynasty_ppr: Mapped[float | None] = mapped_column(Float)
    adp_rookie: Mapped[float | None] = mapped_column(Float)
    adp_idp: Mapped[float | None] = mapped_column(Float)


class SnapCount(Base):
    """nflverse snap counts — one row per player-game (REG season), latest load wins."""

    __tablename__ = "snap_counts"
    __table_args__ = (
        UniqueConstraint("season", "week", "pfr_player_id", name="uq_snap_count_identity"),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("raw.snapshots.id"), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    pfr_player_id: Mapped[str] = mapped_column(String(16), nullable=False)
    sleeper_id: Mapped[str | None] = mapped_column(String(16), index=True)
    player: Mapped[str | None] = mapped_column(String(128))
    position: Mapped[str | None] = mapped_column(String(8))
    team: Mapped[str | None] = mapped_column(String(8))
    opponent: Mapped[str | None] = mapped_column(String(8))
    offense_snaps: Mapped[int | None] = mapped_column(Integer)
    offense_pct: Mapped[float | None] = mapped_column(Float)
    defense_snaps: Mapped[int | None] = mapped_column(Integer)
    defense_pct: Mapped[float | None] = mapped_column(Float)
    st_snaps: Mapped[int | None] = mapped_column(Integer)
    st_pct: Mapped[float | None] = mapped_column(Float)


class ExpectedPoints(Base):
    """ffverse ff_opportunity (xFP) — expected stats/points per player-week, latest load wins.

    `ep` holds the player-level columns (actual + `_exp`), with `_team` and `_diff` columns
    dropped as derivable. ForgeModel feature source and an extra benchmark opponent.
    """

    __tablename__ = "expected_points"
    __table_args__ = (
        UniqueConstraint("season", "week", "gsis_id", name="uq_expected_points_identity"),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("raw.snapshots.id"), nullable=False)
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    gsis_id: Mapped[str] = mapped_column(String(16), nullable=False)
    sleeper_id: Mapped[str | None] = mapped_column(String(16), index=True)
    full_name: Mapped[str | None] = mapped_column(String(128))
    position: Mapped[str | None] = mapped_column(String(8))
    team: Mapped[str | None] = mapped_column(String(8))
    total_fantasy_points: Mapped[float | None] = mapped_column(Float)
    total_fantasy_points_exp: Mapped[float | None] = mapped_column(Float)
    ep: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


# --- derived ------------------------------------------------------------------------------


class EnsembleWeight(Base):
    """Fitted inverse-error blend weights (LS-25) — append-only, one `version` per fit run.

    `horizon` is "season" or "weekly"; `weight` is normalized over the providers of that
    (version, horizon, position). `mae` / `n` are the pooled benchmark numbers behind it.
    """

    __tablename__ = "ensemble_weights"
    __table_args__ = (
        UniqueConstraint(
            "version", "horizon", "position", "provider", name="uq_ensemble_weight_identity"
        ),
        {"schema": "derived"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    fitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    horizon: Mapped[str] = mapped_column(String(8), nullable=False)
    position: Mapped[str] = mapped_column(String(8), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    mae: Mapped[float | None] = mapped_column(Float)
    n: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)


class WeightOverride(Base):
    """Manual per-position blend weights (the λ override) — upserted by hand / from the app.

    Only consulted when `EnsembleConfig.use_overrides` is on; a position with no override rows
    falls back to the fitted weights. Weights are normalized at read time.
    """

    __tablename__ = "weight_overrides"
    __table_args__ = (
        UniqueConstraint("horizon", "position", "provider", name="uq_weight_override_identity"),
        {"schema": "derived"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    horizon: Mapped[str] = mapped_column(String(8), nullable=False)
    position: Mapped[str] = mapped_column(String(8), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EnsembleConfig(Base):
    """Single-row switchboard: use overrides or fitted weights; optionally pin a fitted version."""

    __tablename__ = "ensemble_config"
    __table_args__ = ({"schema": "derived"},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    use_overrides: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    weights_version: Mapped[int | None] = mapped_column(Integer)  # NULL = latest fitted
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BoardConfig(Base):
    """Single-row tier/cliff thresholds for the draft board — adjustable live from the app."""

    __tablename__ = "board_config"
    __table_args__ = ({"schema": "derived"},)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # always 1
    cliff_gap: Mapped[float] = mapped_column(Float, nullable=False)  # season pts to next player
    gap_multiplier: Mapped[float] = mapped_column(Float, nullable=False)  # × median gap
    min_gap: Mapped[float] = mapped_column(Float, nullable=False)  # tier-break floor, season pts
    # LS-29 flag thresholds (migration 0007)
    adp_min_delta: Mapped[float] = mapped_column(Float, nullable=False, default=12.0)
    adp_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.25)
    disagree_min_pts: Mapped[float] = mapped_column(Float, nullable=False, default=20.0)
    disagree_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.15)
    debias_disagreement: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Board(Base):
    """One generated draft board (LS-30): a dated, immutable run of vorp -> tiers -> flags."""

    __tablename__ = "boards"
    __table_args__ = ({"schema": "derived"},)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    season: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # sleeper | espn | ensemble
    baseline: Mapped[str] = mapped_column(String(16), nullable=False)  # live | historical
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # TierConfig in force
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)


class BoardEntry(Base):
    """One ranked row of a generated board — BoardRow flattened plus player identity/injury."""

    __tablename__ = "board_rows"
    __table_args__ = (
        UniqueConstraint("board_id", "rank", name="uq_board_rows_rank"),
        Index("ix_board_rows_board_position", "board_id", "position"),
        {"schema": "derived"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    board_id: Mapped[int] = mapped_column(
        ForeignKey("derived.boards.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    sleeper_id: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str | None] = mapped_column(String(128))
    position: Mapped[str] = mapped_column(String(8), nullable=False)
    team: Mapped[str | None] = mapped_column(String(8))
    injury_status: Mapped[str | None] = mapped_column(String(32))
    points: Mapped[float] = mapped_column(Float, nullable=False)
    baseline: Mapped[float] = mapped_column(Float, nullable=False)
    vorp: Mapped[float] = mapped_column(Float, nullable=False)
    pos_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    tier: Mapped[int | None] = mapped_column(Integer)
    cliff: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gap_to_next: Mapped[float | None] = mapped_column(Float)
    adp: Mapped[float | None] = mapped_column(Float)
    adp_delta: Mapped[float | None] = mapped_column(Float)
    adp_flag: Mapped[str | None] = mapped_column(String(8))
    spread: Mapped[float | None] = mapped_column(Float)
    disagree: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
