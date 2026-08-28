"""Recompute loop (LS-34): engine + runner, timed on the 180-pick replay. DB-free."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lazy_sleeper.board.tiers import BoardRow, TierConfig
from lazy_sleeper.board.vorp import PlayerValue
from lazy_sleeper.draft.engine import EMPTY_ADVICE, BoardContext, DraftEngine, DraftRunner
from lazy_sleeper.draft.poller import (
    DraftPoller,
    MemorySink,
    PickEvent,
    ReplayFixture,
    ReplaySource,
)
from lazy_sleeper.draft.state import DraftSpec
from lazy_sleeper.scoring.league import ScoringRules

FIXTURE = Path(__file__).parent / "fixtures" / "mock_draft_1396298350046760960.json.gz"
SHAPE = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF") + ("BN",) * 5
RULES = ScoringRules(weights={}, roster_positions=SHAPE, total_rosters=12)
ME = "1268591266036203520"  # slot 8 in the recorded mock


@pytest.fixture(scope="module")
def fx() -> ReplayFixture:
    return ReplayFixture.load(FIXTURE)


def _row(sid: str, pos: str, vorp: float, adp: float | None) -> BoardRow:
    return BoardRow(
        PlayerValue(sid, pos, "X", 100 + vorp, 100, vorp, 1, {}), tier=1, cliff=False,
        gap_to_next=None, adp=adp,
    )  # fmt: skip


def _board(fx: ReplayFixture, extra: int = 0, seed: int = 7) -> BoardContext:
    """Every drafted player (VORP ∝ 181 − first pick, ADP = that pick) plus ``extra`` undrafted
    filler players with a search_rank only — a full-sized pool, like draft night."""
    rows = [
        _row(p["player_id"], p["metadata"]["position"], float(181 - p["pick_no"]), float(p["pick_no"]))
        for p in fx.picks
    ]  # fmt: skip
    rng = random.Random(seed)
    search_rank = {p["player_id"]: p["pick_no"] for p in fx.picks}
    for i in range(extra):
        sid = f"u{i}"
        pos = rng.choice(["QB", "RB", "WR", "TE", "K", "DEF"])
        rows.append(_row(sid, pos, -float(i % 40), None))
        search_rank[sid] = 181 + i
    adp = {r.value.sleeper_id: r.adp for r in rows if r.adp is not None}
    return BoardContext.from_rows(rows, adp, TierConfig(), search_rank=search_rank)


def _ev(p: dict, spec: DraftSpec) -> PickEvent:
    n = p["pick_no"]
    return PickEvent("d", n, spec.round_of(n), p["draft_slot"], p["player_id"], None,
                     datetime.now(UTC), n, 1, {"position": p["metadata"]["position"]})  # fmt: skip


def _rows(picks: list[dict]) -> list[dict]:
    """Raw Sleeper picks → the ``core.draft_picks`` row shape ``rebuild`` reads."""
    return [
        {"pick_no": p["pick_no"], "draft_slot": p["draft_slot"], "sleeper_id": p["player_id"],
         "metadata_": p["metadata"]}
        for p in picks
    ]  # fmt: skip


def _doc(fx: ReplayFixture) -> dict:
    return {"teams": 12, "rounds": 15, "type": "snake", "draft_order": {ME: 8}}


# --- engine ----------------------------------------------------------------------------


def test_engine_starts_empty_then_recomputes_on_each_pick(fx: ReplayFixture) -> None:
    eng = DraftEngine(_board(fx), RULES, draft_doc=_doc(fx), user_id=ME)
    assert eng.latest is EMPTY_ADVICE
    spec = eng.state.spec
    first = eng.on_pick(_ev(fx.picks[0], spec))
    assert first.seq == 1 and first.pick_no == 2 and first.on_the_clock == 2
    assert first.my_slot == 8 and first.picks_until_my_turn == 6
    assert fx.picks[0]["player_id"] not in {r.value.sleeper_id for r in first.rows}
    assert first.rows[0].pick_score is not None and not first.error and not first.stale
    assert eng.latest is first
    for p in fx.picks[1:7]:
        adv = eng.on_pick(_ev(p, spec))
    assert adv.seq == 7 and adv.pick_no == 8 and adv.my_turn and adv.picks_until_my_turn == 0


def test_recompute_failure_keeps_last_good_rows_and_flags_error(
    fx: ReplayFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = DraftEngine(_board(fx), RULES, draft_doc=_doc(fx), user_id=ME)
    spec = eng.state.spec
    good = eng.on_pick(_ev(fx.picks[0], spec))

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise ValueError("bad survival")

    monkeypatch.setattr("lazy_sleeper.draft.engine.advise", boom)
    bad = eng.on_pick(_ev(fx.picks[1], spec))
    assert bad.stale and bad.error == "ValueError: bad survival"
    assert bad.rows == good.rows and bad.seq == 2 and bad.pick_no == 3
    assert eng.timing.failures == 1 and eng.timing.count == 1
    assert eng.state.picks_made == 2  # the pick was still seated
    monkeypatch.undo()
    back = eng.on_pick(_ev(fx.picks[2], spec))
    assert not back.stale and back.error is None and back.seq == 3


def test_set_draft_mid_draft_keeps_picks_and_learns_my_slot(fx: ReplayFixture) -> None:
    """Sleeper filled draft_order mid-draft on the 8/21 mock: slot unknown → known, no replay."""
    eng = DraftEngine(_board(fx), RULES, draft_doc={"teams": 12, "rounds": 15}, user_id=ME)
    spec = eng.state.spec
    for p in fx.picks[:10]:
        adv = eng.on_pick(_ev(p, spec))
    assert adv.my_slot is None and adv.picks_until_my_turn is None
    eng.set_draft(_doc(fx))
    assert eng.state.picks_made == 10 and eng.state.my_slot == 8
    adv = eng.recompute()
    assert adv.my_slot == 8 and adv.pick_no == 11 and adv.picks_until_my_turn == 6


def test_rebuild_and_remove_converge_with_apply(fx: ReplayFixture) -> None:
    a = DraftEngine(_board(fx), RULES, draft_doc=_doc(fx), user_id=ME)
    b = DraftEngine(_board(fx), RULES, draft_doc=_doc(fx), user_id=ME)
    spec = a.state.spec
    for p in fx.picks[:30]:
        a.on_pick(_ev(p, spec))
    b.rebuild(_rows(fx.picks[:30]))
    assert a.state.taken() == b.state.taken()
    assert [r.value.sleeper_id for r in a.latest.rows] == [
        r.value.sleeper_id for r in b.latest.rows
    ]
    undone = a.remove(30)
    assert undone.pick_no == 30 and fx.picks[29]["player_id"] in {
        r.value.sleeper_id for r in undone.rows
    }


# --- the acceptance test: a full 15 × 12 draft inside the timer --------------------------


def test_full_draft_recompute_is_fast(fx: ReplayFixture) -> None:
    """180 picks on a ~700-row board: avg < 10 s, worst < 30 s (LS-34 AC — ≥ 60 s of margin in the
    120 s timer). Real numbers are milliseconds; the bound is the contract."""
    eng = DraftEngine(_board(fx, extra=520), RULES, draft_doc=_doc(fx), user_id=ME)
    assert len(eng.board.rows) == 700
    spec = eng.state.spec
    for p in fx.picks:
        adv = eng.on_pick(_ev(p, spec))
        assert adv.error is None
    t = eng.timing
    assert t.count == 180 and t.failures == 0
    assert t.avg_s < 10.0, f"avg recompute {t.avg_s:.3f}s"
    assert t.max_s < 30.0, f"worst recompute {t.max_s:.3f}s"
    assert eng.state.complete and adv.on_the_clock is None and adv.rows
    assert len(adv.rows) == 700 - 180


# --- runner: poller → engine ----------------------------------------------------------------


def _runner(fx: ReplayFixture, **kw) -> tuple[DraftRunner, list]:  # noqa: ANN003
    sink = MemorySink(fx.draft_id)
    poller = DraftPoller(ReplaySource(fx), sink, fx.draft_id, sleep=lambda _s: None)
    eng = DraftEngine(_board(fx), RULES, draft_doc=_doc(fx), user_id=ME)
    seen: list = []
    runner = DraftRunner(poller, eng, on_advice=seen.append, **kw)
    return runner, seen


def test_runner_replays_the_mock_into_the_engine(fx: ReplayFixture) -> None:
    runner, seen = _runner(fx)
    summary = runner.run()
    assert summary.complete and summary.events == 180
    assert runner.engine.state.picks_made == 180 and runner.engine.timing.count >= 180
    assert seen and seen[-1] is runner.engine.latest and runner.engine.latest.pick_no == 181
    # advice is published after every pick, in pick order
    picks = [a.pick_no for a in seen if not a.stale]
    assert picks == sorted(picks)


def test_runner_background_thread_exposes_latest_and_stops(fx: ReplayFixture) -> None:
    runner, _ = _runner(fx)
    runner.start()
    runner.join(timeout=30)
    assert not runner.running and runner.summary is not None and runner.summary.complete
    assert runner.engine.latest.pick_no == 181


def test_runner_rebuilds_from_the_payload_on_first_poll_and_undo(fx: ReplayFixture) -> None:
    """Restart mid-draft: the sink already holds 40 picks, so poll 1 emits no events for them —
    the runner must still seat them, from the poll's own rows. A later undo (row count drops)
    also rebuilds from the payload. No DB read on either path (LS-62)."""
    sink = MemorySink(fx.draft_id)
    pre = DraftPoller(ReplaySource(fx, counts=[40]), sink, fx.draft_id, sleep=lambda _s: None)
    pre.run(max_polls=1)
    assert len(sink.rows) == 40
    poller = DraftPoller(
        ReplaySource(fx, counts=[40, 60, 55, 180]), sink, fx.draft_id, sleep=lambda _s: None
    )
    eng = DraftEngine(_board(fx), RULES, draft_doc=_doc(fx), user_id=ME)
    seen: list = []
    runner = DraftRunner(poller, eng, on_advice=seen.append)
    runner.run()
    assert runner.summary is not None and runner.summary.events == 145  # 40 already known, 5 redone
    assert eng.state.picks_made == 180
    # the undo poll (60 → 55) rebuilt to 55 before the final poll filled the board
    assert 55 in [a.pick_no - 1 for a in seen]


def test_undo_and_repick_inside_one_poll_window_converges(fx: ReplayFixture) -> None:
    """LS-66: poll n shows pick 57 = A; poll n+1 shows pick 57 = B (the commissioner undid A and
    the team re-picked B before the next poll). A must return to the pool, B must leave it, and
    the team's roster must hold exactly one player in that seat."""
    from tests.test_draft_poller import _Payloads, _swap

    a = fx.picks[56]["player_id"]
    payloads = [fx.picks[:57], _swap(fx, 57, "u0", "WR"), fx.picks[:56] + _swap(fx, 57, "u0", "WR")[56:] + fx.picks[57:60]]  # fmt: skip
    sink = MemorySink(fx.draft_id)
    poller = DraftPoller(_Payloads(fx, payloads), sink, fx.draft_id, sleep=lambda _s: None)
    eng = DraftEngine(_board(fx, extra=5), RULES, draft_doc=_doc(fx), user_id=ME)
    runner = DraftRunner(poller, eng, until_complete=False, max_polls=3)
    runner.run()
    st = eng.state
    taken = st.taken()
    assert "u0" in taken and a not in taken and st.picks_made == 60
    slot = fx.picks[56]["draft_slot"]
    seated = [s.sleeper_id for s in st.roster(slot).picks]
    assert seated.count("u0") == 1 and a not in seated
    assert a in {r.value.sleeper_id for r in eng.latest.rows}  # A is advisable again
    assert "u0" not in {r.value.sleeper_id for r in eng.latest.rows}


