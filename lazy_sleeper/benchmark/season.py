"""Season-horizon projection scoreboard (LS-23).

For each past season: take the players the market cared about preseason (top-N by Sleeper ADP per
position), score every provider's preseason season projection under the league's own rules, score
what those players actually did (sum of weekly actuals under the same rules), and report accuracy
per position. Providers:

- ``sleeper`` / ``espn`` — the latest stored preseason vintage of season-total projections
- ``naive`` — the player's *previous* season actual total (the "last year again" baseline)

Pool membership comes from ADP, not from any provider, so the comparison is on the same players
regardless of who bothered to project whom; a provider that skipped a pool player simply has a
smaller ``n`` than ``n_pool``. Pool players with no actual rows are scored 0 (drafted, never
produced) rather than dropped — dropping them would flatter every provider on busts.

The pure functions here take plain mappings so they are testable without Postgres; ``load_inputs``
does the DB assembly.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lazy_sleeper.metrics import bias, mae, rmse, spearman
from lazy_sleeper.scoring import Scorer

POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K", "DEF")

# Draft-relevant depth per position for a 12-team league (≈2 QB/TE/K/DEF, 5 RB, 6 WR per team).
DEFAULT_POOL_SIZES: Mapping[str, int] = {
    "QB": 24,
    "RB": 60,
    "WR": 72,
    "TE": 24,
    "K": 24,
    "DEF": 24,
}

# Where season actuals come from: nflverse weekly for everything it covers, ESPN weekly for DEF
# (nflverse team stats aren't ingested; ESPN weekly DEF rows bucket points-allowed exactly).
ACTUAL_SOURCE_BY_POSITION: Mapping[str, str] = {
    "QB": "nflverse",
    "RB": "nflverse",
    "WR": "nflverse",
    "TE": "nflverse",
    "K": "nflverse",
    "DEF": "espn",
}

# Sleeper's ADP tail is noise (unsigned kickers at pick 300–465); ≈25 rounds is "actually drafted".
DEFAULT_MAX_ADP = 300.0

PROJECTION_PROVIDERS: tuple[str, ...] = ("sleeper", "espn")
NAIVE = "naive"

Points = Mapping[str, float]  # sleeper_id → fantasy points


@dataclass(frozen=True)
class PoolPlayer:
    sleeper_id: str
    position: str
    adp: float


@dataclass(frozen=True)
class ScoreRow:
    """One scoreboard line: a provider's accuracy on one position's pool in one season."""

    season: int
    position: str
    provider: str
    n_pool: int
    n: int  # pool players the provider actually projected
    mae: float
    bias: float
    rmse: float
    spearman: float
    mean_actual: float  # mean actual points over the n scored players (scale for MAE)


@dataclass(frozen=True)
class PlayerRow:
    """Per-player detail behind a ScoreRow, for eyeballing outliers."""

    season: int
    position: str
    sleeper_id: str
    adp: float
    actual: float
    projected: dict[str, float | None] = field(default_factory=dict)  # provider → pts (None = n/a)


@dataclass(frozen=True)
class SeasonInputs:
    season: int
    pool: Sequence[PoolPlayer]
    providers: Mapping[str, Points]  # provider name → sleeper_id → projected pts
    actuals: Points  # sleeper_id → actual season pts (missing = 0 for pool players)


# --- pure ---------------------------------------------------------------------------------


def select_pool(
    adp_rows: Iterable[tuple[str, str | None, float | None]],
    sizes: Mapping[str, int] = DEFAULT_POOL_SIZES,
    max_adp: float = DEFAULT_MAX_ADP,
) -> list[PoolPlayer]:
    """Top-N by ADP per position (lower ADP = drafted earlier), ADP ≤ max_adp.

    Rows without ADP/position are skipped, so a position can come up short of N.
    """
    by_pos: dict[str, list[PoolPlayer]] = defaultdict(list)
    for sleeper_id, position, adp in adp_rows:
        if position in sizes and adp is not None and adp <= max_adp:
            by_pos[position].append(PoolPlayer(sleeper_id, position, float(adp)))
    pool: list[PoolPlayer] = []
    for position, n in sizes.items():
        pool.extend(sorted(by_pos.get(position, ()), key=lambda p: (p.adp, p.sleeper_id))[:n])
    return pool


def scoreboard(inputs: SeasonInputs) -> tuple[list[ScoreRow], list[PlayerRow]]:
    """Accuracy per (position, provider) over the pool, plus the per-player detail rows."""
    rows: list[ScoreRow] = []
    detail: list[PlayerRow] = []
    by_pos: dict[str, list[PoolPlayer]] = defaultdict(list)
    for p in inputs.pool:
        by_pos[p.position].append(p)

    for position in POSITIONS:
        players = by_pos.get(position, [])
        if not players:
            continue
        for p in players:
            detail.append(
                PlayerRow(
                    inputs.season,
                    position,
                    p.sleeper_id,
                    p.adp,
                    inputs.actuals.get(p.sleeper_id, 0.0),
                    {name: pts.get(p.sleeper_id) for name, pts in inputs.providers.items()},
                )
            )
        for provider, projected in inputs.providers.items():
            pred = [projected[p.sleeper_id] for p in players if p.sleeper_id in projected]
            act = [
                inputs.actuals.get(p.sleeper_id, 0.0) for p in players if p.sleeper_id in projected
            ]
            rows.append(
                ScoreRow(
                    season=inputs.season,
                    position=position,
                    provider=provider,
                    n_pool=len(players),
                    n=len(pred),
                    mae=mae(pred, act),
                    bias=bias(pred, act),
                    rmse=rmse(pred, act),
                    spearman=spearman(pred, act),
                    mean_actual=sum(act) / len(act) if act else math.nan,
                )
            )
    return rows, detail


# --- DB assembly --------------------------------------------------------------------------


def load_pool(
    session: Session, season: int, sizes: Mapping[str, int], max_adp: float = DEFAULT_MAX_ADP
) -> list[PoolPlayer]:
    """Pool from the *earliest* stored ADP snapshot for the season (closest to preseason)."""
    from lazy_sleeper.db.models import Adp, Snapshot

    snap_id = session.scalar(
        select(Adp.snapshot_id)
        .join(Snapshot, Snapshot.id == Adp.snapshot_id)
        .where(Adp.season == season)
        .order_by(Snapshot.pulled_at.asc())
        .limit(1)
    )
    if snap_id is None:
        return []
    rows = session.execute(
        select(Adp.sleeper_id, Adp.position, Adp.adp_ppr).where(Adp.snapshot_id == snap_id)
    ).all()
    return select_pool(((sid, pos, adp) for sid, pos, adp in rows), sizes, max_adp)


def load_projection_points(
    session: Session, scorer: Scorer, season: int, source: str
) -> dict[str, float]:
    """Latest season-total vintage for (source, season), scored per player."""
    from lazy_sleeper.db.models import Projection

    latest = session.scalar(
        select(func.max(Projection.snapshot_id)).where(
            Projection.source == source, Projection.season == season, Projection.week.is_(None)
        )
    )
    if latest is None:
        return {}
    out: dict[str, float] = {}
    for sleeper_id, position, stats in session.execute(
        select(Projection.sleeper_id, Projection.position, Projection.stats).where(
            Projection.snapshot_id == latest,
            Projection.season == season,
            Projection.week.is_(None),
            Projection.sleeper_id.is_not(None),
        )
    ):
        out[sleeper_id] = scorer.score(stats, position)
    return out


def load_actual_points(
    session: Session,
    scorer: Scorer,
    season: int,
    sources: Mapping[str, str] = ACTUAL_SOURCE_BY_POSITION,
) -> dict[str, float]:
    """Season totals = Σ weekly actuals scored under league rules, per player.

    Each position reads from one designated source so a player is never double-counted; the
    row's own position drives normalization (K/DEF) and must match the source's assignment.
    """
    from lazy_sleeper.db.models import Actual

    wanted = {(src, pos) for pos, src in sources.items()}
    totals: dict[str, float] = defaultdict(float)
    stmt = select(Actual.source, Actual.sleeper_id, Actual.position, Actual.stats).where(
        Actual.season == season,
        Actual.week.is_not(None),
        Actual.sleeper_id.is_not(None),
        Actual.source.in_(set(sources.values())),
    )
    for source, sleeper_id, position, stats in session.execute(stmt):
        if (source, position) in wanted:
            totals[sleeper_id] += scorer.score(stats, position)
    return dict(totals)


def load_inputs(
    session: Session,
    scorer: Scorer,
    season: int,
    *,
    sizes: Mapping[str, int] = DEFAULT_POOL_SIZES,
    max_adp: float = DEFAULT_MAX_ADP,
    providers: Sequence[str] = PROJECTION_PROVIDERS,
    naive: bool = True,
) -> SeasonInputs:
    provider_points: dict[str, Points] = {
        name: load_projection_points(session, scorer, season, name) for name in providers
    }
    if naive:
        provider_points[NAIVE] = load_actual_points(session, scorer, season - 1)
    return SeasonInputs(
        season=season,
        pool=load_pool(session, season, sizes, max_adp),
        providers=provider_points,
        actuals=load_actual_points(session, scorer, season),
    )


def run(
    session: Session,
    scorer: Scorer,
    seasons: Sequence[int],
    *,
    sizes: Mapping[str, int] = DEFAULT_POOL_SIZES,
    max_adp: float = DEFAULT_MAX_ADP,
) -> tuple[list[ScoreRow], list[PlayerRow]]:
    rows: list[ScoreRow] = []
    detail: list[PlayerRow] = []
    for season in seasons:
        r, d = scoreboard(load_inputs(session, scorer, season, sizes=sizes, max_adp=max_adp))
        rows.extend(r)
        detail.extend(d)
    return rows, detail
