"""Replacement-level baselines by position (LS-26).

VORP = player points − replacement baseline, where the baseline is the points of the *last
starter* at each position. Starters = teams × dedicated roster slots plus the flex seats, and
flex is allocated greedily by value: after dedicated starters are removed from each position's
ranking, every flex seat takes the best remaining eligible player. Cutoffs are therefore derived
from the league's `roster_positions` and the points table itself — on 2025 actuals this lands at
QB12 / RB29 / WR41 / TE14 (the plan doc's anchors) but nothing is hardcoded; a points table that
skews differently moves the flex share and thus the cutoffs.

Two tables feed it (both league-scored):

- historical — 2023–25 season actuals (nflverse offense/K + ESPN weekly DEF, the same source
  split as the benchmark), one derivation per season, baseline points averaged across seasons
- live — 2026 ensemble projections, re-derived on demand for the draft board

Pure functions take plain tuples so they are testable without Postgres; DB assembly lives at the
bottom, same split as ``benchmark/season.py``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from lazy_sleeper.benchmark.season import ACTUAL_SOURCE_BY_POSITION, POSITIONS
from lazy_sleeper.providers.base import ProjectionProvider
from lazy_sleeper.scoring import Scorer
from lazy_sleeper.scoring.rules import ScoringRules

DEFAULT_SEASONS: tuple[int, ...] = (2023, 2024, 2025)

# Sleeper flex slot kinds → the positions each seat may hold.
FLEX_ELIGIBILITY: Mapping[str, tuple[str, ...]] = {
    "FLEX": ("RB", "WR", "TE"),
    "WRRB_FLEX": ("RB", "WR"),
    "REC_FLEX": ("WR", "TE"),
    "SUPER_FLEX": ("QB", "RB", "WR", "TE"),
}

# (sleeper_id, position, league-scored points) — position None rows are skipped.
PointsRow = tuple[str, str | None, float]


@dataclass(frozen=True)
class RosterShape:
    """Starting-lineup shape from the league payload: how many seats, and who may sit where."""

    teams: int
    dedicated: Mapping[str, int]  # position → dedicated starting slots per team
    flex: tuple[tuple[str, ...], ...]  # per-team flex slots, each an eligibility tuple

    @classmethod
    def from_rules(cls, rules: ScoringRules) -> RosterShape:
        if not rules.total_rosters:
            raise ValueError("league payload has no total_rosters — re-pull the league snapshot")
        dedicated = {
            pos: rules.roster_positions.count(pos)
            for pos in POSITIONS
            if pos in rules.roster_positions
        }
        flex = tuple(
            FLEX_ELIGIBILITY[slot] for slot in rules.roster_positions if slot in FLEX_ELIGIBILITY
        )
        return cls(teams=rules.total_rosters, dedicated=dedicated, flex=flex)


@dataclass(frozen=True)
class PositionBaseline:
    position: str
    cutoff_rank: int  # rank of the last starter (dedicated + flex fills)
    points: float  # that player's points — the replacement level
    flex_fills: int  # flex seats this position absorbed


# --- pure ---------------------------------------------------------------------------------


def derive_baselines(
    rows: Iterable[PointsRow],
    shape: RosterShape,
    *,
    stream_depth: Mapping[str, int] | None = None,
) -> dict[str, PositionBaseline]:
    """Baseline per position: fill dedicated seats top-down, then flex seats greedily by value.

    ``stream_depth`` overrides the cutoff for streamable positions (K/DEF): replacement level
    is the best waiver option, not the 12th starter — ``{"K": 6}`` = the 6th-best K (LS-33).

    Flex seats are filled most-restrictive-first (fewest eligible positions) so a narrow seat is
    never starved by a general one. A position short of its seats clamps at its last player.
    """
    wanted = set(shape.dedicated) | {pos for elig in shape.flex for pos in elig}
    pool: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for sleeper_id, position, points in rows:
        if position in wanted:
            pool[position].append((float(points), sleeper_id))
    ranked = {pos: sorted(entries, reverse=True) for pos, entries in pool.items()}

    taken: dict[str, int] = {}  # position → players consumed so far (dedicated + flex)
    fills: dict[str, int] = defaultdict(int)  # position → flex seats absorbed
    for pos, slots in shape.dedicated.items():
        taken[pos] = min(shape.teams * slots, len(ranked.get(pos, ())))

    flex_seats = sorted(shape.flex * shape.teams, key=len)
    for eligible in flex_seats:
        candidates = [
            (ranked[pos][taken.get(pos, 0)][0], pos)
            for pos in eligible
            if taken.get(pos, 0) < len(ranked.get(pos, ()))
        ]
        if not candidates:
            continue
        _, pos = max(candidates)  # points tie → alphabetically last position; deterministic
        taken[pos] = taken.get(pos, 0) + 1
        fills[pos] += 1

    for pos, depth in (stream_depth or {}).items():
        if pos in ranked and depth > 0:
            taken[pos] = min(depth, len(ranked[pos]))

    out: dict[str, PositionBaseline] = {}
    for pos, cutoff in taken.items():
        if cutoff > 0:
            out[pos] = PositionBaseline(pos, cutoff, ranked[pos][cutoff - 1][0], fills[pos])
    return out


def average_baselines(
    per_season: Mapping[int, Mapping[str, PositionBaseline]],
) -> dict[str, float]:
    """Mean baseline points per position over the seasons that produced one."""
    sums: dict[str, list[float]] = defaultdict(list)
    for baselines in per_season.values():
        for pos, b in baselines.items():
            sums[pos].append(b.points)
    return {pos: sum(vals) / len(vals) for pos, vals in sums.items()}


@dataclass(frozen=True)
class HistoricalBaselines:
    seasons: tuple[int, ...]
    per_season: Mapping[int, Mapping[str, PositionBaseline]]
    average: Mapping[str, float]  # position → mean baseline points — the VORP input


# --- DB assembly --------------------------------------------------------------------------


def load_actual_player_points(
    session: Session,
    scorer: Scorer,
    season: int,
    sources: Mapping[str, str] = ACTUAL_SOURCE_BY_POSITION,
) -> list[PointsRow]:
    """Season totals per player from weekly actuals, keeping the position for ranking.

    Same source split as ``benchmark.season.load_actual_points`` (one designated source per
    position so nobody is double-counted); that helper drops the position, this one needs it.
    """
    from sqlalchemy import select

    from lazy_sleeper.db.models import Actual

    wanted = {(src, pos) for pos, src in sources.items()}
    totals: dict[tuple[str, str], float] = defaultdict(float)
    stmt = select(Actual.source, Actual.sleeper_id, Actual.position, Actual.stats).where(
        Actual.season == season,
        Actual.week.is_not(None),
        Actual.sleeper_id.is_not(None),
        Actual.source.in_(set(sources.values())),
    )
    for source, sleeper_id, position, stats in session.execute(stmt):
        if (source, position) in wanted:
            totals[(sleeper_id, position)] += scorer.score(stats, position)
    return [(sid, pos, pts) for (sid, pos), pts in totals.items()]


def historical_baselines(
    session: Session,
    scorer: Scorer,
    shape: RosterShape,
    seasons: Sequence[int] = DEFAULT_SEASONS,
) -> HistoricalBaselines:
    per_season = {
        season: derive_baselines(load_actual_player_points(session, scorer, season), shape)
        for season in seasons
    }
    return HistoricalBaselines(tuple(seasons), per_season, average_baselines(per_season))


def live_baselines(
    provider: ProjectionProvider, shape: RosterShape, season: int
) -> dict[str, PositionBaseline]:
    """Baselines from a provider's current season projections (the draft-board input)."""
    rows = [(p.sleeper_id, p.position, p.points) for p in provider.projections(season)]
    return derive_baselines(rows, shape)
