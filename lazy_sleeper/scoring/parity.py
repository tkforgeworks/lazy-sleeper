"""Parity check: engine output vs nflverse `fantasy_points_ppr` on weekly actuals (LS-21).

Pure functions over fixture rows (see `league.parity_rows` for the DB extraction), so the same code
runs in the unit test against the committed fixture and in `lazy score parity` against the live DB.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from lazy_sleeper.scoring.engine import Scorer
from lazy_sleeper.scoring.rules import ScoringRules


def nflverse_adjustment(stats: Mapping[str, Any], rules: ScoringRules) -> float:
    """Points to subtract from the league score to land on nflverse's fixed PPR map.

    Covers the known differences (`league.NFLVERSE_PPR_DIFFS`): INT −1 vs −2, fumble-recovery TDs
    not in nflverse's formula. Return-fumble losses cannot be adjusted from the stat line (nflverse
    doesn't expose which fumbles were on returns) — they surface as exact +2.0 residuals.
    """
    ints = float(stats.get("pass_int") or 0)
    frtd = float(stats.get("fum_rec_td") or 0)
    return ints * (rules.weight("pass_int") - (-2.0)) + frtd * (rules.weight("fum_rec_td") - 0.0)


@dataclass
class ParityReport:
    n: int = 0
    abs_delta_sum: float = 0.0
    by_position: dict[str, list[float]] = field(default_factory=dict)
    outliers: list[dict[str, Any]] = field(default_factory=list)

    @property
    def mean_abs_delta(self) -> float:
        return self.abs_delta_sum / self.n if self.n else 0.0

    def mean_by_position(self) -> dict[str, float]:
        return {p: sum(v) / len(v) for p, v in sorted(self.by_position.items())}


def parity(
    rows: Iterable[Mapping[str, Any]], scorer: Scorer, *, outlier_at: float = 0.05
) -> ParityReport:
    """Score every row, adjust for the known map differences, and diff against `provider_points`."""
    rep = ParityReport()
    for row in rows:
        stats, pos = row["stats"], row["position"]
        ours = scorer.score(stats, pos)
        adjusted = ours - nflverse_adjustment(stats, scorer.rules)
        delta = adjusted - float(row["provider_points"])
        rep.n += 1
        rep.abs_delta_sum += abs(delta)
        rep.by_position.setdefault(pos, []).append(abs(delta))
        if abs(delta) > outlier_at:
            rep.outliers.append({**row, "league_points": ours, "delta": delta})
    return rep
