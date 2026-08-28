"""Draft-pick poller (LS-31) — DB-free, replayed from the recorded 2026-08-20 mock draft."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lazy_sleeper.draft.poller import (
    DraftPoller,
    MemorySink,
    Persister,
    PickEvent,
    Pulled,
    ReplayFixture,
    ReplaySource,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mock_draft_1396298350046760960.json.gz"


@pytest.fixture(scope="module")
def fx() -> ReplayFixture:
    return ReplayFixture.load(FIXTURE)


def _poller(fx: ReplayFixture, source, **kw) -> tuple[DraftPoller, MemorySink, list[float]]:  # noqa: ANN001
    sink = MemorySink(fx.draft_id)
    sleeps: list[float] = []
    kw.setdefault("clock", lambda: 0.0)  # polls take no time unless a test says otherwise
    p = DraftPoller(
        source, sink, fx.draft_id, sleep=sleeps.append, rng=lambda: 0.0, interval_s=5.0, **kw
    )
    return p, sink, sleeps


# --- fixture sanity ----------------------------------------------------------


def test_fixture_is_the_recorded_rehearsal(fx: ReplayFixture) -> None:
    assert fx.draft_id == "1396298350046760960"
    assert len(fx.picks) == 180
    assert [p["count"] for p in fx.polls] == [
        40,
        40,
        58,
        64,
        79,
        88,
        103,
        118,
        136,
        153,
        168,
        180,
        180,
        180,
    ]
    assert fx.draft["status"] == "complete"
    assert fx.draft["draft_order"] == {"1268591266036203520": 8}


# --- events ------------------------------------------------------------------


def test_replay_emits_each_pick_exactly_once_in_order(fx: ReplayFixture) -> None:
    p, sink, sleeps = _poller(fx, ReplaySource(fx))
    events: list[PickEvent] = []
    summary = p.run(events.append)

    assert [e.pick_no for e in events] == list(range(1, 181))
    assert summary.events == 180 and summary.failures == 0 and summary.complete
    assert len(sink.rows) == 180
    # The first poll delivers the 40 picks already on the board when polling started.
    assert sum(1 for e in events if e.poll_seq == 1) == 40
    # Draft doc was read on poll 1 (status drafting) → Tim's seat is resolvable.
    assert p.my_slot("1268591266036203520") == 8
    assert p.my_slot("nobody") is None


def test_event_carries_slot_autopick_and_embedded_metadata(fx: ReplayFixture) -> None:
    p, _, _ = _poller(fx, ReplaySource(fx))
    first = p.poll_once().new[0]
    assert first.pick_no == 1 and first.round == 1 and first.draft_slot == 1
    assert first.sleeper_id == "7564"
    assert first.metadata == {
        "first_name": "Ja'Marr",
        "last_name": "Chase",
        "position": "WR",
        "team": "CIN",
        "player_id": "7564",
    }
    assert first.picked_by is None  # CPU pick: Sleeper sends "" → None
    assert first.total_picks == 40


def test_identical_payload_skips_sync_but_not_the_poll(fx: ReplayFixture) -> None:
    p, sink, _ = _poller(fx, ReplaySource(fx, counts=[40, 40, 58]))
    r1, r2, r3 = p.poll_once(), p.poll_once(), p.poll_once()
    assert (len(r1.new), r1.unchanged) == (40, False)
    assert (len(r2.new), r2.unchanged, r2.picks) == (0, True, 40)
    assert (len(r3.new), r3.unchanged, r3.picks) == (18, False, 58)
    assert [e.poll_seq for e in r3.new] == [3] * 18


def test_restart_does_not_refire_picks_already_in_the_sink(fx: ReplayFixture) -> None:
    sink = MemorySink(fx.draft_id)
    sink.sync(Pulled(fx.payload(103), datetime.now(UTC)))  # a previous run got this far
    p = DraftPoller(ReplaySource(fx, counts=[103, 118]), sink, fx.draft_id, sleep=lambda _: None)
    r1, r2 = p.poll_once(), p.poll_once()
    assert r1.new == ()
    assert [e.pick_no for e in r2.new] == list(range(104, 119))


def test_commissioner_undo_is_counted_not_emitted(fx: ReplayFixture) -> None:
    p, sink, _ = _poller(fx, ReplaySource(fx, counts=[64, 60, 64]))
    p.poll_once()
    undo = p.poll_once()
    assert p.persist.flush()
    assert undo.removed == 4 and undo.removed_picks == (61, 62, 63, 64) and undo.new == ()
    assert len(sink.rows) == 60 and len(undo.rows) == 60
    redo = p.poll_once()
    assert [e.pick_no for e in redo.new] == [61, 62, 63, 64]


# --- termination -----------------------------------------------------------


def test_stops_when_pick_count_reaches_rounds_times_teams(fx: ReplayFixture) -> None:
    # Status is still "drafting" on the poll that delivers pick 180; the 15×12 count ends it.
    p, _, sleeps = _poller(fx, ReplaySource(fx, counts=[100, 180, 180, 180]))
    summary = p.run()
    assert summary.polls == 2 and summary.complete
    # one interval wait between the two polls; then the final draft refresh retries twice (2 s
    # apart) because the replay doc still says "drafting" — Sleeper flips it a beat late
    assert sleeps == [5.0, 2.0, 2.0]


def test_stops_on_status_complete_from_draft_doc(fx: ReplayFixture) -> None:
    class Src(ReplaySource):
        def draft(self) -> bytes:
            return self._fx.draft_payload(status="complete")

    p, _, _ = _poller(fx, Src(fx, counts=[40, 58]))
    summary = p.run()
    assert summary.polls == 1 and summary.complete and p.status == "complete"


def test_stop_event_and_max_polls_end_the_loop(fx: ReplayFixture) -> None:
    stop = threading.Event()
    p, _, _ = _poller(fx, ReplaySource(fx))
    summary = p.run(max_polls=3, until_complete=False)
    assert summary.polls == 3 and not summary.complete

    p2, _, _ = _poller(fx, ReplaySource(fx))
    p2.run(on_poll=lambda r: stop.set() if r.poll_seq == 2 else None, stop=stop)
    assert stop.is_set()


# --- backoff -----------------------------------------------------------------


class _Flaky:
    """Fails N times, then replays."""

    def __init__(self, inner: ReplaySource, fail: int) -> None:
        self._inner, self._fail = inner, fail

    def picks(self) -> Pulled:
        if self._fail:
            self._fail -= 1
            raise ConnectionError("boom")
        return self._inner.picks()

    def draft(self) -> bytes | None:
        return self._inner.draft()


def test_backoff_doubles_caps_and_resets_on_success(fx: ReplayFixture) -> None:
    p, _, sleeps = _poller(fx, _Flaky(ReplaySource(fx, counts=[40, 58]), fail=5), max_backoff_s=30)
    summary = p.run(max_polls=2, until_complete=False)
    # 5 failures: 10, 20, 30 (cap), 30, 30 — then one normal interval between the two good polls.
    assert sleeps == [10.0, 20.0, 30.0, 30.0, 30.0, 5.0]
    assert summary.failures == 5 and summary.polls == 2 and summary.events == 58


def test_backoff_jitter_is_bounded() -> None:
    sink = MemorySink("x")
    src = ReplaySource(ReplayFixture({}, [], []))
    hi = DraftPoller(src, sink, "x", rng=lambda: 1.0, interval_s=5.0)
    lo = DraftPoller(src, sink, "x", rng=lambda: 0.0, interval_s=5.0)
    assert lo.backoff(1) == 10.0 and hi.backoff(1) == 12.5
    assert lo.backoff(10) == 15.0  # capped: a dead network blinds the draft for ≤ 15 s (LS-65)
    assert DraftPoller(src, sink, "x", rng=lambda: 0.0, max_backoff_s=60).backoff(10) == 60.0


def test_on_pick_exception_does_not_kill_the_loop(fx: ReplayFixture) -> None:
    p, _, _ = _poller(fx, ReplaySource(fx))
    seen: list[int] = []

    def cb(ev: PickEvent) -> None:
        if ev.pick_no == 2:
            raise ValueError("consumer bug")
        seen.append(ev.pick_no)

    summary = p.run(cb)
    assert summary.events == 180 and 2 not in seen and len(seen) == 179


# --- replay source ------------------------------------------------------------


@pytest.mark.parametrize("name", ["1396298350046760960", "1397325850717749248"])
def test_every_recorded_mock_replays_cleanly(name: str) -> None:
    fx = ReplayFixture.load(FIXTURE.with_name(f"mock_draft_{name}.json.gz"))
    p, sink, _ = _poller(fx, ReplaySource(fx))
    summary = p.run()
    assert summary.complete and summary.events == 180 and summary.failures == 0
    assert len(sink.rows) == 180 and p.expected_picks == 180


def test_replay_source_reports_drafting_until_exhausted(fx: ReplayFixture) -> None:
    src = ReplaySource(fx, counts=[40])
    assert json.loads(src.draft())["status"] == "drafting"
    src.picks()
    assert json.loads(src.draft())["status"] == "complete"
    with pytest.raises(StopIteration):
        src.picks()


# --- draft-doc refresh cadence ------------------------------------------------


class _CountingSource(ReplaySource):
    """Draft doc is unsettled (no draft_order) for the first `unsettled` reads."""

    def __init__(self, fx: ReplayFixture, counts: list[int], unsettled: int) -> None:
        super().__init__(fx, counts=counts)
        self.draft_reads = 0
        self._unsettled = unsettled

    def draft(self) -> bytes:
        self.draft_reads += 1
        doc = dict(self._fx.draft)
        doc["status"] = "drafting"
        if self.draft_reads <= self._unsettled:
            doc["draft_order"] = None  # Sleeper hadn't assigned seats yet (2026-08-21 mock)
        return json.dumps(doc).encode()


def test_draft_doc_reread_every_poll_until_order_assigned_then_every_n(fx: ReplayFixture) -> None:
    src = _CountingSource(fx, counts=[40] * 12, unsettled=3)
    p, _, _ = _poller(fx, src, draft_refresh_every=5)
    p.run(max_polls=12, until_complete=False)
    # polls 1–3 unsettled → read each; settled on poll 4 (read); then only polls 5 and 10.
    assert src.draft_reads == 4 + 2
    assert p.my_slot("1268591266036203520") == 8


def test_completion_triggers_a_final_draft_refresh(fx: ReplayFixture) -> None:
    src = _CountingSource(fx, counts=[180], unsettled=0)
    p, _, _ = _poller(fx, src, draft_refresh_every=10)
    summary = p.run()
    # poll 1 + the final refresh, retried twice while the doc still says "drafting"
    assert summary.complete and src.draft_reads == 4


# --- undo + repick inside one poll window (LS-66) ------------------------------


def _swap(fx: ReplayFixture, pick_no: int, sleeper_id: str, position: str) -> list[dict]:
    """The fixture's first ``pick_no`` picks with the last one re-picked as another player."""
    picks = [dict(p) for p in fx.picks[:pick_no]]
    meta = dict(picks[-1]["metadata"])
    meta.update(position=position, player_id=sleeper_id, first_name="Re", last_name="Pick")
    picks[-1].update(player_id=sleeper_id, metadata=meta)
    return picks


