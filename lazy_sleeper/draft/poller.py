"""Draft-pick poller (LS-31): poll ``/draft/{id}/picks``, emit events, persist off the poll thread.

One iteration (``poll_once``) = fetch the picks payload → diff it against what the previous poll
held, keyed on ``(pick_no, sleeper_id)`` (LS-66) → a ``PickEvent`` per pick that wasn't there →
hand the payload to the :class:`Persister`, a background thread that snapshots it and syncs
``core.draft_picks``. ``run`` loops that on a fixed interval, backing off exponentially (with
jitter) while the *fetch* fails and snapping back to the interval on the first success. The loop
never dies on an error; it stops when the draft reports ``complete`` (or the pick count reaches
``rounds × teams``), when ``max_polls`` is hit, or when the ``stop`` event is set.

Persistence is never on the critical path (LS-62). The diff runs against the poller's own memory
of the last payload, so a Supabase brownout costs snapshots and table sync *until it recovers* —
the persister retries the pending payloads with its own backoff and catches up — but never a
pick event. The one DB read is at the start of a run: ``sink.known()`` seeds the diff so a
restarted poller doesn't re-fire picks an earlier run already delivered; if that read fails the
run starts ``degraded`` and re-fires everything on the board (consumers are idempotent).
Identical consecutive payloads (by sha256) are neither diffed nor persisted (the snapshot layer
would dedup them anyway, LS-52).

The poller talks to two narrow ports so it is testable without HTTP or Postgres:

* ``PickSource`` — network only (``SleeperPickSource`` = SleeperClient; ``ReplaySource`` = the
  recorded mock-draft fixture).
* ``PickSink`` — persistence (``DbPickSink`` = snapshot + ``load_draft_picks``/``load_draft`` in
  one session per call; ``MemorySink`` = a dict, for tests).

Events are delivered synchronously on the polling thread, in ``pick_no`` order, to a plain
callback. A callback that raises is logged and skipped; the recompute loop (LS-34) owns its own
last-good fallback. Undone picks (commissioner undo) are not events but are listed in
``PollResult.removed_picks``, and every changed poll carries the full parsed pick list
(``PollResult.rows``) so consumers rebuild from the payload itself, never from the DB.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from lazy_sleeper.ingest.league_loaders import parse_draft, parse_picks
from lazy_sleeper.ingest.snapshots import SnapshotKey
from lazy_sleeper.ingest.validate import validate_json_any

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 2.0  # Sleeper tolerates this; 5 s + page refresh felt slow on the clock
DEFAULT_MAX_BACKOFF_S = 60.0

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
    picks: int
    removed: int
    new: tuple[PickEvent, ...]
    unchanged: bool  # payload identical to the previous poll → no diff, no persistence
    status: str | None  # latest known draft status, if the draft doc has been read
    complete: bool
    rows: tuple[dict[str, Any], ...] = ()  # every parsed pick this poll (empty when unchanged)
    removed_picks: tuple[int, ...] = ()  # pick_nos that vanished since the previous poll


@dataclass
class RunSummary:
    polls: int = 0
    failures: int = 0
    events: int = 0
    complete: bool = False
    stopped: bool = False


@dataclass(frozen=True)
class Pulled:
    """A picks payload as fetched. Persistence (snapshot id) happens later, off-thread."""

    payload: bytes
    pulled_at: datetime


PickKey = tuple[int, str | None]  # (pick_no, sleeper_id) — a pick_no re-picked with a different
# player after an undo is a *new* pick (LS-66), so the diff is keyed on the pair


def pick_keys(rows: Iterable[Mapping[str, Any]]) -> frozenset[PickKey]:
    return frozenset((r["pick_no"], r.get("sleeper_id")) for r in rows)


# --- ports -----------------------------------------------------------------


class PickSource(Protocol):
    def picks(self) -> Pulled: ...

    def draft(self) -> bytes | None:
        """The ``/draft/{id}`` doc, or None if this source can't provide one."""
        ...


