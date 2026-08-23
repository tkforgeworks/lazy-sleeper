"""Turn a polled draft's snapshots into an offline replay fixture (LS-36).

Every poll of a draft lands in ``raw.snapshots`` (``sleeper/draft_picks``). This rebuilds the
``ReplayFixture`` shape from them: the final pick list once (metadata trimmed to name/position/
team) plus each poll's pick count, so ``ReplaySource`` can replay the night poll-by-poll with no
network and no DB — the CI/regression form of a rehearsal. Polls that are not a strict prefix
of the final list (a commissioner undo) are kept as their own ``count`` only if they *are* a prefix;
otherwise they are reported and dropped, since the fixture format stores one pick list.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lazy_sleeper.db.models import Snapshot
from lazy_sleeper.ingest.league_loaders import parse_draft
from lazy_sleeper.ingest.snapshots import SnapshotStore
from lazy_sleeper.ingest.validate import parse_json

PICK_KEYS = (
    "draft_id",
    "pick_no",
    "round",
    "draft_slot",
    "roster_id",
    "picked_by",
    "player_id",
    "is_keeper",
)
META_KEYS = ("first_name", "last_name", "position", "team", "player_id")


@dataclass
class FixtureBuild:
    draft: dict[str, Any]
    polls: list[dict[str, Any]]
    picks: list[dict[str, Any]]
    dropped: list[dict[str, Any]] = field(default_factory=list)  # non-prefix polls (undo)
    snapshots_scanned: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"draft": self.draft, "polls": self.polls, "picks": self.picks}

    def write(self, path: Path) -> int:
        data = json.dumps(self.as_dict(), separators=(",", ":")).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wb", compresslevel=9) as f:
            f.write(data)
        return path.stat().st_size


def _trim_pick(p: dict[str, Any]) -> dict[str, Any]:
    out = {k: p.get(k) for k in PICK_KEYS}
    meta = p.get("metadata") or {}
    out["metadata"] = {k: meta.get(k) for k in META_KEYS if meta.get(k) is not None}
    return out


def _is_prefix(shorter: list[dict[str, Any]], longer: list[dict[str, Any]]) -> bool:
    if len(shorter) > len(longer):
        return False
    return all(a["pick_no"] == b["pick_no"] and a.get("player_id") == b.get("player_id")
               for a, b in zip(shorter, longer, strict=False))  # fmt: skip


def build_fixture(
    session: Session,
    store: SnapshotStore,
    draft_id: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> FixtureBuild:
    """Scan ``sleeper/draft_picks`` (and ``draft``) snapshots, keep those for ``draft_id``."""
    stmt = select(Snapshot).where(
        Snapshot.source == "sleeper", Snapshot.kind.in_(("draft_picks", "draft"))
    )
    if since is not None:
        stmt = stmt.where(Snapshot.pulled_at >= since)
    if until is not None:
        stmt = stmt.where(Snapshot.pulled_at <= until)
    stmt = stmt.order_by(Snapshot.pulled_at, Snapshot.id)

    draft_doc: dict[str, Any] | None = None
    polls: list[tuple[Snapshot, list[dict[str, Any]]]] = []
    scanned = 0
    for snap in session.scalars(stmt):
        scanned += 1
        try:
            payload = store.read(snap.storage_path)
        except FileNotFoundError:
            continue
        if snap.kind == "draft":
            try:
                doc = parse_draft(payload)
            except ValueError:
                continue
            if str(doc.get("draft_id")) == str(draft_id):
                draft_doc = doc  # latest wins (status flips to complete at the end)
            continue
        data = parse_json(payload)
        if not isinstance(data, list):
            continue
        picks = [p for p in data if isinstance(p, dict) and str(p.get("draft_id")) == draft_id]
        if not picks and data:
            continue  # another draft's polls
        if not picks and not data and not polls:
            # an empty payload is ambiguous (pre-draft poll of *some* draft); keep it only
            # once we've already seen this draft so the fixture starts where the poll loop did
            continue
        picks.sort(key=lambda p: p["pick_no"])
        polls.append((snap, [_trim_pick(p) for p in picks]))

    if not polls:
        raise ValueError(f"no draft_picks snapshots for draft {draft_id} in that window")
    final = max((p for _, p in polls), key=len)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for snap, picks in polls:
        entry = {
            "snapshot_id": snap.id,
            "pulled_at": snap.pulled_at.isoformat(),
            "count": len(picks),
        }
        (kept if _is_prefix(picks, final) else dropped).append(entry)
    if draft_doc is None:
        draft_doc = {"draft_id": draft_id, "status": "complete"}
    settings = draft_doc.get("settings") or {}
    expected = (settings.get("rounds") or 0) * (settings.get("teams") or 0)
    if expected and len(final) >= expected and draft_doc.get("status") != "complete":
        # Sleeper flips status a beat after the last pick; the poller's final read can miss it
        draft_doc = {**draft_doc, "status": "complete", "status_note": "set by fixture builder"}
    return FixtureBuild(draft_doc, kept, final, dropped, scanned)


def summarize(b: FixtureBuild) -> Iterable[str]:
    yield f"draft {b.draft.get('draft_id')} status={b.draft.get('status')} picks={len(b.picks)}"
    yield (
        f"polls kept={len(b.polls)} dropped(non-prefix)={len(b.dropped)} "
        f"scanned={b.snapshots_scanned}"
    )
    if b.polls:
        counts = [p["count"] for p in b.polls]
        yield (
            f"counts {counts[0]} ... {counts[-1]} over {b.polls[0]['pulled_at']} -> "
            f"{b.polls[-1]['pulled_at']}"
        )


__all__ = ["FixtureBuild", "build_fixture", "summarize"]
