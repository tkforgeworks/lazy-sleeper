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
    DbPickSink,
    DraftPoller,
    SleeperPickSource,
)
from lazy_sleeper.draft.state import DraftState, TeamRoster

# --- payload ---------------------------------------------------------------------------------

ROW_FIELDS = (
    "rank",
    "sleeper_id",
    "name",
    "position",
    "team",
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


def row_dict(rank: int, r: BoardRow, names: Mapping[str, str]) -> dict[str, Any]:
    v = r.value
    return {
        "rank": rank,
        "sleeper_id": v.sleeper_id,
        "name": _name(names, v.sleeper_id),
        "position": v.position,
        "team": v.team,
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
    rows = [row_dict(i, r, names) for i, r in enumerate(a.rows, start=1)]
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
            "on_the_clock": st.on_the_clock,
            "my_slot": st.my_slot,
            "my_turn": a.my_turn and st.on_the_clock == st.my_slot,
            "my_next_pick": my_next,
            "picks_until_my_turn": st.picks_until_my_turn(),
            "picks_made": st.picks_made,
            "complete": st.complete,
        },
        "my_roster": roster_dict(st.my_roster(), names),
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
    error: str | None = None  # the runner thread died with this (poller.run never raises)
    lock: threading.Lock = field(default_factory=threading.Lock)


class DraftHost:
    """Keeps one :class:`DraftRunner` per draft id alive inside the host process.

    ``make_engine(draft_id, season)`` does the pre-draft load; ``make_poller(draft_id)`` builds
    a :class:`DraftPoller`; ``reload_rows(draft_id)`` returns ``core.draft_picks`` rows for
    restarts / undo. All three are injected so the host is testable with the replay fixture.
    """

    def __init__(
        self,
        make_engine: Callable[[str, int], DraftEngine],
        make_poller: Callable[[str], DraftPoller],
        reload_rows: Callable[[str], list[dict[str, Any]]] | None = None,
        *,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self._make_engine = make_engine
        self._make_poller = make_poller
        self._reload_rows = reload_rows
        self._clock = clock
        self._lock = threading.Lock()
        self._runs: dict[str, Running] = {}

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
        overrides the poll cadence for this run (the draft-night latency dial)."""
        with self._lock:
            cur = self._runs.get(draft_id)
            if cur is not None and cur.runner.running:
                return cur
            engine = self._make_engine(draft_id, season)
            poller = self._make_poller(draft_id)
            if interval_s is not None:
                poller.interval_s = interval_s
            reload = (
                (lambda: self._reload_rows(draft_id)) if self._reload_rows is not None else None
            )
            runner = DraftRunner(poller, engine, reload_rows=reload, until_complete=until_complete)
            run = Running(
                draft_id, season, engine, runner, started_at=self._clock() if self._clock else None
            )
            runner.start()
            self._runs[draft_id] = run
            return run

    def stop(self, draft_id: str, *, timeout: float = 10.0) -> Running | None:
        run = self._runs.get(draft_id)
        if run is None:
            return None
        run.runner.stop.set()
        run.runner.join(timeout)
        return run

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
        payload["poller"] = {
            "interval_s": run.runner.poller.interval_s,
            "status": run.runner.poller.status,
            "expected_picks": run.runner.poller.expected_picks,
            "started_at": run.started_at,
            "summary": (
                None
                if run.runner.summary is None
                else {
                    "polls": run.runner.summary.polls,
                    "events": run.runner.summary.events,
                    "failures": run.runner.summary.failures,
                    "complete": run.runner.summary.complete,
                    "stopped": run.runner.summary.stopped,
                }
            ),
        }
        return payload


# --- production wiring ----------------------------------------------------------------------


class DbDraftFactory:
    """Factories for :class:`DraftHost` backed by the DB, Sleeper and the ensemble provider.

    ``provider(session, name)`` is the one place provider names resolve (``_Ctx.provider`` /
    ``providers.make_provider``); ``puller(session)`` builds the snapshot-writing ``Puller``.
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
        max_backoff_s: float = 60.0,
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
            }

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
                ).where(DraftPick.draft_id == draft_id)
            ).all()
        return [
            {"pick_no": p[0], "draft_slot": p[1], "sleeper_id": p[2], "metadata_": p[3]}
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
        eng = DraftEngine(
            board,
            rules,
            draft_doc=self.draft_doc(draft_id),
            my_slot=self._settings.my_draft_slot,
            user_id=self._settings.sleeper_user_id,
        )
        eng.rebuild(self.pick_rows(draft_id))
        return eng

    def poller(self, draft_id: str) -> DraftPoller:
        source = SleeperPickSource(self._sessions, self._puller, self._sleeper, draft_id)
        return DraftPoller(
            source,
            DbPickSink(self._sessions, draft_id),
            draft_id,
            interval_s=self.interval_s,
            max_backoff_s=self.max_backoff_s,
        )

    def host(self) -> DraftHost:
        from datetime import UTC, datetime

        return DraftHost(self.engine, self.poller, self.pick_rows, clock=lambda: datetime.now(UTC))


__all__ = [
    "ROW_FIELDS",
    "DbDraftFactory",
    "DraftHost",
    "Running",
    "roster_dict",
    "row_dict",
    "state_payload",
]
