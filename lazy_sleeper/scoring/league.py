"""Load `ScoringRules` from the latest valid Sleeper league snapshot."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from lazy_sleeper.ingest.snapshots import SnapshotKey, SnapshotRepository, SnapshotStore
from lazy_sleeper.scoring.rules import ScoringRules

LEAGUE_KEY = SnapshotKey(source="sleeper", kind="league")


def load_league_rules(session: Session, store: SnapshotStore) -> ScoringRules:
    """Rules from the most recent valid `sleeper/league` snapshot; raises if none exists."""
    snap = SnapshotRepository(session).latest(LEAGUE_KEY)
    if snap is None:
        raise LookupError("no valid sleeper/league snapshot — run `lazy pull league` first")
    return ScoringRules.from_league(json.loads(store.read(snap.storage_path)))
