"""LS-77: a runner started early idles until the draft is close, and the page can stop it.

DB-free: a scripted source hands out draft docs and empty pick lists; the wall clock and the
sleeps are injected so an afternoon of waiting runs in milliseconds.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from lazy_sleeper.draft.poller import DraftPoller, MemorySink, Pulled
from lazy_sleeper.draft.render import draft_page

T0 = datetime(2026, 9, 4, 20, 0, tzinfo=UTC)  # "now" at the start of every test
MIN = timedelta(minutes=1)


def _doc(status: str = "pre_draft", start: datetime | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {
        "draft_id": "d1",
        "status": status,
        "settings": {"rounds": 2, "teams": 2},
        "draft_order": None,
    }
    if start is not None:
        d["start_time"] = int(start.timestamp() * 1000)
    return d


class ScriptedSource:
    """``draft()`` serves ``docs`` in order (the last one forever); ``picks()`` is always empty."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self.doc_reads = 0
        self.pick_polls = 0

    def picks(self) -> Pulled:
        self.pick_polls += 1
        return Pulled(b"[]", datetime.now(UTC))

    def draft(self) -> bytes | None:
        doc = self._docs[min(self.doc_reads, len(self._docs) - 1)]
        self.doc_reads += 1
        return json.dumps(doc).encode()


class Harness:
    """Injected wall clock + sleep: every wait advances the clock; ``stop_when`` ends the run."""

    def __init__(self, source: ScriptedSource, *, idle_before_min: float = 30.0, **kw: Any):
        self.now = T0
        self.sleeps: list[float] = []
        self.stop = threading.Event()
        self.source = source
        self.stop_when = lambda: False

        def sleep(d: float) -> None:
            self.sleeps.append(d)
            self.now += timedelta(seconds=d)
            if self.stop_when():
                self.stop.set()

        self.poller = DraftPoller(
            source,
            MemorySink("d1"),
            "d1",
            interval_s=2.0,
            idle_before_start_s=60.0 * idle_before_min,
            idle_poll_s=60.0,
            sleep=sleep,
            rng=lambda: 0.0,
            clock=lambda: 0.0,
            wall=lambda: self.now,
            **kw,
        )

    def run(self, **kw: Any):  # noqa: ANN201
        return self.poller.run(stop=self.stop, **kw)


def test_first_poll_seeds_then_idles_until_the_window_then_polls() -> None:
    src = ScriptedSource([_doc(start=T0 + 120 * MIN)])  # draft in two hours
    h = Harness(src)
    h.stop_when = lambda: src.pick_polls >= 3
    summary = h.run()

    p = h.poller
    assert summary.stopped and src.pick_polls == 3
    # poll 1 waits its own 2 s, then 2 h out with a 30 min window = 90 min of idling in ≤ 60 s
    # ticks (the last one trimmed so the wake is not overshot), then polling resumed
    assert p.idle_ticks == 90 and p.mode == "polling" and p.next_check_at is None
    assert h.sleeps[0] == 2.0
    idle_waits = h.sleeps[1 : 1 + p.idle_ticks]
    assert max(idle_waits) == 60.0 and sum(idle_waits) == 90 * 60 - 2
    assert h.sleeps[1 + p.idle_ticks :] == [2.0, 2.0]  # back on the pick cadence
    # the doc was re-read on every idle tick (so a moved start time is noticed)
    assert src.doc_reads >= p.idle_ticks + 1
    assert p.last_ok_at is not None and p.failures_in_a_row == 0


def test_status_flip_to_drafting_ends_idle_early() -> None:
    far = T0 + 120 * MIN
    src = ScriptedSource([_doc(start=far), _doc(start=far), _doc(start=far), _doc("drafting", far)])
    h = Harness(src)
    h.stop_when = lambda: src.pick_polls >= 2
    h.run()
    # read 1 = poll 1; idle ticks read 2, 3 (pre_draft) and 4 (drafting) → polling resumes
    assert h.poller.idle_ticks == 3
    assert h.now < far - 30 * MIN  # well before the window opened


def test_a_moved_start_time_is_picked_up_while_idle() -> None:
    src = ScriptedSource([_doc(start=T0 + 120 * MIN), _doc(start=T0 + 20 * MIN)])
    h = Harness(src)
    h.stop_when = lambda: src.pick_polls >= 2
    h.run()
    assert h.poller.idle_ticks == 1  # one tick, then the new start is inside the window


def test_a_doc_without_start_time_never_idles() -> None:
    src = ScriptedSource([_doc()])
    h = Harness(src)
    h.run(max_polls=3)
    assert src.pick_polls == 3 and h.poller.idle_ticks == 0 and h.poller.mode == "polling"


def test_idle_is_off_unless_a_window_is_configured() -> None:
    src = ScriptedSource([_doc(start=T0 + 120 * MIN)])
    h = Harness(src, idle_before_min=0)
    h.run(max_polls=3)
    assert src.pick_polls == 3 and h.poller.idle_ticks == 0


def test_idle_wait_never_overshoots_the_window() -> None:
    src = ScriptedSource([_doc(start=T0 + 30 * MIN + timedelta(seconds=45))])  # 45 s of idle
    h = Harness(src)
    h.stop_when = lambda: src.pick_polls >= 2
    h.run()
    assert h.poller.idle_ticks == 1 and h.sleeps[:2] == [2.0, 43.0]  # 45 s minus poll 1's wait


def test_idle_doc_read_failure_is_reported_and_retried() -> None:
    src = ScriptedSource([_doc(start=T0 + 120 * MIN)])
    h = Harness(src)
    calls = {"n": 0}
    good = src.draft

    def flaky() -> bytes | None:
        calls["n"] += 1
        if calls["n"] == 3:  # the second idle read blows up
            raise RuntimeError("sleeper 503")
        return good()

    src.draft = flaky  # type: ignore[method-assign]
    h.stop_when = lambda: h.poller.idle_ticks >= 3
    h.run()
    assert h.poller.idle_ticks == 3 and h.poller.last_error is None  # tick 3 healed it


def test_stop_interrupts_an_idle_wait_immediately() -> None:
    """Production waits on the stop event, so `/stop` must not sit out a 60 s idle sleep."""
    src = ScriptedSource([_doc(start=datetime.now(UTC) + 120 * MIN)])
    stop = threading.Event()
    p = DraftPoller(
        src, MemorySink("d1"), "d1", idle_before_start_s=1800.0, idle_poll_s=60.0, interval_s=0.05
    )
    out: list[Any] = []
    t = threading.Thread(target=lambda: out.append(p.run(stop=stop)), daemon=True)
    t0 = time.monotonic()
    t.start()
    time.sleep(0.3)
    assert p.mode == "idle"
    stop.set()
    t.join(5)
    assert out and out[0].stopped and time.monotonic() - t0 < 3


def test_page_has_a_stop_button_wired_to_the_stop_route() -> None:
    html = draft_page("d1", season=2026)
    assert 'id="stop"' in html and "/draft/${DID}/stop" in html
    assert "idle until" in html  # the status line names the idle mode
