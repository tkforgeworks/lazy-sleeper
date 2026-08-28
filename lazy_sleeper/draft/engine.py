"""Recompute loop (LS-34): board built once pre-draft, advice re-run on every pick.

The expensive inputs — projections → VORP → tiers → flags, ADP, the search-rank→ADP map and the
player name/position lookup — are loaded once into a :class:`BoardContext`. The hot path on each
:class:`PickEvent` is just ``DraftState.apply`` + :func:`advise` (pool filter + survival + runs +
option value), which is milliseconds for a ~500-row board. :class:`DraftEngine` owns the state,
recomputes under a lock, and always keeps the **last good** :class:`Advice`: a failed recompute
re-publishes the previous rows with ``error`` set instead of blocking the draft.

:class:`DraftRunner` runs a :class:`DraftPoller` feeding the engine on a daemon thread so a host
process (``lazy draft poll``, the API for LS-35) can read ``engine.latest`` from any thread.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lazy_sleeper.board import RosterShape, build_board, latest_adp
from lazy_sleeper.board.tiers import BoardRow, TierConfig
from lazy_sleeper.db.models import Player
from lazy_sleeper.draft.poller import DraftPoller, PickEvent, PollResult, RunSummary
from lazy_sleeper.draft.signals import SearchRankAdp, advise
from lazy_sleeper.draft.state import DraftSpec, DraftState, resolve_my_slot
from lazy_sleeper.ingest.byes import byes_for
from lazy_sleeper.providers.base import ProjectionProvider
from lazy_sleeper.scoring.league import ScoringRules

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BoardContext:
    """Everything ``advise`` needs that does not change during the draft."""

    rows: tuple[BoardRow, ...]  # unfiltered VORP-ordered board (build_board output)
    adp: Mapping[str, float]
    cfg: TierConfig
    search_rank: Mapping[str, int]
    rank_map: SearchRankAdp | None
    positions: Mapping[str, str]  # sleeper_id → position (fallback when a pick has no metadata)
    names: Mapping[str, str] = field(default_factory=dict)  # sleeper_id → "Name POS/TEAM"
    injuries: Mapping[str, str] = field(default_factory=dict)  # sleeper_id → injury_status
    season: int | None = None
    built_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    byes: Mapping[str, int] = field(default_factory=dict)  # team → bye week (LS-57)

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[BoardRow],
        adp: Mapping[str, float],
        cfg: TierConfig,
        *,
        search_rank: Mapping[str, int] | None = None,
        positions: Mapping[str, str] | None = None,
        names: Mapping[str, str] | None = None,
        injuries: Mapping[str, str] | None = None,
        season: int | None = None,
        byes: Mapping[str, int] | None = None,
    ) -> BoardContext:
        rows = tuple(rows)
        search_rank = dict(search_rank or {})
        rank_map = (
            SearchRankAdp((search_rank[sid], a) for sid, a in adp.items() if sid in search_rank)
            or None
        )
        positions = dict(positions or {})
        for r in rows:
            positions.setdefault(r.value.sleeper_id, r.value.position)
        return cls(
            rows, dict(adp), cfg, search_rank, rank_map, positions, dict(names or {}),
            dict(injuries or {}), season, byes=dict(byes or {}),
        )  # fmt: skip


def load_board_context(
    session: Session,
    provider: ProjectionProvider,
    rules: ScoringRules,
    cfg: TierConfig,
    season: int,
) -> BoardContext:
    """The pre-draft load: full board + ADP + player lookup from the DB. Call once."""
    adp = latest_adp(session, season)
    rows = build_board(provider, RosterShape.from_rules(rules), season, cfg, adp)
    players = session.execute(
        select(
            Player.sleeper_id,
            Player.full_name,
            Player.position,
            Player.team,
            Player.search_rank,
            Player.injury_status,
        )
    ).all()
    return BoardContext.from_rows(
        rows,
        adp,
        cfg,
        search_rank={p[0]: p[4] for p in players if p[4] is not None},
        positions={p[0]: p[2] for p in players if p[2]},
        names={p[0]: f"{p[1]} {p[2]}/{p[3]}" for p in players},
        injuries={p[0]: p[5] for p in players if p[5]},
        season=season,
        byes=byes_for(session, season),
    )


@dataclass(frozen=True)
class Advice:
    """One recompute's output. ``rows`` are best-pick-first; on ``error`` they are the previous
    good rows (``stale=True``) so the surface never goes blank."""

    seq: int
    pick_no: int  # the pick being decided when this was computed (state.current_pick)
    on_the_clock: int | None
    my_slot: int | None
    picks_until_my_turn: int | None
    rows: tuple[BoardRow, ...]
    computed_at: datetime
    elapsed_s: float
    error: str | None = None
    stale: bool = False

    @property
    def my_turn(self) -> bool:
        return self.my_slot is not None and self.on_the_clock == self.my_slot


EMPTY_ADVICE = Advice(0, 1, None, None, None, (), datetime.min.replace(tzinfo=UTC), 0.0)

# LS-56: how far before the time we first *saw* the latest pick Sleeper's own `last_picked`
# may sit and still be taken as that pick's exact start (the doc is read just before the picks
# in the same poll, so a fresh value trails by the poll interval + latency). Anything older
# belongs to an earlier pick — using it would start the clock early.
DOC_SLACK_S = 4.0


def _ms(v: Any) -> datetime:
    return datetime.fromtimestamp(int(v) / 1000, UTC)


@dataclass
class Timing:
    count: int = 0
    total_s: float = 0.0
    max_s: float = 0.0
    failures: int = 0

    @property
    def avg_s(self) -> float:
        return self.total_s / self.count if self.count else 0.0


class DraftEngine:
    """Draft state + last-good advice, recomputed on every pick. Thread-safe."""

    def __init__(
        self,
        board: BoardContext,
        rules: ScoringRules,
        *,
        draft_doc: Mapping[str, Any] | None = None,
        my_slot: int | None = None,
        user_id: str | None = None,
        team_names: Mapping[str, str] | None = None,
        horizon: int | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.board = board
        self._rules = rules
        self._my_slot_override = my_slot
        self._user_id = user_id
        self._team_names = dict(team_names or {})  # Sleeper user_id → team/display name
        self._horizon = horizon
        self._clock = clock
        self._lock = threading.RLock()
        self._seq = 0
        self.timing = Timing()
        self.state = DraftState(DraftSpec.build(rules), position_of=board.positions.get)
        self.latest: Advice = EMPTY_ADVICE
        # LS-56: the pick clock. `pick_started_at` moves only when `current_pick` does, so the
        # deadline is stable across recomputes within a pick and a client can tick it locally.
        self.slot_names: dict[int, str] = {}
        self.pick_timer_s: int | None = None
        self.pick_started_at: datetime | None = None
        self.set_draft(draft_doc)

    # -- draft doc / state --------------------------------------------------------------
    def set_draft(self, draft_doc: Mapping[str, Any] | None) -> None:
        """(Re)build the spec from the ``/draft/{id}`` doc; keeps seated picks. Sleeper filled
        ``draft_order`` mid-draft on the 8/21 mock, so this is called on every draft refresh."""
        with self._lock:
            doc = draft_doc or {}
            spec = DraftSpec.build(self._rules, doc)
            my_slot = resolve_my_slot(self._my_slot_override, doc.get("draft_order"), self._user_id)
            picks = [
                {"pick_no": n, "draft_slot": s, "sleeper_id": sid, "metadata": {"position": pos}}
                for n, (s, sid, pos) in self.state.picks.items()
            ]
            self.state = DraftState(spec, my_slot=my_slot, position_of=self.board.positions.get)
            if picks:
                self.state.rebuild(picks)
            order = doc.get("draft_order") or {}
            self.slot_names = {
                int(slot): self._team_names[str(uid)]
                for uid, slot in order.items()
                if slot is not None and str(uid) in self._team_names
            }
            timer = doc.get("pick_timer")
            if timer is None and isinstance(doc.get("settings"), Mapping):
                timer = doc["settings"].get("pick_timer")
            self.pick_timer_s = int(timer) if timer else None  # 0 / absent = no clock
            self._note_doc_times(doc)

    def _note_doc_times(self, doc: Mapping[str, Any]) -> None:
        """Sleeper's doc carries ``start_time`` (pick 1's clock once ``drafting``) and
        ``last_picked`` (ms) — exact for the current pick *if* it refers to it. The doc doesn't
        say which pick it means, so it is trusted only when it sits within ``DOC_SLACK_S`` of
        the time we first saw the latest pick; older values are an earlier pick's."""
        st = self.state
        if st.complete:
            return
        if not st.picks_made:
            if doc.get("status") == "drafting" and doc.get("start_time"):
                self.pick_started_at = _ms(doc["start_time"])
            return
        if not doc.get("last_picked"):
            return
        lp = _ms(doc["last_picked"])
        seen = self.pick_started_at
        if seen is None or seen - timedelta(seconds=DOC_SLACK_S) <= lp <= seen:
            self.pick_started_at = lp

    @property
    def pick_deadline(self) -> datetime | None:
        """When the current pick's clock runs out; None without a timer, a start, or a draft."""
        if self.state.complete or not self.pick_timer_s or self.pick_started_at is None:
            return None
        return self.pick_started_at + timedelta(seconds=self.pick_timer_s)

    def set_config(self, cfg: TierConfig) -> Advice:
        """Swap the draft-time dials (survival / runs / need bonus / late_rounds) and recompute.
        Dials baked into the board rows at build time (tiers, cliffs, ADP/disagreement flags,
        ``stream_depth``) need a full restart — ``DraftHost.restart``."""
        with self._lock:
            self.board = replace(self.board, cfg=cfg)
            return self.recompute()

    def rebuild(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        recompute: bool = True,
        at: datetime | None = None,
    ) -> Advice:
        """Replace all picks from ``core.draft_picks`` rows (start-up, commissioner undo) or a
        poll's parsed picks. The pick clock restarts from the rows' ``first_seen_at`` when they
        carry one (DB rows), else from ``at`` (the poll that delivered them), else it is unknown
        until the next pick."""
        with self._lock:
            rows = list(rows)
            before = self.state.current_pick
            self.state.rebuild(rows)
            if self.state.current_pick != before or self.pick_started_at is None:
                stamps = [r["first_seen_at"] for r in rows if r.get("first_seen_at")]
                self.pick_started_at = max(stamps) if stamps else at
            return self.recompute() if recompute else self.latest

    def on_pick(self, ev: PickEvent) -> Advice:
        with self._lock:
            before = self.state.current_pick
            self.state.apply(ev)
            if self.state.current_pick != before:
                self.pick_started_at = ev.first_seen_at  # ≤ one poll interval after the fact
            return self.recompute()

    def remove(self, pick_no: int) -> Advice:
        with self._lock:
            before = self.state.current_pick
            self.state.remove(pick_no)
            if self.state.current_pick != before:
                self.pick_started_at = None  # the clock for a re-opened pick is unknown
            return self.recompute()

    # -- the hot path ---------------------------------------------------------------------
    def recompute(self) -> Advice:
        with self._lock:
            st = self.state
            self._seq += 1
            t0 = self._clock()
            try:
                rows = advise(
                    self.board.rows,
                    st,
                    self.board.adp,
                    self.board.cfg,
                    search_rank_by_id=self.board.search_rank,
                    rank_map=self.board.rank_map,
                    horizon=self._horizon,
                )
            except Exception as exc:  # noqa: BLE001 — never block the draft on a bad recompute
                elapsed = self._clock() - t0
                self.timing.failures += 1
                log.exception("recompute %d failed at pick %d", self._seq, st.current_pick)
                self.latest = replace(
                    self.latest,
                    seq=self._seq,
                    pick_no=st.current_pick,
                    on_the_clock=st.on_the_clock,
                    my_slot=st.my_slot,
                    picks_until_my_turn=st.picks_until_my_turn(),
                    computed_at=datetime.now(UTC),
                    elapsed_s=elapsed,
                    error=f"{type(exc).__name__}: {exc}",
                    stale=True,
                )
                return self.latest
            elapsed = self._clock() - t0
            self.timing.count += 1
            self.timing.total_s += elapsed
            self.timing.max_s = max(self.timing.max_s, elapsed)
            log.info(
                "recompute seq=%d pick=%d on_clock=%s rows=%d elapsed=%.3fs",
                self._seq,
                st.current_pick,
                st.on_the_clock,
                len(rows),
                elapsed,
            )
            self.latest = Advice(
                self._seq,
                st.current_pick,
                st.on_the_clock,
                st.my_slot,
                st.picks_until_my_turn(),
                tuple(rows),
                datetime.now(UTC),
                elapsed,
            )
            return self.latest


