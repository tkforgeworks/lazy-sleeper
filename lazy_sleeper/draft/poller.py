"""Draft-pick poller (LS-31): poll ``/draft/{id}/picks``, sync ``core.draft_picks``, emit events.

One iteration (``poll_once``) = snapshot the picks payload → sync the table → diff against what
the table held before → a ``PickEvent`` per pick_no that wasn't there. ``run`` loops that on a
fixed interval, backing off exponentially (with jitter) while anything in the iteration fails and
snapping back to the interval on the first success. The loop never dies on an error; it stops
when the draft reports ``complete`` (or the pick count reaches ``rounds × teams``), when
``max_polls`` is hit, or when the ``stop`` event is set.

Identical consecutive payloads (by sha256) are still snapshotted — every poll is raw evidence and
LS-36 replays them — but the DB sync is skipped, so an idle 5 s loop costs Supabase nothing.

The poller talks to two narrow ports so it is testable without HTTP or Postgres:

* ``PickSource`` — where payloads come from (``SleeperPickSource`` = Puller + SleeperClient;
  ``ReplaySource`` = the recorded mock-draft fixture).
* ``PickSink`` — where they go (``DbPickSink`` = ``load_draft_picks``/``load_draft`` in a
  session; ``MemorySink`` = a dict, for tests).

Events are delivered synchronously on the polling thread, in ``pick_no`` order, to a plain
callback. A callback that raises is logged and skipped; the pick is already in the table, and the
recompute loop (LS-34) owns its own last-good fallback. Undone picks (commissioner undo) are not
events — consumers rebuild the pool from ``core.draft_picks`` — but are counted in
``PollResult.removed``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from lazy_sleeper.ingest.league_loaders import parse_draft, parse_picks
from lazy_sleeper.ingest.snapshots import SnapshotKey
from lazy_sleeper.ingest.validate import validate_json_any

log = logging.getLogger(__name__)

OnPick = Callable[["PickEvent"], None]

# --- data ------------------------------------------------------------------


@dataclass(frozen=True)
class PickEvent:
    """A pick that appeared in the payload for the first time this run."""

    draft_id: str
    pick_no: int
    round: int | None
    draft_slot: int | None
    sleeper_id: str | None
    picked_by: str | None  # None = autopick (Sleeper sends "")
    first_seen_at: datetime
    total_picks: int  # picks in the draft after this poll
    poll_seq: int  # nth successful poll of this run
    metadata: dict[str, Any] | None = None  # Sleeper's embedded name/position/team


@dataclass(frozen=True)
class PollResult:
    poll_seq: int
    snapshot_id: int | None
    picks: int
    removed: int
    new: tuple[PickEvent, ...]
    unchanged: bool  # payload identical to the previous poll → sync skipped
    status: str | None  # latest known draft status, if the draft doc has been read
    complete: bool


@dataclass
class RunSummary:
    polls: int = 0
    failures: int = 0
    events: int = 0
    complete: bool = False
    stopped: bool = False


@dataclass(frozen=True)
class Pulled:
    """A picks payload plus the snapshot row that recorded it."""

    payload: bytes
    snapshot_id: int | None
    pulled_at: datetime


@dataclass(frozen=True)
class SyncResult:
    before: frozenset[int]  # pick_nos the sink held before this sync
    rows: tuple[dict[str, Any], ...]  # parsed rows now in the sink, pick_no order
    removed: int


# --- ports -----------------------------------------------------------------


class PickSource(Protocol):
    def picks(self) -> Pulled: ...

    def draft(self) -> bytes | None:
        """The ``/draft/{id}`` doc, or None if this source can't provide one."""
        ...


class PickSink(Protocol):
    def sync(self, pulled: Pulled) -> SyncResult: ...

    def store_draft(self, payload: bytes) -> None: ...


# --- production adapters ---------------------------------------------------


class SleeperPickSource:
    """Live source: every call snapshots through a Puller in its own committed session (raw
    evidence, LS-31 AC). ``puller_factory(session)`` is ``_Ctx.puller`` in the CLI."""

    def __init__(self, session_factory, puller_factory, sleeper, draft_id: str) -> None:  # noqa: ANN001
        self._sessions = session_factory
        self._puller = puller_factory
        self._sleeper = sleeper
        self._draft_id = draft_id

    def _snapshot(self, kind: str, payload: bytes) -> tuple[int, datetime]:
        from lazy_sleeper.db.session import session_scope

        with session_scope(self._sessions) as s:
            snap = self._puller(s).snapshot(
                SnapshotKey("sleeper", kind), payload, validate_json_any
            )
            return snap.id, snap.pulled_at

    def picks(self) -> Pulled:
        payload = self._sleeper.draft_picks(self._draft_id)
        snapshot_id, pulled_at = self._snapshot("draft_picks", payload)
        return Pulled(payload, snapshot_id, pulled_at)

    def draft(self) -> bytes | None:
        payload = self._sleeper.draft(self._draft_id)
        self._snapshot("draft", payload)
        return payload


