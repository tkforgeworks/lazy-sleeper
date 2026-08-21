"""Stored tier/cliff/flag thresholds — `derived.board_config`, one row, app-adjustable."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from lazy_sleeper.board.tiers import TierConfig

NUMERIC_FIELDS = (
    "cliff_gap",
    "gap_multiplier",
    "min_gap",
    "adp_min_delta",
    "adp_pct",
    "disagree_min_pts",
    "disagree_pct",
    "survival_sigma_min",
    "survival_sigma_pct",
    "demand_shift",
    "need_bonus",
)
INT_FIELDS = ("run_window", "run_threshold", "run_streak")  # LS-33, positive ints
BOOL_FIELDS = ("debias_disagreement",)
FIELDS = (*NUMERIC_FIELDS, *INT_FIELDS, *BOOL_FIELDS)


class BoardConfigRepository:
    """Read/update the single `derived.board_config` row; falls back to TierConfig defaults."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def row(self):  # noqa: ANN201 — returns the ORM row
        from lazy_sleeper.db.models import BoardConfig

        row = self._s.get(BoardConfig, 1)
        if row is None:
            defaults = TierConfig()
            row = BoardConfig(
                id=1,
                **{name: getattr(defaults, name) for name in FIELDS},
                updated_at=datetime.now(UTC),
            )
            self._s.add(row)
            self._s.flush()
        return row

    def get(self) -> TierConfig:
        row = self.row()
        return TierConfig(**{name: getattr(row, name) for name in FIELDS})

    def as_dict(self) -> dict[str, object]:
        row = self.row()
        return {**{name: getattr(row, name) for name in FIELDS}, "updated_at": row.updated_at}

    def set(self, **values: float | int | bool | None) -> TierConfig:
        """Update any subset of ``FIELDS`` (None = leave alone). Numeric/int dials must be > 0."""
        unknown = set(values) - set(FIELDS)
        if unknown:
            raise ValueError(f"unknown board_config field(s): {sorted(unknown)}")
        for name, value in values.items():
            if value is None:
                continue
            if name in NUMERIC_FIELDS and value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
            if name in INT_FIELDS and (int(value) != value or value < 1):
                raise ValueError(f"{name} must be a positive integer, got {value}")
        row = self.row()
        for name, value in values.items():
            if value is not None:
                setattr(row, name, int(value) if name in INT_FIELDS else value)
        row.updated_at = datetime.now(UTC)
        self._s.flush()
        return self.get()