class PickSink(Protocol):
    def known(self) -> Mapping[int, str | None]:
        """pick_no → sleeper_id currently held, to seed the diff at the start of a run."""
        ...

    def sync(self, pulled: Pulled) -> None: ...

    def store_draft(self, payload: bytes, pulled_at: datetime) -> None: ...


# --- production adapters ---------------------------------------------------


class SleeperPickSource:
    """Live source: the two Sleeper GETs, nothing else (the sink snapshots them)."""

    def __init__(self, sleeper, draft_id: str) -> None:  # noqa: ANN001 — SleeperClient
        self._sleeper = sleeper
        self._draft_id = draft_id

    def picks(self) -> Pulled:
        return Pulled(self._sleeper.draft_picks(self._draft_id), datetime.now(UTC))

    def draft(self) -> bytes | None:
        return self._sleeper.draft(self._draft_id)


class DbPickSink:
    """Production sink: snapshot the payload (raw evidence, LS-31) and ``load_draft_picks`` /
    ``load_draft`` inside one session per call. ``puller_factory(session)`` is ``_Ctx.puller``
    in the CLI; without it the payload is loaded but not snapshotted."""

    def __init__(self, session_factory, draft_id: str, puller_factory=None) -> None:  # noqa: ANN001
        self._sessions = session_factory
        self._draft_id = draft_id
        self._puller = puller_factory

    def _snapshot(self, s, kind: str, payload: bytes, pulled_at: datetime):  # noqa: ANN001, ANN202
        if self._puller is None:
            return None
        return self._puller(s).snapshot(
            SnapshotKey("sleeper", kind), payload, validate_json_any, pulled_at=pulled_at
        )

    def known(self) -> Mapping[int, str | None]:
        from sqlalchemy import select

        from lazy_sleeper.db.models import DraftPick
        from lazy_sleeper.db.session import session_scope

        with session_scope(self._sessions) as s:
            return {
                int(pn): sid
                for pn, sid in s.execute(
                    select(DraftPick.pick_no, DraftPick.sleeper_id).where(
                        DraftPick.draft_id == self._draft_id
                    )
                )
            }

    def sync(self, pulled: Pulled) -> None:
        from lazy_sleeper.db.session import session_scope
        from lazy_sleeper.ingest.league_loaders import load_draft_picks

        with session_scope(self._sessions) as s:
            snap = self._snapshot(s, "draft_picks", pulled.payload, pulled.pulled_at)
            load_draft_picks(
                s,
                pulled.payload,
                self._draft_id,
                snap.id if snap is not None else None,
                snap.pulled_at if snap is not None else pulled.pulled_at,
            )

    def store_draft(self, payload: bytes, pulled_at: datetime) -> None:
        from lazy_sleeper.db.session import session_scope
        from lazy_sleeper.ingest.league_loaders import load_draft

        with session_scope(self._sessions) as s:
            snap = self._snapshot(s, "draft", payload, pulled_at)
            load_draft(s, payload, snap.id if snap is not None else None)


# --- test / replay adapters ------------------------------------------------


class MemorySink:
    """Dict-backed sink with the same sync semantics as the table (upsert + delete-missing)."""

    def __init__(self, draft_id: str) -> None:
        self._draft_id = draft_id
        self.rows: dict[int, dict[str, Any]] = {}
        self.draft: dict[str, Any] | None = None
        self.synced = 0

    def known(self) -> Mapping[int, str | None]:
        return {pn: r.get("sleeper_id") for pn, r in self.rows.items()}

    def sync(self, pulled: Pulled) -> None:
        rows = parse_picks(pulled.payload, self._draft_id)
        self.rows = {r["pick_no"]: r for r in rows}
        self.synced += 1

    def store_draft(self, payload: bytes, pulled_at: datetime) -> None:
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
        return Pulled(self._fx.payload(n), datetime.now(UTC))

    def draft(self) -> bytes | None:
        done = self._i >= len(self._counts)
        return self._fx.draft_payload(status=None if done else "drafting")