class DbPickSink:
    """Production sink: ``load_draft_picks`` / ``load_draft`` inside one session per call."""

    def __init__(self, session_factory, draft_id: str) -> None:  # noqa: ANN001 — sessionmaker
        self._sessions = session_factory
        self._draft_id = draft_id

    def sync(self, pulled: Pulled) -> SyncResult:
        from sqlalchemy import select

        from lazy_sleeper.db.models import DraftPick
        from lazy_sleeper.db.session import session_scope
        from lazy_sleeper.ingest.league_loaders import load_draft_picks

        rows = parse_picks(pulled.payload, self._draft_id)
        with session_scope(self._sessions) as s:
            before = frozenset(
                s.execute(
                    select(DraftPick.pick_no).where(DraftPick.draft_id == self._draft_id)
                ).scalars()
            )
            _, removed = load_draft_picks(
                s, pulled.payload, self._draft_id, pulled.snapshot_id, pulled.pulled_at
            )
        return SyncResult(before, tuple(rows), removed)

    def store_draft(self, payload: bytes) -> None:
        from lazy_sleeper.db.session import session_scope
        from lazy_sleeper.ingest.league_loaders import load_draft

        with session_scope(self._sessions) as s:
            load_draft(s, payload, None)


# --- test / replay adapters ------------------------------------------------


class MemorySink:
    """Dict-backed sink with the same sync semantics as the table (upsert + delete-missing)."""

    def __init__(self, draft_id: str) -> None:
        self._draft_id = draft_id
        self.rows: dict[int, dict[str, Any]] = {}
        self.draft: dict[str, Any] | None = None

    def sync(self, pulled: Pulled) -> SyncResult:
        rows = parse_picks(pulled.payload, self._draft_id)
        before = frozenset(self.rows)
        now = {r["pick_no"] for r in rows}
        removed = len(before - now)
        self.rows = {r["pick_no"]: r for r in rows}
        return SyncResult(before, tuple(rows), removed)

    def store_draft(self, payload: bytes) -> None:
        self.draft = parse_draft(payload)


@dataclass
class ReplayFixture:
    """The recorded mock draft: one full pick list + the pick count each poll saw.

    Every poll of the rehearsal was a strict prefix of the final list (verified 2026-08-20),
    so the fixture stores the final 180 picks once and replays poll *i* as ``picks[:count_i]``.
    """

    draft: dict[str, Any]
    polls: list[dict[str, Any]]
    picks: list[dict[str, Any]]

    @classmethod
    def load(cls, path: Path) -> ReplayFixture:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            d = json.load(f)
        return cls(draft=d["draft"], polls=d["polls"], picks=d["picks"])

    @property
    def draft_id(self) -> str:
        return str(self.draft["draft_id"])

    def payload(self, count: int) -> bytes:
        return json.dumps(self.picks[:count]).encode()

    def draft_payload(self, *, status: str | None = None) -> bytes:
        doc = dict(self.draft)
        if status is not None:
            doc["status"] = status
        return json.dumps(doc).encode()


class ReplaySource:
    """Feeds recorded polls in order; ``picks()`` past the end raises ``StopIteration``.

    The draft doc reports ``drafting`` until the last recorded poll has been served, then the
    fixture's real status (``complete``) — mirroring what the live endpoint did.
    """

    def __init__(self, fixture: ReplayFixture, *, counts: list[int] | None = None) -> None:
        self._fx = fixture
        self._counts = counts if counts is not None else [p["count"] for p in fixture.polls]
        self._i = 0

    def picks(self) -> Pulled:
        if self._i >= len(self._counts):
            raise StopIteration("replay exhausted")
        n = self._counts[self._i]
        self._i += 1
        return Pulled(self._fx.payload(n), None, datetime.now(UTC))

    def draft(self) -> bytes | None:
        done = self._i >= len(self._counts)
        return self._fx.draft_payload(status=None if done else "drafting")


# --- the poller ------------------------------------------------------------


