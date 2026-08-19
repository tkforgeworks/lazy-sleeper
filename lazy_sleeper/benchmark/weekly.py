"""Weekly-horizon projection scoreboard (LS-24).

Same idea as the season scoreboard, one week at a time: for each (season, week) take the season's
ADP pool, keep the players at least one provider expected to play (projected > 0), score each
provider's *pre-game* weekly projection under league rules and compare with that week's scored
actual (0 if the player has no row). Providers:

- ``sleeper`` / ``espn`` — the latest stored vintage for (source, season, week)
- ``naive`` — the player's mean weekly actual over *earlier* weeks this season; week 1 uses the
  prior season's per-game average. "He'll do what he's been doing."

Reporting: ``scoreboard()`` runs per week; ``aggregate()`` then rolls the weeks up per (season,
position, provider) with MAE / bias / RMSE pooled over all player-weeks and ``spearman`` = the mean
of the per-week rank correlations — rank *within a week* is the start/sit question.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lazy_sleeper.benchmark.season import (
    ACTUAL_SOURCE_BY_POSITION,
    DEFAULT_MAX_ADP,
    DEFAULT_POOL_SIZES,
    NAIVE,
    POSITIONS,
    PROJECTION_PROVIDERS,
    PlayerRow,
    Points,
    PoolPlayer,
    ScoreRow,
    SeasonInputs,
    load_pool,
    scoreboard,
)
from lazy_sleeper.metrics import bias, mae, rmse
from lazy_sleeper.scoring import Scorer

WEEKS: tuple[int, ...] = tuple(range(1, 19))

WeeklyPoints = Mapping[int, Points]  # week → sleeper_id → pts


@dataclass(frozen=True)
class WeeklyScoreRow:
    """A provider's weekly-horizon accuracy on one position, rolled up over a season's weeks."""

    season: int
    position: str
    provider: str
    weeks: int  # weeks with at least one scored player
    n_pool: int  # Σ weekly pool sizes (player-weeks)
    n: int  # Σ player-weeks the provider projected
    mae: float  # pooled over all player-weeks
    bias: float
    rmse: float
    spearman: float  # mean of per-week Spearman ρ (NaN weeks skipped)
    spearman_min: float  # worst week — how bad does it get?
    mean_actual: float


# --- pure ---------------------------------------------------------------------------------


def weekly_pool(
    season_pool: Sequence[PoolPlayer],
    week_projections: Mapping[str, Points],
    providers: Iterable[str] = PROJECTION_PROVIDERS,
) -> list[PoolPlayer]:
    """Season pool members that at least one projection provider expected to play (> 0 pts)."""
    expected = {
        sid for name in providers for sid, pts in week_projections.get(name, {}).items() if pts > 0
    }
    return [p for p in season_pool if p.sleeper_id in expected]


def naive_weekly(
    weekly_actuals: WeeklyPoints,
    prior_season_actuals: WeeklyPoints,
    weeks: Sequence[int] = WEEKS,
) -> dict[int, dict[str, float]]:
    """Trailing per-game mean of earlier weeks this season; week 1 = prior season per-game mean."""
    out: dict[int, dict[str, float]] = {}
    totals: dict[str, float] = defaultdict(float)
    games: dict[str, int] = defaultdict(int)
    prior_pts: dict[str, float] = defaultdict(float)
    prior_gp: dict[str, int] = defaultdict(int)
    for pts in prior_season_actuals.values():
        for sid, v in pts.items():
            prior_pts[sid] += v
            prior_gp[sid] += 1
    prior_mean = {sid: prior_pts[sid] / prior_gp[sid] for sid in prior_pts}
    for w in weeks:
        out[w] = {sid: totals[sid] / games[sid] for sid in games} if games else dict(prior_mean)
        for sid, v in weekly_actuals.get(w, {}).items():
            totals[sid] += v
            games[sid] += 1
    return out


def week_inputs(
    season: int,
    week: int,
    season_pool: Sequence[PoolPlayer],
    providers: Mapping[str, WeeklyPoints],
    actuals: WeeklyPoints,
) -> SeasonInputs:
    week_proj = {name: pts.get(week, {}) for name, pts in providers.items()}
    return SeasonInputs(
        season=season,
        pool=weekly_pool(season_pool, week_proj),
        providers=week_proj,
        actuals=actuals.get(week, {}),
        week=week,
    )


def aggregate(week_rows: Sequence[ScoreRow], detail: Sequence[PlayerRow]) -> list[WeeklyScoreRow]:
    """Roll per-week ScoreRows/PlayerRows up to (season, position, provider)."""
    rhos: dict[tuple[int, str, str], list[float]] = defaultdict(list)
    n_pool: dict[tuple[int, str, str], int] = defaultdict(int)
    order: dict[str, int] = {}  # provider → first-seen rank, keeps the season table's order
    for r in week_rows:
        key = (r.season, r.position, r.provider)
        order.setdefault(r.provider, len(order))
        n_pool[key] += r.n_pool
        if r.n and not math.isnan(r.spearman):
            rhos[key].append(r.spearman)
    pairs: dict[tuple[int, str, str], tuple[list[float], list[float], set[int]]] = defaultdict(
        lambda: ([], [], set())
    )
    for d in detail:
        for provider, pts in d.projected.items():
            if pts is None:
                continue
            pred, act, weeks = pairs[(d.season, d.position, provider)]
            pred.append(pts)
            act.append(d.actual)
            weeks.add(d.week if d.week is not None else 0)
    out: list[WeeklyScoreRow] = []
    for key in sorted(n_pool, key=lambda k: (k[0], POSITIONS.index(k[1]), order[k[2]])):
        season, position, provider = key
        pred, act, weeks = pairs.get(key, ([], [], set()))
        rho = rhos.get(key, [])
        out.append(
            WeeklyScoreRow(
                season=season,
                position=position,
                provider=provider,
                weeks=len(weeks),
                n_pool=n_pool[key],
                n=len(pred),
                mae=mae(pred, act),
                bias=bias(pred, act),
                rmse=rmse(pred, act),
                spearman=sum(rho) / len(rho) if rho else math.nan,
                spearman_min=min(rho) if rho else math.nan,
                mean_actual=sum(act) / len(act) if act else math.nan,
            )
        )
    return out


def run_season(
    season: int,
    season_pool: Sequence[PoolPlayer],
    providers: Mapping[str, WeeklyPoints],
    actuals: WeeklyPoints,
    weeks: Sequence[int] = WEEKS,
) -> tuple[list[ScoreRow], list[PlayerRow]]:
    rows: list[ScoreRow] = []
    detail: list[PlayerRow] = []
    for w in weeks:
        r, d = scoreboard(week_inputs(season, w, season_pool, providers, actuals))
        rows.extend(r)
        detail.extend(d)
    return rows, detail


# --- DB assembly --------------------------------------------------------------------------


def load_weekly_projection_points(
    session: Session, scorer: Scorer, season: int, source: str, weeks: Sequence[int] = WEEKS
) -> dict[int, dict[str, float]]:
    """Latest vintage per (source, season, week), scored per player."""
    from lazy_sleeper.db.models import Projection

    latest = dict(
        session.execute(
            select(Projection.week, func.max(Projection.snapshot_id))
            .where(
                Projection.source == source,
                Projection.season == season,
                Projection.week.in_(weeks),
            )
            .group_by(Projection.week)
        ).all()
    )
    out: dict[int, dict[str, float]] = {}
    for week, snap_id in latest.items():
        pts: dict[str, float] = {}
        for sleeper_id, position, stats in session.execute(
            select(Projection.sleeper_id, Projection.position, Projection.stats).where(
                Projection.snapshot_id == snap_id,
                Projection.season == season,
                Projection.week == week,
                Projection.sleeper_id.is_not(None),
            )
        ):
            pts[sleeper_id] = scorer.score(stats, position)
        out[week] = pts
    return out


def load_weekly_actual_points(
    session: Session,
    scorer: Scorer,
    season: int,
    sources: Mapping[str, str] = ACTUAL_SOURCE_BY_POSITION,
) -> dict[int, dict[str, float]]:
    """Per-week scored actuals, one designated source per position (see season module)."""
    from lazy_sleeper.db.models import Actual

    wanted = {(src, pos) for pos, src in sources.items()}
    out: dict[int, dict[str, float]] = defaultdict(dict)
    stmt = select(
        Actual.week, Actual.source, Actual.sleeper_id, Actual.position, Actual.stats
    ).where(
        Actual.season == season,
        Actual.week.is_not(None),
        Actual.sleeper_id.is_not(None),
        Actual.source.in_(set(sources.values())),
    )
    for week, source, sleeper_id, position, stats in session.execute(stmt):
        if (source, position) in wanted:
            out[week][sleeper_id] = out[week].get(sleeper_id, 0.0) + scorer.score(stats, position)
    return dict(out)


def run(
    session: Session,
    scorer: Scorer,
    seasons: Sequence[int],
    *,
    sizes: Mapping[str, int] = DEFAULT_POOL_SIZES,
    max_adp: float = DEFAULT_MAX_ADP,
    weeks: Sequence[int] = WEEKS,
    providers: Sequence[str] = PROJECTION_PROVIDERS,
) -> tuple[list[WeeklyScoreRow], list[ScoreRow], list[PlayerRow]]:
    """(rolled-up rows, per-week rows, per-player-week detail) across seasons."""
    week_rows: list[ScoreRow] = []
    detail: list[PlayerRow] = []
    for season in seasons:
        actuals = load_weekly_actual_points(session, scorer, season)
        provider_points: dict[str, WeeklyPoints] = {
            name: load_weekly_projection_points(session, scorer, season, name, weeks)
            for name in providers
        }
        provider_points[NAIVE] = naive_weekly(
            actuals, load_weekly_actual_points(session, scorer, season - 1), weeks
        )
        pool = load_pool(session, season, sizes, max_adp)
        r, d = run_season(season, pool, provider_points, actuals, weeks)
        week_rows.extend(r)
        detail.extend(d)
    return aggregate(week_rows, detail), week_rows, detail
