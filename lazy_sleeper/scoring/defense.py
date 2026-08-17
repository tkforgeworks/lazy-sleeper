"""DEF normalization + season-average streaming rank (LS-20).

The league scores team defense on counting stats (`sack`, `int`, `fum_rec`, `ff`, `safe`,
`blk_kick`, `def_td`, `def_st_td`, …) plus a points-allowed bracket (`pts_allow_0` 10 …
`pts_allow_35p` −4). Sources differ:

* ESPN — buckets `0 / 1_6 / 7_13 / 14_17 / 18_21 / 22_27 / 28_34 / 35_45 / 46p` (kept ESPN-native at
  ingest). Actuals carry 0/1 flags + the exact `pts_allow`; projections carry per-bucket
  *probabilities* + expected `pts_allow`. `18_21` straddles the league's 14_20 | 21_27 edge, so it
  is split by an empirical points-allowed distribution. TDs: `def_td` (fum+int), `def_st_td` =
  blocked-kick TD only, returns as `def_kr_td` / `def_pr_td`.
* Sleeper projections — **no points-allowed data at all** (season or weekly); TDs only as sub-keys
  (`def_fum_td`, `pass_int_td`, `def_kr_td`, `pr_td`) that Sleeper's own `pts_ppr` ignores. We roll
  them up; the missing bracket is *not* imputed (see CLAUDE.md — DEF board value comes from the
  streaming rank, and LS-25 must not blend Sleeper DEF totals naively).

Points allowed keys are parsed as intervals exactly like FG distances (`pts_allow_18_21`,
`pts_allow_46p`, `pts_allow_0`); the bare `pts_allow` is the numeric points, not a range. When a row
is a single game with an integral `pts_allow` (flags sum to 1) the league bucket is set exactly.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lazy_sleeper.scoring.engine import Scorer, Stats

# League points-allowed brackets (Sleeper vocabulary): (name, lo, hi) inclusive; hi=None → and up.
PA_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("0", 0, 0),
    ("1_6", 1, 6),
    ("7_13", 7, 13),
    ("14_20", 14, 20),
    ("21_27", 21, 27),
    ("28_34", 28, 34),
    ("35p", 35, None),
)
PA_BUCKET_NAMES: tuple[str, ...] = tuple(b[0] for b in PA_BUCKETS)

# Points allowed per team-game, ESPN weekly DEF actuals 2024–2025 (1078 games). Derived from
# core.actuals on 2026-08-17 (`pts_allowed_from_actuals`); refresh after each season.
DEFAULT_PTS_ALLOWED_COUNTS: Mapping[int, float] = {
    3: 15, 4: 1, 6: 23, 7: 20, 8: 4, 9: 19, 10: 55, 11: 2, 12: 12, 13: 45, 14: 36, 15: 14,
    16: 32, 17: 57, 18: 24, 19: 34, 20: 87, 21: 40, 22: 30, 23: 44, 24: 65, 25: 21, 26: 32,
    27: 65, 28: 30, 29: 20, 30: 37, 31: 30, 32: 15, 33: 13, 34: 40, 35: 17, 36: 8, 37: 14,
    38: 20, 39: 1, 40: 9, 41: 11, 42: 12, 44: 9, 45: 4, 47: 5, 48: 3, 52: 3,
}  # fmt: skip

_PA_RANGE_RE = re.compile(r"^pts_allow_(?P<lo>\d+)(?:_(?P<hi>\d+)|(?P<plus>p))?$")


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PointsAllowedPmf:
    """Empirical distribution of points allowed per game (integer support)."""

    counts: Mapping[int, float]

    def mass(self, lo: int, hi: int | None) -> float:
        hi_v = math.inf if hi is None else hi
        return sum(c for p, c in self.counts.items() if lo <= p <= hi_v)

    def spread(self, amount: float, ranges: list[tuple[str, int, int | None]]) -> dict[str, float]:
        """Split `amount` over named ranges proportional to their mass (equal if all zero)."""
        masses = {name: self.mass(lo, hi) for name, lo, hi in ranges}
        total = sum(masses.values())
        if total > 0:
            return {name: amount * m / total for name, m in masses.items()}
        return {name: amount / len(ranges) for name, _, _ in ranges}


DEFAULT_PA_PMF = PointsAllowedPmf(DEFAULT_PTS_ALLOWED_COUNTS)


def bucket_for_points(points: float) -> str:
    """League bucket name for an exact points-allowed value."""
    for name, lo, hi in PA_BUCKETS:
        if lo <= points <= (math.inf if hi is None else hi):
            return name
    raise ValueError(f"points allowed out of range: {points}")


def _parse_pa(key: str) -> tuple[int, int | None] | None:
    m = _PA_RANGE_RE.match(key)
    if not m:
        return None
    lo = int(m.group("lo"))
    if m.group("plus"):
        return lo, None
    return lo, int(m.group("hi")) if m.group("hi") is not None else lo


def _overlap(lo: int, hi: int | None, b_lo: int, b_hi: int | None) -> tuple[int, int | None] | None:
    o_lo = max(lo, b_lo)
    o_hi = min(math.inf if hi is None else hi, math.inf if b_hi is None else b_hi)
    if o_lo > o_hi:
        return None
    return o_lo, None if o_hi == math.inf else int(o_hi)


def split_pts_allowed(stats: Stats, pmf: PointsAllowedPmf) -> dict[str, float] | None:
    """League-bucket counts from whatever `pts_allow_*` ranges the row carries; None if it has none.

    Exact path: a single game with an integral `pts_allow` (flags sum to ≈1) → 1 in its bucket.
    Otherwise each source range is distributed over the league buckets it overlaps, proportional to
    the pmf mass of each overlap. Source ranges nested inside one league bucket land there whole, so
    Sleeper-vocabulary rows pass through unchanged.
    """
    ranges: list[tuple[int, int | None, float]] = []
    for key, raw in stats.items():
        parsed = _parse_pa(key)
        v = _num(raw)
        if parsed is None or v is None:
            continue
        ranges.append((parsed[0], parsed[1], v))
    if not ranges:
        return None

    total_flags = sum(v for _, _, v in ranges)
    exact = _num(stats.get("pts_allow"))
    single_game = abs(total_flags - 1) < 1e-6 and all(v.is_integer() for _, _, v in ranges)
    if exact is not None and exact >= 0 and exact.is_integer() and single_game:
        out = dict.fromkeys(PA_BUCKET_NAMES, 0.0)
        out[bucket_for_points(exact)] = 1.0
        return out

    out = dict.fromkeys(PA_BUCKET_NAMES, 0.0)
    for lo, hi, value in ranges:
        if value == 0:
            continue
        pieces = []
        for name, b_lo, b_hi in PA_BUCKETS:
            ov = _overlap(lo, hi, b_lo, b_hi)
            if ov is not None:
                pieces.append((name, ov[0], ov[1]))
        for name, share in pmf.spread(value, pieces).items():
            out[name] += share
    return out


def _f(stats: Stats, key: str) -> float:
    return _num(stats.get(key)) or 0.0


@dataclass(frozen=True)
class DefenseNormalizer:
    """Callable stat normalizer for DEF rows — plug into `Scorer(normalizers={"DEF": ...})`."""

    pmf: PointsAllowedPmf = DEFAULT_PA_PMF

    def __call__(self, stats: Stats) -> dict[str, Any]:
        out: dict[str, Any] = dict(stats)

        # Defensive TDs: keep the recorded total when it already covers the parts, else sum them.
        int_td = _f(stats, "def_int_td") or _f(stats, "pass_int_td")
        parts = _f(stats, "def_fum_td") + int_td
        out["def_td"] = max(_f(stats, "def_td"), parts)

        # Special-teams TDs: ESPN's def_st_td is blocked-kick TDs only, returns come separately;
        # Sleeper's def_st_td already includes returns. Add returns only when they aren't covered.
        returns = max(
            _f(stats, "def_kr_td") + _f(stats, "def_pr_td"), _f(stats, "kr_td") + _f(stats, "pr_td")
        )
        recorded = _f(stats, "def_st_td")
        out["def_st_td"] = recorded if recorded >= returns else recorded + returns

        pa = split_pts_allowed(stats, self.pmf)
        if pa is not None:
            for name, v in pa.items():
                out[f"pts_allow_{name}"] = v
        return out


# --- streaming rank --------------------------------------------------------------


@dataclass(frozen=True)
class TeamRank:
    team: str
    sleeper_id: str | None
    games: int
    ppg: float
    rank: int


def streaming_ranks(
    session: Any,
    scorer: Scorer,
    seasons: tuple[int, ...] = (2024, 2025),
    source: str = "espn",
) -> list[TeamRank]:
    """Season-average DEF streaming rank v1: mean league points/game over `seasons` weekly actuals.

    Every game is scored through the engine (exact points-allowed bracket), so the rank reflects the
    league's map, not a provider's. Ties broken by team abbreviation for determinism.
    """
    from sqlalchemy import select

    from lazy_sleeper.db.models import Actual

    stmt = select(Actual.team, Actual.sleeper_id, Actual.stats).where(
        Actual.source == source,
        Actual.position == "DEF",
        Actual.week.is_not(None),
        Actual.season.in_(seasons),
    )
    totals: dict[str, list[float]] = {}
    ids: dict[str, str | None] = {}
    for team, sid, stats in session.execute(stmt):
        if not team:
            continue
        totals.setdefault(team, []).append(scorer.score(stats, "DEF"))
        ids.setdefault(team, sid)
    rows = sorted(((sum(v) / len(v), t) for t, v in totals.items()), key=lambda r: (-r[0], r[1]))
    return [
        TeamRank(team=t, sleeper_id=ids[t], games=len(totals[t]), ppg=ppg, rank=i + 1)
        for i, (ppg, t) in enumerate(rows)
    ]


def pts_allowed_from_actuals(
    session: Any, seasons: tuple[int, ...] = (2024, 2025), source: str = "espn"
) -> PointsAllowedPmf:
    """Re-derive the points-allowed distribution behind `DEFAULT_PA_PMF` from weekly DEF actuals."""
    from sqlalchemy import select

    from lazy_sleeper.db.models import Actual

    stmt = select(Actual.stats).where(
        Actual.source == source,
        Actual.position == "DEF",
        Actual.week.is_not(None),
        Actual.season.in_(seasons),
    )
    counts: dict[int, float] = {}
    for (stats,) in session.execute(stmt):
        v = _num(stats.get("pts_allow"))
        if v is not None and v.is_integer():
            counts[int(v)] = counts.get(int(v), 0) + 1
    return PointsAllowedPmf(dict(sorted(counts.items())))