class DraftRunner:
    """Run a poller into an engine on a background thread.

    ``on_pick`` seats the pick and recomputes (on the poll thread — the recompute is ms-scale, and
    doing it inline keeps ``latest`` causally after the pick). ``on_poll`` re-reads the draft doc
    whenever the poller refreshed it and rebuilds the whole state from the poll's own parsed
    rows on the first poll (picks an earlier run already delivered are not re-emitted) and on a
    commissioner undo — the payload is the truth; no DB read sits on this path (LS-62).
    """

    def __init__(
        self,
        poller: DraftPoller,
        engine: DraftEngine,
        *,
        on_pick: Callable[[PickEvent], None] | None = None,
        on_advice: Callable[[Advice], None] | None = None,
        on_poll: Callable[[PollResult], None] | None = None,
        until_complete: bool = True,
        max_polls: int | None = None,
    ) -> None:
        self.poller = poller
        self.engine = engine
        self._on_pick = on_pick
        self._on_advice = on_advice
        self._on_poll = on_poll
        self._until_complete = until_complete
        self._max_polls = max_polls
        self.stop = threading.Event()
        self.summary: RunSummary | None = None
        self.error: str | None = None  # set if run() died with an exception (LS-64)
        self.rebuild_pending = False  # a rebuild raised; redo it on the next changed poll
        self._thread: threading.Thread | None = None
        self._draft_doc: Mapping[str, Any] | None = poller.draft

    def _pick(self, ev: PickEvent) -> None:
        if self._on_pick:
            self._on_pick(ev)
        advice = self.engine.on_pick(ev)
        if self._on_advice:
            self._on_advice(advice)

    def _poll(self, r: PollResult) -> None:
        if self.poller.draft is not None and self.poller.draft is not self._draft_doc:
            self._draft_doc = self.poller.draft
            self.engine.set_draft(self._draft_doc)
            if not r.new and not r.removed:
                advice = self.engine.recompute()
                if self._on_advice:
                    self._on_advice(advice)
        if (r.removed or r.poll_seq == 1 or self.rebuild_pending) and not r.unchanged:
            self.rebuild_pending = True  # cleared only once the rebuild has gone through
            at = max(ev.first_seen_at for ev in r.new) if r.new else None
            advice = self.engine.rebuild(r.rows, at=at)
            self.rebuild_pending = False
            if self._on_advice:
                self._on_advice(advice)
        if self._on_poll:
            self._on_poll(r)

    def run(self) -> RunSummary:
        """Blocking: poll until complete / stopped. Use :meth:`start` for the background form."""
        try:
            self.summary = self.poller.run(
                self._pick,
                on_poll=self._poll,
                stop=self.stop,
                until_complete=self._until_complete,
                max_polls=self._max_polls,
            )
        except Exception as exc:
            # poller.run guards every callback, so this is a bug in the poller itself — but the
            # host must be able to show *that* the thread is gone, not just that it isn't running
            self.error = f"{type(exc).__name__}: {exc}"
            log.exception("draft runner died")
            raise
        if self.summary.fatal:
            # LS-70: the poller gave up (the draft id doesn't exist on Sleeper) — surface it the
            # same way as a dead thread so /state and the page show why nothing is happening
            self.error = self.summary.fatal
        return self.summary

    def start(self) -> threading.Thread:
        if self._thread is not None:
            return self._thread
        self._thread = threading.Thread(target=self.run, name="draft-runner", daemon=True)
        self._thread.start()
        return self._thread

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


__all__ = [
    "DOC_SLACK_S",
    "EMPTY_ADVICE",
    "Advice",
    "BoardContext",
    "DraftEngine",
    "DraftRunner",
    "Timing",
    "load_board_context",
]
