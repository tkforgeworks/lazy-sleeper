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
)
FIELDS = (*NUMERIC_FIELDS, "debias_disagreement")


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

    def set(
        self,
        *,
        cliff_gap: float | None = None,
        gap_multiplier: float | None = None,
        min_gap: float | None = None,
        adp_min_delta: float | None = None,
        adp_pct: float | None = None,
        disagree_min_pts: float | None = None,
        disagree_pct: float | None = None,
        debias_disagreement: bool | None = None,
    ) -> TierConfig:
        numeric = {
            "cliff_gap": cliff_gap,
            "gap_multiplier": gap_multiplier,
            "min_gap": min_gap,
            "adp_min_delta": adp_min_delta,
            "adp_pct": adp_pct,
            "disagree_min_pts": disagree_min_pts,
            "disagree_pct": disagree_pct,
        }
        for name, value in numeric.items():
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        row = self.row()
        for name, value in numeric.items():
            if value is not None:
                setattr(row, name, value)
        if debias_disagreement is not None:
            row.debias_disagreement = debias_disagreement
        row.updated_at = datetime.now(UTC)
        self._s.flush()
        return self.get()
