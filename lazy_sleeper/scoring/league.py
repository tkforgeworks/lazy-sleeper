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