# --- persistence, off the poll thread --------------------------------------


class Persister:
    """Applies sink calls in order on a daemon thread, retrying the head of the queue with
    backoff (1, 2, 4 … capped at ``max_backoff_s``) so a DB brownout delays persistence instead
    of losing it. Bounded: past ``max_pending`` items the oldest are dropped (and counted) —
    ``core.draft_picks`` only needs the latest payload; snapshots are evidence, not state.

    ``close`` waits up to ``timeout`` for the queue to drain, then abandons what's left so a
    stopped run (``DraftHost.restart``) can never write a stale payload over a fresh one.
    """

    def __init__(
        self,
        sink: PickSink,
        *,
        max_pending: int = 50,
        max_backoff_s: float = 15.0,
        retry_base_s: float = 1.0,
        name: str = "draft-persist",
    ) -> None:
        self._sink = sink
        self.max_pending = max_pending
        self.max_backoff_s = max_backoff_s
        self.retry_base_s = retry_base_s
        self._name = name
        self._q: deque[tuple[str, Any, Any]] = deque()
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None
        self._closing = False
        self._abandon = False
        self.applied = 0
        self.failures = 0
        self.failures_in_a_row = 0
        self.dropped = 0
        self.last_error: str | None = None

    # -- submit --------------------------------------------------------------------
    def submit_picks(self, pulled: Pulled, poll_seq: int) -> None:
        self._submit(("picks", pulled, poll_seq))

    def submit_draft(self, payload: bytes, pulled_at: datetime) -> None:
        self._submit(("draft", payload, pulled_at))

    def _submit(self, item: tuple[str, Any, Any]) -> None:
        with self._cv:
            if self._closing:
                self.dropped += 1
                return
            self._q.append(item)
            while len(self._q) > self.max_pending:
                self._q.popleft()
                self.dropped += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
                self._thread.start()
            self._cv.notify_all()

    # -- observe -------------------------------------------------------------------
    @property
    def pending(self) -> int:
        with self._cv:
            return len(self._q)

    @property
    def degraded(self) -> bool:
        """True while the sink is failing and a payload is waiting to be retried."""
        return self.failures_in_a_row > 0

    def flush(self, timeout: float | None = 10.0) -> bool:
        """Wait until everything submitted so far has been applied (or ``timeout``)."""
        with self._cv:
            return self._cv.wait_for(lambda: not self._q, timeout)

    def close(self, timeout: float | None = 10.0) -> bool:
        """Drain (bounded), then stop the thread. Returns True if nothing was abandoned."""
        with self._cv:
            self._closing = True
            self._cv.notify_all()
        drained = self.flush(timeout)
        with self._cv:
            if not drained:
                self._abandon = True
                n = len(self._q)
                self._q.clear()
                self.dropped += n
                log.warning("persist: abandoned %d pending payload(s) on close", n)
                self._cv.notify_all()
        return drained

    # -- the thread ----------------------------------------------------------------
    def _apply(self, item: tuple[str, Any, Any]) -> None:
        kind, a, b = item
        if kind == "picks":
            self._sink.sync(a)
        else:
            self._sink.store_draft(a, b)

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._q and not self._closing:
                    self._cv.wait()
                if not self._q:
                    return  # closing and drained
                item = self._q[0]
            try:
                self._apply(item)
            except Exception as exc:  # noqa: BLE001 — the writer must survive anything
                self.failures += 1
                self.failures_in_a_row += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                delay = min(
                    self.retry_base_s * 2 ** (self.failures_in_a_row - 1), self.max_backoff_s
                )
                log.warning(
                    "persist %s failed (%s); %d pending, retry %d in %.1fs",
                    item[0],
                    self.last_error,
                    len(self._q),
                    self.failures_in_a_row,
                    delay,
                )
                with self._cv:
                    if self._abandon:
                        return
                    self._cv.wait(delay)
                continue
            self.applied += 1
            self.failures_in_a_row = 0
            with self._cv:
                if self._q and self._q[0] is item:
                    self._q.popleft()
                self._cv.notify_all()


