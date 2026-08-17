"""Scoring engine: apply a league's `ScoringRules` to a stat line in Sleeper vocabulary.

Stat lines are the `stats` JSONB from `core.projections` / `core.actuals` (Sleeper keys, so
projections and actuals score through the same path). No position-specific constants live here —
the league map is the only source of weights. Position-specific *normalization* of stats before
scoring (K distance mix — LS-19; DEF points-allowed buckets — LS-20) plugs in via
`Scorer.normalizers` so the arithmetic below stays generic.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from numbers import Real
from typing import Any

from lazy_sleeper.scoring.rules import PRESCORED_KEYS, ScoringRules

Stats = Mapping[str, Any]
Normalizer = Callable[[Stats], Stats]


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, Real):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def breakdown(stats: Stats, rules: ScoringRules) -> dict[str, float]:
    """Per-key point contributions: `{key: stats[key] * weight}` for keys the league scores.

    Keys the league does not score, non-numeric values, and pre-scored totals are ignored.
    Zero-contribution keys are dropped so the result reads as "where the points came from".
    """
    out: dict[str, float] = {}
    for key, raw in stats.items():
        if key in PRESCORED_KEYS or key not in rules:
            continue
        v = _num(raw)
        if v is None or v == 0.0:
            continue
        pts = v * rules.weight(key)
        if pts != 0.0:
            out[key] = pts
    return out


def score(stats: Stats, rules: ScoringRules) -> float:
    """Total fantasy points for one stat line under `rules`."""
    return sum(breakdown(stats, rules).values())


@dataclass(frozen=True)
class Scored:
    points: float
    breakdown: dict[str, float]


@dataclass(frozen=True)
class Scorer:
    """Scores stat lines for a league; optional per-position normalizers run before scoring."""

    rules: ScoringRules
    normalizers: Mapping[str, Normalizer] = field(default_factory=dict)

    def normalize(self, stats: Stats, position: str | None) -> Stats:
        fn = self.normalizers.get(position or "")
        return fn(stats) if fn else stats

    def score(self, stats: Stats, position: str | None = None) -> float:
        return score(self.normalize(stats, position), self.rules)

    def explain(self, stats: Stats, position: str | None = None) -> Scored:
        parts = breakdown(self.normalize(stats, position), self.rules)
        return Scored(points=sum(parts.values()), breakdown=parts)