class _Payloads:
    """A source over explicit pick lists (not prefixes of the fixture)."""

    def __init__(self, fx: ReplayFixture, payloads: list[list[dict]]) -> None:
        self._fx, self._payloads, self._i = fx, payloads, 0

    def picks(self) -> Pulled:
        if self._i >= len(self._payloads):
            raise StopIteration("replay exhausted")
        p = self._payloads[self._i]
        self._i += 1
        return Pulled(json.dumps(p).encode(), datetime.now(UTC))

    def draft(self) -> bytes | None:
        return self._fx.draft_payload(status="drafting")


def test_changed_player_at_an_existing_pick_no_is_a_new_event(fx: ReplayFixture) -> None:
    before = fx.picks[:57]
    after = _swap(fx, 57, "999999", "TE")
    p, sink, _ = _poller(fx, _Payloads(fx, [before, after]))
    r1, r2 = p.poll_once(), p.poll_once()
    assert len(r1.new) == 57
    assert r2.removed == 0 and [(e.pick_no, e.sleeper_id) for e in r2.new] == [(57, "999999")]
    assert r2.new[0].metadata["position"] == "TE"
    assert p.persist.flush()
    assert sink.rows[57]["sleeper_id"] == "999999" and len(sink.rows) == 57


# --- persistence off the poll thread (LS-62) -----------------------------------