# --- the poller ------------------------------------------------------------


class DraftPoller:
    def __init__(
        self,
        source: PickSource,
        sink: PickSink,
        draft_id: str,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
        draft_refresh_every: int = 10,
        persister: Persister | None = None,
        flush_timeout_s: float = 10.0,
        sleep: Callable[[float], None] | None = None,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._source = source
        self._sink = sink
        self.draft_id = draft_id
        self.interval_s = interval_s
        self.max_backoff_s = max_backoff_s
        self.draft_refresh_every = max(1, draft_refresh_every)
        self.persist = persister if persister is not None else Persister(sink)
        self.flush_timeout_s = flush_timeout_s
        self._sleep = sleep
        self._rng = rng
        self._seq = 0
        self._last_sha: str | None = None
        self._last_count = 0
        self._known: dict[int, str | None] | None = None  # pick_no → sleeper_id, last poll
        self.draft: dict[str, Any] | None = None  # parsed /draft doc, refreshed periodically
        self.degraded = False  # the start-of-run sink read failed → diff seeded empty
        # health, for /state and the page
        self.last_poll_at: datetime | None = None
        self.last_ok_at: datetime | None = None
        self.failures_in_a_row = 0
        self.last_error: str | None = None

    # -- derived from the draft doc ------------------------------------------------
    @property
    def status(self) -> str | None:
        return self.draft.get("status") if self.draft else None

    @property
    def expected_picks(self) -> int | None:
        if not self.draft or not self.draft.get("rounds") or not self.draft.get("teams"):
            return None
        return int(self.draft["rounds"]) * int(self.draft["teams"])

    @property
    def draft_settled(self) -> bool:
        """True once the doc has a draft order and the draft is underway. Until then Sleeper may
        still be filling `draft_order` in (seen on the 2026-08-21 mock), so re-read every poll."""
        return bool(self.draft and self.draft.get("draft_order") and self.status == "drafting")

    def my_slot(self, user_id: str | None) -> int | None:
        order = (self.draft or {}).get("draft_order") or {}
        v = order.get(str(user_id)) if user_id else None
        return int(v) if v is not None else None

    # -- one iteration ------------------------------------------------------------
    def poll_once(self) -> PollResult:
        seq = self._seq + 1
        if not self.draft_settled or seq % self.draft_refresh_every == 0:
            self._refresh_draft()
        pulled = self._source.picks()
        sha = hashlib.sha256(pulled.payload).hexdigest()
        if sha == self._last_sha:
            self._seq = seq
            return self._result(seq, self._last_count, 0, (), unchanged=True)

        rows = parse_picks(pulled.payload, self.draft_id)
        now = {r["pick_no"]: r.get("sleeper_id") for r in rows}
        base = self._known if self._known is not None else self._seed_known()
        missing = object()
        total = len(rows)
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
            for r in rows
            if base.get(r["pick_no"], missing) != r.get("sleeper_id")
        )
        removed = tuple(sorted(pn for pn in base if pn not in now))
        self._seq, self._last_sha, self._last_count, self._known = seq, sha, total, now
        self.persist.submit_picks(pulled, seq)
        return self._result(
            seq, total, len(removed), new, unchanged=False, rows=tuple(rows), removed_picks=removed
        )

    def _seed_known(self) -> dict[int, str | None]:
        """What an earlier run already delivered (so a restart doesn't re-fire it). A failing
        sink here means the run starts degraded: everything on the board is re-emitted once."""
        try:
            known = dict(self._sink.known())
        except Exception as exc:  # noqa: BLE001
            self.degraded = True
            log.warning(
                "could not read known picks from the sink (%s: %s); starting degraded — "
                "every pick on the board is re-emitted",
                type(exc).__name__,
                exc,
            )
            return {}
        self.degraded = False
        return known

    def _result(
        self,
        seq: int,
        picks: int,
        removed: int,
        new: tuple[PickEvent, ...],
        *,
        unchanged: bool,
        rows: tuple[dict[str, Any], ...] = (),
        removed_picks: tuple[int, ...] = (),
    ) -> PollResult:
        exp = self.expected_picks
        complete = self.status == "complete" or (exp is not None and picks >= exp)
        return PollResult(
            seq, picks, removed, new, unchanged, self.status, complete, rows, removed_picks
        )

    def _refresh_draft(self) -> None:
        payload = self._source.draft()
        if payload is None:
            return
        self.draft = parse_draft(payload)
        self.persist.submit_draft(payload, datetime.now(UTC))

    def _final_refresh(self) -> None:
        """Sleeper flips `status` to complete a beat after the last pick; catch it so
        `core.drafts` ends with `complete` + `last_picked` instead of a stale `drafting`."""
        for attempt in range(3):  # the 8/23 mock still read `drafting` on the first try
            try:
                self._refresh_draft()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "final draft refresh failed (%s); core.drafts may show stale status", exc
                )
                return
            if self.status == "complete" or attempt == 2:
                return
            (self._sleep or time.sleep)(2.0)

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
        try:
            while not stop.is_set():
                self.last_poll_at = datetime.now(UTC)
                try:
                    result = self.poll_once()
                except Exception as exc:  # noqa: BLE001 — the loop must survive anything
                    failures += 1
                    summary.failures += 1
                    self.failures_in_a_row = failures
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    delay = self.backoff(failures)
                    log.warning(
                        "poll failed (%s); attempt %d, retrying in %.1fs",
                        self.last_error,
                        failures,
                        delay,
                    )
                    wait(delay)
                    continue

                failures = 0
                self.failures_in_a_row = 0
                self.last_error = None
                self.last_ok_at = datetime.now(UTC)
                summary.polls += 1
                log.info(
                    "poll seq=%d picks=%d removed=%d new=%d unchanged=%s status=%s pending=%d",
                    result.poll_seq,
                    result.picks,
                    result.removed,
                    len(result.new),
                    result.unchanged,
                    result.status,
                    self.persist.pending,
                )
                for ev in result.new:
                    summary.events += 1
                    log.info(
                        "pick draft=%s pick_no=%d round=%s slot=%s player=%s picked_by=%s seen=%s",
                        ev.draft_id,
                        ev.pick_no,
                        ev.round,
                        ev.draft_slot,
                        ev.sleeper_id,
                        ev.picked_by or "auto",
                        ev.first_seen_at.isoformat(),
                    )
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
                    self._final_refresh()
                    break
                if max_polls is not None and summary.polls >= max_polls:
                    break
                wait(self.interval_s)
            summary.stopped = stop.is_set()
        finally:
            self.persist.close(self.flush_timeout_s)
        return summary

    def backoff(self, failures: int) -> float:
        """interval × 2^failures, capped, with up to +25 % jitter so retries don't align."""
        base = min(self.interval_s * (2**failures), self.max_backoff_s)
        return base * (1.0 + 0.25 * self._rng())


__all__ = [
    "DEFAULT_INTERVAL_S",
    "DEFAULT_MAX_BACKOFF_S",
    "DbPickSink",
    "DraftPoller",
    "MemorySink",
    "OnPick",
    "Persister",
    "PickEvent",
    "PickKey",
    "PickSink",
    "PickSource",
    "PollResult",
    "Pulled",
    "ReplayFixture",
    "ReplaySource",
    "RunSummary",
    "SleeperPickSource",
    "pick_keys",
]
