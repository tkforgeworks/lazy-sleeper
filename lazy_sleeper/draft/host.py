"""Draft decision surface (LS-35): one coherent JSON view of the live draft, and the in-process
host that keeps a :class:`DraftRunner` alive per draft so the API can serve it.

* :func:`state_payload` — pure: ``DraftEngine`` → the ``/draft/{id}/state`` document (clock,
  my roster + needs, best-available rows with the LS-33 signal columns, recompute metadata).
* :class:`DraftHost` — registry of running drafts. ``start(draft_id, season)`` builds the engine
  (the one-off pre-draft board load) and a poller through injected factories and starts the
  runner on a daemon thread; ``state(draft_id)`` reads ``engine.latest`` — always the product of
  the most recent recompute, never an older cache.
* :class:`DbDraftFactory` — the production factories (Sleeper source, DB sink, board from the
  ensemble provider), shared by the API and ``lazy draft poll --advise``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from lazy_sleeper.board.tiers import BoardRow
from lazy_sleeper.draft.engine import Advice, DraftEngine, DraftRunner, load_board_context
from lazy_sleeper.draft.poller import (
    DEFAULT_INTERVAL_S,
    DEFAULT_MAX_BACKOFF_S,
    DbPickSink,
    DraftPoller,
    SleeperPickSource,
)
from lazy_sleeper.draft.state import DraftState, TeamRoster
from lazy_sleeper.ingest.byes import bye_of

# --- payload ---------------------------------------------------------------------------------

ROW_FIELDS = (
    "rank",
    "sleeper_id",
    "name",
    "position",
    "team",
    "injury_status",
    "bye",
    "points",
    "vorp",
    "pos_rank",
    "tier",
    "cliff",
    "gap_to_next",
    "adp",
    "adp_delta",
    "adp_flag",
    "disagree",
    "survival",
    "run",
    "run_count",
    "pick_score",
)


def _name(names: Mapping[str, str], sleeper_id: str | None) -> str | None:
    """``BoardContext.names`` holds "Name POS/TEAM"; the surface wants just the name."""
    if sleeper_id is None:
        return None
    label = names.get(sleeper_id)
    return label.rsplit(" ", 1)[0] if label else sleeper_id


def row_dict(
    rank: int,
    r: BoardRow,
    names: Mapping[str, str],
    injuries: Mapping[str, str] | None = None,
    byes: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    v = r.value
    injuries = injuries or {}
    return {
        "rank": rank,
        "sleeper_id": v.sleeper_id,
        "name": _name(names, v.sleeper_id),
        "position": v.position,
        "team": v.team,
        "injury_status": injuries.get(v.sleeper_id),
        "bye": bye_of(byes, v.team),
        "points": v.points,
        "vorp": v.vorp,
        "pos_rank": v.pos_rank,
        "tier": r.tier,
        "cliff": r.cliff,
        "gap_to_next": r.gap_to_next,
        "adp": r.adp,
        "adp_delta": r.adp_delta,
        "adp_flag": r.adp_flag,
        "disagree": r.disagree,
        "survival": r.survival,
        "run": r.run,
        "run_count": r.run_count,
        "pick_score": r.pick_score,
    }


def roster_dict(roster: TeamRoster | None, names: Mapping[str, str]) -> dict[str, Any] | None:
    if roster is None:
        return None
    return {
        "slot": roster.slot,
        "picks": [
            {
                "pick_no": s.pick_no,
                "sleeper_id": s.sleeper_id,
                "name": _name(names, s.sleeper_id),
                "position": s.position,
                "seat": s.seat,
            }
            for s in roster.picks
        ],
        "counts": dict(roster.counts),
        "open_starters": dict(roster.open_starters),
        "open_flex": roster.open_flex,
        "open_bench": roster.open_bench,
        "needs": {k: round(v, 3) for k, v in roster.needs().items() if v},
    }


RECENT_PICKS = 8


def recent_picks(
    st: DraftState,
    names: Mapping[str, str],
    slot_names: Mapping[int, str],
    *,
    limit: int = RECENT_PICKS,
) -> list[dict[str, Any]]:
    """The league-wide pick feed (LS-56): the last ``limit`` picks, most recent first."""
    out = []
    for n in sorted(st.picks, reverse=True)[:limit]:
        slot, sid, pos = st.picks[n]
        out.append(
            {
                "pick_no": n,
                "slot": slot,
                "team_name": slot_names.get(slot) if slot is not None else None,
                "sleeper_id": sid,
                "name": _name(names, sid),
                "position": pos,
            }
        )
    return out


def state_payload(
    engine: DraftEngine,
    draft_id: str,
    *,
    position: str | None = None,
    limit: int | None = None,
    running: bool | None = None,
) -> dict[str, Any]:
    """The ``/draft/{id}/state`` document from the engine's latest advice. ``rank`` is the overall
    pick_score order before any ``position`` filter, so a filtered view still shows where each
    player sits on the whole board."""
    a: Advice = engine.latest
    st: DraftState = engine.state
    spec = st.spec
    names = engine.board.names
    otc = st.on_the_clock
    rows = [
        row_dict(i, r, names, engine.board.injuries, engine.board.byes)
        for i, r in enumerate(a.rows, start=1)
    ]
    if position:
        pos = position.upper()
        rows = [r for r in rows if r["position"] == pos]
    if limit is not None:
        rows = rows[:limit]
    my_next = st.my_next_pick()
    t = engine.timing
    return {
        "draft_id": draft_id,
        "spec": {
            "teams": spec.teams,
            "rounds": spec.rounds,
            "type": spec.type,
            "total_picks": spec.total_picks,
        },
        "clock": {
            "current_pick": st.current_pick,
            "round": spec.round_of(st.current_pick) if not st.complete else None,
            "on_the_clock": otc,
            "on_the_clock_team_name": engine.slot_names.get(otc) if otc is not None else None,
            "my_slot": st.my_slot,
            "my_turn": a.my_turn and otc == st.my_slot,
            "my_next_pick": my_next,
            "picks_until_my_turn": st.picks_until_my_turn(),
            "picks_made": st.picks_made,
            "complete": st.complete,
            "pick_timer_s": engine.pick_timer_s,
            "pick_deadline": engine.pick_deadline,
        },
        "my_roster": roster_dict(st.my_roster(), names),
        "recent_picks": recent_picks(st, names, engine.slot_names),
        "recompute": {
            "seq": a.seq,
            "pick_no": a.pick_no,
            "computed_at": a.computed_at,
            "elapsed_ms": round(a.elapsed_s * 1000, 1),
            "stale": a.stale,
            "error": a.error,
            "count": t.count,
            "avg_ms": round(t.avg_s * 1000, 1),
            "max_ms": round(t.max_s * 1000, 1),
            "failures": t.failures,
        },
        "board": {
            "built_at": engine.board.built_at,
            "season": engine.board.season,
            "rows": len(engine.board.rows),
            "available": len(a.rows),
        },
        "running": running,
        "rows": rows,
    }


# --- host --------------------------------------------------------------------------------------


@dataclass
class Running:
    draft_id: str
    season: int
    engine: DraftEngine
    runner: DraftRunner
    started_at: Any = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def error(self) -> str | None:
        """Set when the runner thread died with an exception (LS-64); None while healthy."""
        return self.runner.error


class DraftHost:
    """Keeps one :class:`DraftRunner` per draft id alive inside the host process.

    ``make_engine(draft_id, season)`` does the pre-draft load; ``make_poller(draft_id)`` builds
    a :class:`DraftPoller`. Both are injected so the host is testable with the replay fixture.
    """

    def __init__(
        self,
        make_engine: Callable[[str, int], DraftEngine],
        make_poller: Callable[[str], DraftPoller],
        *,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self._make_engine = make_engine
        self._make_poller = make_poller
        self._clock = clock
        self._lock = threading.Lock()  # guards _runs and _starting only — never held across I/O
        self._runs: dict[str, Running] = {}
        self._starting: dict[str, threading.Lock] = {}  # per-draft: one start at a time

    def get(self, draft_id: str) -> Running | None:
        return self._runs.get(draft_id)

    def ids(self) -> list[str]:
        return sorted(self._runs)

    def start(
        self,
        draft_id: str,
        season: int,
        *,
        until_complete: bool = True,
        interval_s: float | None = None,
    ) -> Running:
        """Build and start; idempotent while a runner for ``draft_id`` is alive. ``interval_s``
        overrides the poll cadence for this run (the draft-night latency dial).

        The pre-draft load (``make_engine`` — seconds of DB work, minutes if the DB is wedged,
        LS-69) runs under a *per-draft* lock: two starts for the same draft still serialize (the
        second returns the first's runner), but a slow start for one draft never blocks another.
        """
        with self._lock:
            starting = self._starting.setdefault(draft_id, threading.Lock())
        with starting:
            with self._lock:
                cur = self._runs.get(draft_id)
            if cur is not None and cur.runner.running and not cur.runner.stop.is_set():
                return cur  # a stopped runner may still be flushing its writes; don't wait on it
            engine = self._make_engine(draft_id, season)
            poller = self._make_poller(draft_id)
            if interval_s is not None:
                poller.interval_s = interval_s
            runner = DraftRunner(poller, engine, until_complete=until_complete)
            run = Running(
                draft_id, season, engine, runner, started_at=self._clock() if self._clock else None
            )
            runner.start()
            with self._lock:
                self._runs[draft_id] = run
            return run

    def stop(self, draft_id: str, *, timeout: float = 10.0) -> Running | None:
        run = self._runs.get(draft_id)
        if run is None:
            return None
        run.runner.stop.set()
        run.runner.join(timeout)
        return run

    def restart(self, draft_id: str, *, timeout: float = 10.0) -> Running | None:
        """Stop and start again with the same season/cadence: a full board rebuild under the
        current ``board_config``; draft state comes back from the sink rows."""
        run = self.stop(draft_id, timeout=timeout)
        if run is None:
            return None
        return self.start(
            draft_id, run.season, until_complete=run.runner._until_complete,  # noqa: SLF001
            interval_s=run.runner.poller.interval_s,
        )  # fmt: skip

    def stop_all(self, *, timeout: float = 10.0) -> None:
        for did in list(self._runs):
            self.stop(did, timeout=timeout)

    def state(
        self, draft_id: str, *, position: str | None = None, limit: int | None = None
    ) -> dict[str, Any] | None:
        run = self._runs.get(draft_id)
        if run is None:
            return None
        payload = state_payload(
            run.engine, draft_id, position=position, limit=limit, running=run.runner.running
        )
        p = run.runner.poller
        payload["poller"] = {
            "interval_s": p.interval_s,
            "status": p.status,
            "expected_picks": p.expected_picks,
            "started_at": run.started_at,
            "last_poll_at": p.last_poll_at,
            "last_ok_at": p.last_ok_at,
            "failures_in_a_row": p.failures_in_a_row,
            "last_error": p.last_error,
            "degraded": p.degraded,
            "runner_error": run.error,
            "rebuild_pending": run.runner.rebuild_pending,
            "persist": {
                "pending": p.persist.pending,
                "applied": p.persist.applied,
                "failures": p.persist.failures,
                "failures_in_a_row": p.persist.failures_in_a_row,
                "dropped": p.persist.dropped,
                "last_error": p.persist.last_error,
            },
            "summary": (
                None
                if run.runner.summary is None
                else {
                    "polls": run.runner.summary.polls,
                    "events": run.runner.summary.events,
                    "failures": run.runner.summary.failures,
                    "complete": run.runner.summary.complete,
                    "stopped": run.runner.summary.stopped,
                    "fatal": run.runner.summary.fatal,
                }
            ),
        }
        return payload


# --- production wiring ----------------------------------------------------------------------


class DbDraftFactory:
    """Factories for :class:`DraftHost` backed by the DB, Sleeper and the ensemble provider.

    ``provider(session, name)`` is the one place provider names resolve (``_Ctx.provider`` /
    ``providers.make_provider``); ``puller(session)`` builds the snapshot-writing ``Puller``.
    ``sleeper`` should be a ``SleeperClient`` on the *draft* ``HttpClient`` (short timeout, no
    retries — ``Settings.draft_http_timeout_s``, LS-65), not the daily-pull one.
    """

    def __init__(  # noqa: PLR0913
        self,
        sessions,  # noqa: ANN001 — sessionmaker
        store,  # noqa: ANN001 — SnapshotStore
        sleeper,  # noqa: ANN001 — SleeperClient
        provider,  # noqa: ANN001 — Callable[[Session, str], ProjectionProvider]
        puller,  # noqa: ANN001 — Callable[[Session], Puller]
        settings,  # noqa: ANN001 — Settings
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        max_backoff_s: float = DEFAULT_MAX_BACKOFF_S,
        provider_name: str = "ensemble",
    ) -> None:
        self._sessions = sessions
        self._store = store
        self._sleeper = sleeper
        self._provider = provider
        self._puller = puller
        self._settings = settings
        self.interval_s = interval_s
        self.max_backoff_s = max_backoff_s
        self.provider_name = provider_name

    def draft_doc(self, draft_id: str) -> dict[str, Any] | None:
        from lazy_sleeper.db.models import Draft
        from lazy_sleeper.db.session import session_scope

        with session_scope(self._sessions) as s:
            row = s.get(Draft, draft_id)
            if row is None:
                return None
            return {
                "teams": row.teams,
                "rounds": row.rounds,
                "type": row.type,
                "draft_order": row.draft_order,
                "league_id": row.league_id,
                "status": row.status,
                "pick_timer": row.pick_timer,
                "start_time": row.start_time,
                "last_picked": row.last_picked,
            }

    def team_names(self, league_id: str | None = None) -> dict[str, str]:
        """Sleeper user_id → team name (display name when unset) from ``core.league_users``.
        Mock drafts carry no ``league_id``; the configured league's members are the drafters."""
        from lazy_sleeper.db.models import LeagueUser
        from lazy_sleeper.db.session import session_scope

        league = league_id or self._settings.sleeper_league_id
        with session_scope(self._sessions) as s:
            rows = s.execute(
                select(LeagueUser.user_id, LeagueUser.display_name, LeagueUser.team_name).where(
                    LeagueUser.league_id == league
                )
            ).all()
        return {uid: (team or display) for uid, display, team in rows if team or display}

    def pick_rows(self, draft_id: str) -> list[dict[str, Any]]:
        from lazy_sleeper.db.models import DraftPick
        from lazy_sleeper.db.session import session_scope

        with session_scope(self._sessions) as s:
            picks = s.execute(
                select(
                    DraftPick.pick_no,
                    DraftPick.draft_slot,
                    DraftPick.sleeper_id,
                    DraftPick.metadata_,
                    DraftPick.first_seen_at,
                ).where(DraftPick.draft_id == draft_id)
            ).all()
        return [
            {
                "pick_no": p[0],
                "draft_slot": p[1],
                "sleeper_id": p[2],
                "metadata_": p[3],
                "first_seen_at": p[4],
            }
            for p in picks
        ]

    def engine(self, draft_id: str, season: int) -> DraftEngine:
        """Pre-draft load (board + ADP + player lookup, once) → engine seeded from the DB."""
        from lazy_sleeper.board import BoardConfigRepository
        from lazy_sleeper.db.session import session_scope
        from lazy_sleeper.scoring import load_league_rules

        with session_scope(self._sessions) as s:
            rules = load_league_rules(s, self._store)
            cfg = BoardConfigRepository(s).get()
            board = load_board_context(s, self._provider(s, self.provider_name), rules, cfg, season)
        doc = self.draft_doc(draft_id)
        eng = DraftEngine(
            board,
            rules,
            draft_doc=doc,
            my_slot=self._settings.my_draft_slot,
            user_id=self._settings.sleeper_user_id,
            team_names=self.team_names((doc or {}).get("league_id")),
        )
        eng.rebuild(self.pick_rows(draft_id))
        return eng

    def poller(self, draft_id: str) -> DraftPoller:
        return DraftPoller(
            SleeperPickSource(self._sleeper, draft_id),
            DbPickSink(self._sessions, draft_id, self._puller),
            draft_id,
            interval_s=self.interval_s,
            max_backoff_s=self.max_backoff_s,
        )

    def host(self) -> DraftHost:
        from datetime import UTC, datetime

        return DraftHost(self.engine, self.poller, clock=lambda: datetime.now(UTC))


__all__ = [
    "RECENT_PICKS",
    "ROW_FIELDS",
    "DbDraftFactory",
    "DraftHost",
    "Running",
    "recent_picks",
    "roster_dict",
    "row_dict",
    "state_payload",
]