class _FlakySink(MemorySink):
    """Fails the first ``fail`` sync calls (a Supabase brownout), then behaves."""

    def __init__(self, draft_id: str, fail: int, *, known_fails: bool = False) -> None:
        super().__init__(draft_id)
        self.fail = fail
        self.known_fails = known_fails
        self.attempts = 0

    def known(self):  # noqa: ANN202
        if self.known_fails:
            raise RuntimeError("db down")
        return super().known()

    def sync(self, pulled: Pulled) -> None:
        self.attempts += 1
        if self.attempts <= self.fail:
            raise RuntimeError("db hiccup")
        super().sync(pulled)


def _fast(sink: MemorySink) -> Persister:
    return Persister(sink, retry_base_s=0.01, max_backoff_s=0.05)


def test_failing_sink_never_blocks_events_and_the_db_catches_up(fx: ReplayFixture) -> None:
    sink = _FlakySink(fx.draft_id, fail=3)
    p = DraftPoller(
        ReplaySource(fx, counts=[40, 58, 64]), sink, fx.draft_id, sleep=lambda _s: None,
        persister=_fast(sink),
    )  # fmt: skip
    events: list[PickEvent] = []
    summary = p.run(events.append, max_polls=3, until_complete=False)
    # every pick was delivered on time, no poll failed, the DB errors were the writer's problem
    assert summary.polls == 3 and summary.failures == 0 and summary.events == 64
    assert [e.pick_no for e in events] == list(range(1, 65))
    assert p.persist.failures == 3 and p.persist.failures_in_a_row == 0
    # run() drained the queue before returning: the table caught up
    assert len(sink.rows) == 64 and p.persist.pending == 0 and p.persist.applied >= 3
    assert "RuntimeError: db hiccup" in (p.persist.last_error or "")


