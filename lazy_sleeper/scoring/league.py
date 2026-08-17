"""DB-backed helpers: league rules from the latest league snapshot; FG distance mix from actuals."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from lazy_sleeper.ingest.snapshots import SnapshotKey, SnapshotRepository, SnapshotStore
from lazy_sleeper.scoring.kicking import BUCKET_NAMES, DistanceMix
from lazy_sleeper.scoring.rules import ScoringRules

LEAGUE_KEY = SnapshotKey(source="sleeper", kind="league")


def load_league_rules(session: Session, store: SnapshotStore) -> ScoringRules:
    """Rules from the most recent valid `sleeper/league` snapshot; raises if none exists."""
    snap = SnapshotRepository(session).latest(LEAGUE_KEY)
    if snap is None:
        raise LookupError("no valid sleeper/league snapshot — run `lazy pull league` first")
    return ScoringRules.from_league(json.loads(store.read(snap.storage_path)))


def distance_mix_from_actuals(
    session: Session,
    seasons: tuple[int, ...] = (2023, 2024, 2025),
    source: str = "nflverse",
) -> DistanceMix:
    """League-wide FG distance mix from kicker actuals — the data behind `DEFAULT_MIX`."""
    from sqlalchemy import select

    from lazy_sleeper.db.models import Actual

    stmt = select(Actual.stats).where(
        Actual.source == source, Actual.position == "K", Actual.season.in_(seasons)
    )
    counts = dict.fromkeys(BUCKET_NAMES, 0.0)
    for (stats,) in session.execute(stmt):
        for b in BUCKET_NAMES:
            v = stats.get(f"fgm_{b}")
            if isinstance(v, int | float):
                counts[b] += float(v)
    return DistanceMix.from_counts(counts)


# --- parity vs nflverse ---------------------------------------------------------

# Known, intentional differences between this league's map and nflverse `fantasy_points_ppr` (a
# fixed 4 / 0.04 / −2 INT / full-PPR / −2 fumble map, no kicking). Documented here; the parity test
# adjusts for them explicitly rather than hiding them in a tolerance.
NFLVERSE_PPR_DIFFS: dict[str, str] = {
    "pass_int": "league −1 vs nflverse −2",
    "fum_rec_td": "league +6 vs nflverse 0 (not in its formula)",
    "fum_lost": "nflverse skips fumbles lost on kick/punt returns; the league charges them",
}


def parity_rows(
    session: Session, rules: ScoringRules, season: int = 2025, source: str = "nflverse"
) -> list[dict]:
    """Weekly offense actuals for the parity fixture: position, week, ids, provider pts, stats.

    Stats are trimmed to keys the league scores plus `fum`, which is enough to score and to explain
    every delta. K is excluded — nflverse PPR excludes kicking.
    """
    from sqlalchemy import select

    from lazy_sleeper.db.models import Actual
    from lazy_sleeper.scoring.rules import OFFENSE_POSITIONS

    keep = set(rules.weights) | {"fum"}
    stmt = (
        select(Actual)
        .where(
            Actual.source == source,
            Actual.season == season,
            Actual.week.is_not(None),
            Actual.position.in_(sorted(OFFENSE_POSITIONS)),
            Actual.provider_points.is_not(None),
        )
        .order_by(Actual.week, Actual.source_player_id)
    )
    return [
        {
            "position": a.position,
            "week": a.week,
            "sleeper_id": a.sleeper_id,
            "source_player_id": a.source_player_id,
            "provider_points": a.provider_points,
            "stats": {k: v for k, v in a.stats.items() if k in keep},
        }
        for a in session.scalars(stmt)
    ]