class DraftPoller:
    def __init__(
        self,
        source: PickSource,
        sink: PickSink,
        draft_id: str,
        *,
        interval_s: float = 5.0,
        max_backoff_s: float = 60.0,
        draft_refresh_every: int = 10,
        sleep: Callable[[float], None] | None = None,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._source = source
        self._sink = sink
        self.draft_id = draft_id
        self.interval_s = interval_s
        self.max_backoff_s = max_backoff_s
        self.draft_refresh_every = max(1, draft_refresh_every)
        self._sleep = sleep
        self._rng = rng
        self._seq = 0
        self._last_sha: str | None = None
        self._last_count = 0
        self.draft: dict[str, Any] | None = None  # parsed /draft doc, refreshed periodically

    # -- derived from the draft doc ------------------------------------------------
    @property
    def status(self) -> str | None:
        return self.draft.get("status") if self.draft else None

    @property
    def expected_picks(self) -> int | None:
        if not self.draft or not self.draft.get("rounds") or not self.draft.get("teams"):
            return None
        return int(self.draft["rounds"]) * int(self.draft["teams"])

    def my_slot(self, user_id: str | None) -> int | None:
        order = (self.draft or {}).get("draft_order") or {}
        v = order.get(str(user_id)) if user_id else None
        return int(v) if v is not None else None

    # -- one iteration ------------------------------------------------------------
    def poll_once(self) -> PollResult:
        seq = self._seq + 1
        if seq == 1 or seq % self.draft_refresh_every == 0:
            self._refresh_draft()
        pulled = self._source.picks()
        sha = hashlib.sha256(pulled.payload).hexdigest()
        if sha == self._last_sha:
            self._seq = seq
            return self._result(seq, pulled.snapshot_id, self._last_count, 0, (), unchanged=True)

        res = self._sink.sync(pulled)
        total = len(res.rows)
        new = tuple(
            PickEvent(
                draft_id=self.draft_id,
                pick_no=r["pick_no"],
                round=r.get("round"),
                draft_slot=r.get("draft_slot"),
                sleeper_id=r.get("sleeper_id"),
                picked_by=r.get("picked_by"),
                first_seen_at=pulled.pulled_at,
                total_picks=total,
                poll_seq=seq,
                metadata=r.get("metadata_"),
            )
            for r in res.rows
            if r["pick_no"] not in res.before
        )
        self._seq, self._last_sha, self._last_count = seq, sha, total
        return self._result(seq, pulled.snapshot_id, total, res.removed, new, unchanged=False)

    def _result(
        self,
        seq: int,
        snapshot_id: int | None,
        picks: int,
        removed: int,
        new: tuple[PickEvent, ...],
        *,
        unchanged: bool,
    ) -> PollResult:
        exp = self.expected_picks
        complete = self.status == "complete" or (exp is not None and picks >= exp)
        return PollResult(seq, snapshot_id, picks, removed, new, unchanged, self.status, complete)

    def _refresh_draft(self) -> None:
        payload = self._source.draft()
        if payload is None:
            return
        self.draft = parse_draft(payload)
        self._sink.store_draft(payload)

    # -- the loop -----------------------------------------------------------------
    def run(
        self,
        on_pick: OnPick | None = None,
        *,
        until_complete: bool = True,
        stop: threading.Event | None = None,
        max_polls: int | None = None,
        on_poll: Callable[[PollResult], None] | None = None,
    ) -> RunSummary:
        stop = stop or threading.Event()
        wait = self._sleep or stop.wait
        summary = RunSummary()
        failures = 0
        while not stop.is_set():
            try:
                result = self.poll_once()
            except Exception as exc:  # noqa: BLE001 — the loop must survive anything
                failures += 1
                summary.failures += 1
                delay = self.backoff(failures)
                log.warning(
                    "poll failed (%s: %s); attempt %d, retrying in %.1fs",
                    type(exc).__name__,
                    exc,
                    failures,
                    delay,
                )
                wait(delay)
                continue

            failures = 0
            summary.polls += 1
            for ev in result.new:
                summary.events += 1
                if on_pick is None:
                    continue
                try:
                    on_pick(ev)
                except Exception:  # noqa: BLE001
                    log.exception("on_pick failed for pick %d", ev.pick_no)
            if on_poll is not None:
                on_poll(result)
            if until_complete and result.complete:
                summary.complete = True
                break
            if max_polls is not None and summary.polls >= max_polls:
                break
            wait(self.interval_s)
        summary.stopped = stop.is_set()
        return summary

    def backoff(self, failures: int) -> float:
        """interval × 2^failures, capped, with up to +25 % jitter so retries don't align."""
        base = min(self.interval_s * (2**failures), self.max_backoff_s)
        return base * (1.0 + 0.25 * self._rng())


__all__ = [
    "DbPickSink",
    "DraftPoller",
    "MemorySink",
    "OnPick",
    "PickEvent",
    "PickSink",
    "PickSource",
    "PollResult",
    "Pulled",
    "ReplayFixture",
    "ReplaySource",
    "RunSummary",
    "SleeperPickSource",
    "SyncResult",
]