def test_runner_delivers_advice_through_a_full_db_outage(fx: ReplayFixture) -> None:
    """LS-62: the sink fails on every write for the whole draft. Advice must still be recomputed
    on every pick; the writer keeps retrying and reports its failure instead of blocking."""
    from tests.test_draft_poller import _fast, _FlakySink

    sink = _FlakySink(fx.draft_id, fail=10**6)
    poller = DraftPoller(
        ReplaySource(fx), sink, fx.draft_id, sleep=lambda _s: None, persister=_fast(sink),
        flush_timeout_s=0.1,
    )  # fmt: skip
    eng = DraftEngine(_board(fx), RULES, draft_doc=_doc(fx), user_id=ME)
    seen: list = []
    runner = DraftRunner(poller, eng, on_advice=seen.append)
    summary = runner.run()
    assert summary.complete and summary.events == 180 and summary.failures == 0
    assert eng.state.complete and eng.timing.count >= 180
    assert poller.persist.failures >= 1 and poller.persist.degraded and sink.rows == {}


def test_runner_retries_a_failed_rebuild_on_the_next_changed_poll(
    fx: ReplayFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LS-64: the undo rebuild raises once (an engine hiccup); the runner thread survives, flags
    rebuild_pending, and redoes the rebuild from the next changed payload."""
    sink = MemorySink(fx.draft_id)
    poller = DraftPoller(
        ReplaySource(fx, counts=[60, 55, 55, 70, 180]), sink, fx.draft_id, sleep=lambda _s: None
    )
    eng = DraftEngine(_board(fx), RULES, draft_doc=_doc(fx), user_id=ME)
    runner = DraftRunner(poller, eng)
    real = eng.rebuild
    calls: list[int] = []
    pending: list[bool] = []

    def flaky(rows, **kw):  # noqa: ANN001, ANN003, ANN202
        calls.append(len(rows))
        pending.append(runner.rebuild_pending)
        if len(calls) == 2:
            raise RuntimeError("rebuild hiccup")
        return real(rows, **kw)

    monkeypatch.setattr(eng, "rebuild", flaky)
    summary = runner.run()
    assert summary.complete and summary.failures == 0 and runner.error is None
    # poll 1 (60), the undo (55, raised), the retry on the next *changed* poll (70) — not on the
    # unchanged one in between — and nothing after
    assert calls == [60, 55, 70] and pending == [True, True, True]
    assert not runner.rebuild_pending and eng.state.picks_made == 180


def test_runner_records_its_own_death(fx: ReplayFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _ = _runner(fx)

    def boom(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("poller bug")

    monkeypatch.setattr(runner.poller, "run", boom)
    runner.start()
    runner.join(5)
    assert not runner.running and runner.summary is None
    assert runner.error == "RuntimeError: poller bug"


# --- LS-56: pick clock, team names ----------------------------------------------------------


def _at(s: float) -> datetime:
    return datetime(2026, 9, 4, 20, 0, 0, tzinfo=UTC) + timedelta(seconds=s)


def _ev_at(p: dict, spec: DraftSpec, at: datetime) -> PickEvent:
    return PickEvent("d", p["pick_no"], spec.round_of(p["pick_no"]), p["draft_slot"],
                     p["player_id"], None, at, p["pick_no"], 1,
                     {"position": p["metadata"]["position"]})  # fmt: skip


def test_pick_deadline_starts_when_the_pick_is_first_seen_and_holds_across_recomputes(
    fx: ReplayFixture,
) -> None:
    doc = {**_doc(fx), "settings": {"pick_timer": 120}}
    eng = DraftEngine(_board(fx), RULES, draft_doc=doc, user_id=ME)
    assert eng.pick_timer_s == 120 and eng.pick_deadline is None  # no pick seen yet
    spec = eng.state.spec
    eng.on_pick(_ev_at(fx.picks[0], spec, _at(10)))
    assert eng.pick_started_at == _at(10) and eng.pick_deadline == _at(130)
    eng.set_config(TierConfig(need_bonus=1.0))  # a recompute within the pick
    eng.set_draft(doc)  # a draft-doc refresh within the pick
    assert eng.pick_deadline == _at(130)
    eng.on_pick(_ev_at(fx.picks[0], spec, _at(50)))  # same pick re-delivered: not a new pick
    assert eng.pick_deadline == _at(130)
    eng.on_pick(_ev_at(fx.picks[1], spec, _at(60)))
    assert eng.pick_deadline == _at(180)
    eng.remove(2)  # undo: the re-opened pick has no known clock
    assert eng.pick_deadline is None


def test_pick_deadline_is_null_without_a_timer_or_after_the_draft(fx: ReplayFixture) -> None:
    eng = DraftEngine(_board(fx), RULES, draft_doc=_doc(fx), user_id=ME)
    eng.on_pick(_ev_at(fx.picks[0], eng.state.spec, _at(0)))
    assert eng.pick_timer_s is None and eng.pick_deadline is None
    eng = DraftEngine(_board(fx), RULES, draft_doc={**_doc(fx), "pick_timer": 0}, user_id=ME)
    assert eng.pick_timer_s is None
    eng = DraftEngine(_board(fx), RULES, draft_doc={**_doc(fx), "pick_timer": 90}, user_id=ME)
    spec = eng.state.spec
    for p in fx.picks:
        eng.on_pick(_ev_at(p, spec, _at(p["pick_no"])))
    assert eng.state.complete and eng.pick_deadline is None


def test_doc_last_picked_refines_the_clock_only_when_it_is_the_current_pick(
    fx: ReplayFixture,
) -> None:
    def ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    doc = {**_doc(fx), "pick_timer": 120, "status": "drafting", "start_time": ms(_at(0))}
    eng = DraftEngine(_board(fx), RULES, draft_doc=doc, user_id=ME)
    assert eng.pick_started_at == _at(0)  # the first pick is on the clock from the start
    spec = eng.state.spec
    eng.on_pick(_ev_at(fx.picks[0], spec, _at(31.5)))  # seen ≤ one poll after the real pick
    eng.set_draft({**doc, "last_picked": ms(_at(30))})  # Sleeper: the exact time, 1.5 s earlier
    assert eng.pick_started_at == _at(30)
    eng.on_pick(_ev_at(fx.picks[1], spec, _at(95)))
    eng.set_draft({**doc, "last_picked": ms(_at(30))})  # a stale doc: the previous pick
    assert eng.pick_started_at == _at(95)
    eng.set_draft({**doc, "last_picked": ms(_at(96))})  # never later than first seen
    assert eng.pick_started_at == _at(95)


def test_rebuild_restarts_the_clock_from_db_stamps_or_the_delivering_poll(
    fx: ReplayFixture,
) -> None:
    eng = DraftEngine(_board(fx), RULES, draft_doc={**_doc(fx), "pick_timer": 120}, user_id=ME)
    rows = _rows(fx.picks[:5])
    for i, r in enumerate(rows):
        r["first_seen_at"] = _at(10 * i)
    eng.rebuild(rows)  # start-up from core.draft_picks
    assert eng.pick_started_at == _at(40) and eng.state.current_pick == 6
    eng.rebuild(_rows(fx.picks[:5]), at=_at(99))  # same picks from a poll: clock unchanged
    assert eng.pick_started_at == _at(40)
    eng.rebuild(_rows(fx.picks[:7]), at=_at(70))  # new picks in the payload: that poll's time
    assert eng.pick_started_at == _at(70) and eng.pick_deadline == _at(190)
    eng.rebuild(_rows(fx.picks[:3]))  # undo via rebuild with no time: unknown
    assert eng.pick_started_at is None and eng.pick_deadline is None


def test_team_names_follow_draft_order(fx: ReplayFixture) -> None:
    names = {ME: "Lazy Sleepers", "111": "The Other Guys"}
    eng = DraftEngine(_board(fx), RULES, draft_doc=_doc(fx), user_id=ME, team_names=names)
    assert eng.slot_names == {8: "Lazy Sleepers"}
    eng.set_draft({**_doc(fx), "draft_order": {ME: 8, "111": 1, "222": 2}})
    assert eng.slot_names == {8: "Lazy Sleepers", 1: "The Other Guys"}  # 222: no user row
    eng.set_draft({**_doc(fx), "draft_order": None})
    assert eng.slot_names == {}