def test_known_read_failure_starts_degraded_and_refires_the_board(fx: ReplayFixture) -> None:
    sink = _FlakySink(fx.draft_id, fail=0, known_fails=True)
    sink.sync(Pulled(fx.payload(103), datetime.now(UTC)))  # a previous run got this far
    p = DraftPoller(
        ReplaySource(fx, counts=[103, 118]), sink, fx.draft_id, sleep=lambda _s: None,
        persister=_fast(sink),
    )  # fmt: skip
    r1, r2 = p.poll_once(), p.poll_once()
    assert p.degraded and len(r1.new) == 103  # nothing to diff against → all re-emitted once
    assert [e.pick_no for e in r2.new] == list(range(104, 119))  # then normal


def test_persister_bounds_its_backlog_and_close_abandons_what_a_dead_db_left() -> None:
    sink = _FlakySink("x", fail=10**6)
    ps = Persister(sink, max_pending=5, retry_base_s=0.01, max_backoff_s=0.02)
    for i in range(12):
        ps.submit_picks(Pulled(b"[]", datetime.now(UTC)), i)
    assert ps.pending == 5 and ps.dropped == 7
    assert ps.close(timeout=0.1) is False
    assert ps.pending == 0 and ps.dropped == 12 and ps.failures >= 1
    ps.submit_picks(Pulled(b"[]", datetime.now(UTC)), 99)  # after close: dropped, not queued
    assert ps.pending == 0 and ps.dropped == 13


def test_draft_doc_is_persisted_off_thread_too(fx: ReplayFixture) -> None:
    p, sink, _ = _poller(fx, ReplaySource(fx, counts=[40]))
    p.run(max_polls=1, until_complete=False)
    assert sink.draft is not None and sink.draft["draft_id"] == fx.draft_id
    assert sink.synced == 1 and len(sink.rows) == 40


def test_on_poll_exception_does_not_kill_the_loop(fx: ReplayFixture) -> None:
    """LS-64: on_poll is guarded like on_pick — the poll thread must outlive a consumer bug."""
    p, _, _ = _poller(fx, ReplaySource(fx))
    polls: list[int] = []

    def on_poll(r) -> None:  # noqa: ANN001
        polls.append(r.poll_seq)
        if r.poll_seq == 2:
            raise RuntimeError("consumer bug")

    summary = p.run(on_poll=on_poll)
    assert (
        summary.complete and summary.events == 180 and len(polls) == 12
    )  # polls after the bug ran


# --- cadence (LS-65) ---------------------------------------------------------


def test_interval_wait_is_compensated_for_the_poll_duration(fx: ReplayFixture) -> None:
    """interval_s is poll-start to poll-start: a 1.3 s fetch leaves a 3.7 s wait at 5 s, and a
    fetch slower than the interval waits 0 (never negative). Backoff waits are not compensated."""
    now = [0.0]

    def clock() -> float:
        return now[0]

    class Slow(ReplaySource):
        def picks(self) -> Pulled:
            now[0] += 1.3 if self._i < 2 else 7.0
            return super().picks()

    p, _, sleeps = _poller(fx, Slow(fx, counts=[40, 40, 40, 40]), clock=clock)
    p.run(max_polls=4, until_complete=False)
    assert sleeps == [pytest.approx(3.7), pytest.approx(3.7), 0.0]
