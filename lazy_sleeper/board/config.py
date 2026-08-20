"""Stored tier/cliff thresholds — `derived.board_config`, single row, app-adjustable (LS-28)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from lazy_sleeper.board.tiers import TierConfig


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
                cliff_gap=defaults.cliff_gap,
                gap_multiplier=defaults.gap_multiplier,
                min_gap=defaults.min_gap,
                updated_at=datetime.now(UTC),
            )
            self._s.add(row)
            self._s.flush()
        return row

    def get(self) -> TierConfig:
        row = self.row()
        return TierConfig(
            cliff_gap=row.cliff_gap, gap_multiplier=row.gap_multiplier, min_gap=row.min_gap
        )

    def set(
        self,
        *,
        cliff_gap: float | None = None,
        gap_multiplier: float | None = None,
        min_gap: float | None = None,
    ) -> TierConfig:
        for name, value in (
            ("cliff_gap", cliff_gap),
            ("gap_multiplier", gap_multiplier),
            ("min_gap", min_gap),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        row = self.row()
        if cliff_gap is not None:
            row.cliff_gap = cliff_gap
        if gap_multiplier is not None:
            row.gap_multiplier = gap_multiplier
        if min_gap is not None:
            row.min_gap = min_gap
        row.updated_at = datetime.now(UTC)
        self._s.flush()
        return self.get()
