"""Blend weights: inverse-error fit from the benchmark scoreboards, plus the DB-backed store that
resolves fitted vs. manual-override weights per (horizon, position).

Fit: for each horizon and position, pool each provider's MAE across seasons (n-weighted), then
``w_p ∝ 1 / MAE_p`` normalized over the providers that have a number. The benchmark's ``naive``
row is an opponent, not an ensemble member, and is ignored.

Resolution order (``WeightRepository.resolve``):
  1. ``ensemble_config.use_overrides`` is on **and** override rows exist for the position → those
  2. fitted weights at ``ensemble_config.weights_version`` (NULL = latest fitted version)
  3. nothing stored → ``None`` (the ensemble then splits equally — see ``EnsembleProvider``)
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from lazy_sleeper.providers.base import SEASON, WEEKLY

DEFAULT_MEMBERS: tuple[str, ...] = ("sleeper", "espn")


@dataclass(frozen=True)
class FittedWeight:
    horizon: str
    position: str
    provider: str
    weight: float
    mae: float
    n: int


@dataclass(frozen=True)
class ResolvedWeights:
    horizon: str
    position: str
    weights: dict[str, float]  # provider → normalized weight
    source: str  # "override" | "fitted"
    version: int | None  # fitted version, None for overrides


# --- pure ---------------------------------------------------------------------------------


def normalize(weights: Mapping[str, float]) -> dict[str, float]:
    """Scale to sum 1 over positive entries; non-positive entries drop out."""
    kept = {k: float(v) for k, v in weights.items() if v is not None and v > 0}
    total = sum(kept.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in kept.items()}


def pooled_mae(rows: Iterable[Mapping[str, object]]) -> dict[tuple[str, str], tuple[float, int]]:
    """(position, provider) → (n-weighted MAE across seasons, Σ n) from scoreboard-shaped rows."""
    num: dict[tuple[str, str], float] = defaultdict(float)
    den: dict[tuple[str, str], int] = defaultdict(int)
    for r in rows:
        n = int(r["n"] or 0)
        mae = r["mae"]
        if n <= 0 or mae in (None, "") or (isinstance(mae, float) and math.isnan(mae)):
            continue
        key = (str(r["position"]), str(r["provider"]))
        num[key] += float(mae) * n
        den[key] += n
    return {k: (num[k] / den[k], den[k]) for k in den}


def fit_weights(
    rows_by_horizon: Mapping[str, Iterable[Mapping[str, object]]],
    members: Sequence[str] = DEFAULT_MEMBERS,
) -> list[FittedWeight]:
    """Inverse-MAE weights per (horizon, position) over ``members`` that have a pooled MAE."""
    out: list[FittedWeight] = []
    for horizon, rows in rows_by_horizon.items():
        pooled = pooled_mae(rows)
        positions = sorted({pos for pos, _ in pooled})
        for pos in positions:
            inv = {
                p: 1.0 / pooled[(pos, p)][0]
                for p in members
                if (pos, p) in pooled and pooled[(pos, p)][0] > 0
            }
            for provider, w in normalize(inv).items():
                mae, n = pooled[(pos, provider)]
                out.append(FittedWeight(horizon, pos, provider, w, mae, n))
    return out


def read_scoreboard(path: Path) -> list[dict[str, object]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def fit_from_csvs(season_csv: Path, weekly_csv: Path | None = None) -> list[FittedWeight]:
    rows: dict[str, Iterable[Mapping[str, object]]] = {SEASON: read_scoreboard(season_csv)}
    if weekly_csv is not None and weekly_csv.exists():
        rows[WEEKLY] = read_scoreboard(weekly_csv)
    return fit_weights(rows)


def to_json(fitted: Sequence[FittedWeight], version: int, fitted_at: datetime) -> dict:
    """Committed artefact shape: {version, fitted_at, weights: {horizon: {pos: {provider: …}}}}."""
    tree: dict[str, dict[str, dict[str, dict[str, float | int]]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for f in fitted:
        tree[f.horizon][f.position][f.provider] = {
            "weight": round(f.weight, 6),
            "mae": round(f.mae, 4),
            "n": f.n,
        }
    return {
        "version": version,
        "fitted_at": fitted_at.isoformat(),
        "method": "inverse-MAE, pooled across seasons (n-weighted)",
        "weights": {h: dict(p) for h, p in tree.items()},
    }


# --- DB -----------------------------------------------------------------------------------


class WeightRepository:
    """Fitted versions, manual overrides, and the switchboard row in ``derived.*``."""

    def __init__(self, session: Session) -> None:
        self._s = session

    # fitted -------------------------------------------------------------------------------

    def latest_version(self) -> int | None:
        from lazy_sleeper.db.models import EnsembleWeight

        return self._s.scalar(select(func.max(EnsembleWeight.version)))

    def store_fitted(
        self,
        fitted: Sequence[FittedWeight],
        *,
        fitted_at: datetime | None = None,
        note: str | None = None,
    ) -> int:
        """Append a new version; returns it."""
        from lazy_sleeper.db.models import EnsembleWeight

        version = (self.latest_version() or 0) + 1
        fitted_at = fitted_at or datetime.now(UTC)
        for f in fitted:
            self._s.add(
                EnsembleWeight(
                    version=version,
                    fitted_at=fitted_at,
                    horizon=f.horizon,
                    position=f.position,
                    provider=f.provider,
                    weight=f.weight,
                    mae=f.mae,
                    n=f.n,
                    note=note,
                )
            )
        self._s.flush()
        return version

    def fitted(self, version: int | None = None) -> dict[tuple[str, str], dict[str, float]]:
        """(horizon, position) → provider → weight for one fitted version (default latest)."""
        from lazy_sleeper.db.models import EnsembleWeight

        version = version if version is not None else self.latest_version()
        if version is None:
            return {}
        out: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
        for w in self._s.scalars(select(EnsembleWeight).where(EnsembleWeight.version == version)):
            out[(w.horizon, w.position)][w.provider] = w.weight
        return dict(out)

    # overrides ----------------------------------------------------------------------------

    def overrides(self) -> dict[tuple[str, str], dict[str, float]]:
        from lazy_sleeper.db.models import WeightOverride

        out: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
        for o in self._s.scalars(select(WeightOverride)):
            out[(o.horizon, o.position)][o.provider] = o.weight
        return dict(out)

    def set_override(
        self, horizon: str, position: str, weights: Mapping[str, float], note: str | None = None
    ) -> dict[str, float]:
        """Replace override rows for (horizon, position); stored as given, normalized on read."""
        from lazy_sleeper.db.models import WeightOverride

        if horizon not in (SEASON, WEEKLY):
            raise ValueError(f"horizon must be {SEASON!r} or {WEEKLY!r}, got {horizon!r}")
        if not normalize(weights):
            raise ValueError("at least one provider weight must be > 0")
        now = datetime.now(UTC)
        self._s.execute(
            delete(WeightOverride).where(
                WeightOverride.horizon == horizon, WeightOverride.position == position
            )
        )
        for provider, w in weights.items():
            self._s.add(
                WeightOverride(
                    horizon=horizon,
                    position=position,
                    provider=provider,
                    weight=float(w),
                    note=note,
                    updated_at=now,
                )
            )
        self._s.flush()
        return dict(weights)

    def clear_override(self, horizon: str, position: str | None = None) -> int:
        from lazy_sleeper.db.models import WeightOverride

        stmt = delete(WeightOverride).where(WeightOverride.horizon == horizon)
        if position is not None:
            stmt = stmt.where(WeightOverride.position == position)
        return self._s.execute(stmt).rowcount

    # config -------------------------------------------------------------------------------

    def config(self):  # noqa: ANN201 — returns the ORM row
        from lazy_sleeper.db.models import EnsembleConfig

        row = self._s.get(EnsembleConfig, 1)
        if row is None:
            row = EnsembleConfig(id=1, use_overrides=False, updated_at=datetime.now(UTC))
            self._s.add(row)
            self._s.flush()
        return row

    def set_config(
        self, *, use_overrides: bool | None = None, weights_version: int | None | str = "keep"
    ):  # noqa: ANN201
        row = self.config()
        if use_overrides is not None:
            row.use_overrides = use_overrides
        if weights_version != "keep":
            row.weights_version = weights_version  # type: ignore[assignment]
        row.updated_at = datetime.now(UTC)
        self._s.flush()
        return row

    # resolution ---------------------------------------------------------------------------

    def resolve_all(self, horizon: str) -> dict[str, ResolvedWeights]:
        """position → weights in force for the horizon (overrides first when enabled)."""
        cfg = self.config()
        version = cfg.weights_version if cfg.weights_version is not None else self.latest_version()
        out: dict[str, ResolvedWeights] = {}
        for (h, pos), w in self.fitted(version).items():
            if h == horizon:
                out[pos] = ResolvedWeights(horizon, pos, normalize(w), "fitted", version)
        if cfg.use_overrides:
            for (h, pos), w in self.overrides().items():
                if h == horizon and normalize(w):
                    out[pos] = ResolvedWeights(horizon, pos, normalize(w), "override", None)
        return out

    def resolve(self, horizon: str, position: str) -> ResolvedWeights | None:
        return self.resolve_all(horizon).get(position)
