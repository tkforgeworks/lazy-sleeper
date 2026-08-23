"""Decision surface (LS-35): state payload, DraftHost, and the API routes — on the replay
fixture, DB-free (the API is built with an injected host; no DATABASE_URL is touched)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lazy_sleeper.api.app import create_app
from lazy_sleeper.config import Settings
from lazy_sleeper.draft.engine import DraftEngine
from lazy_sleeper.draft.host import ROW_FIELDS, DraftHost, state_payload
from lazy_sleeper.draft.poller import DraftPoller, MemorySink, ReplayFixture, ReplaySource
from tests.test_draft_engine import ME, RULES, _board, _doc, _ev, _rows

FIXTURE = Path(__file__).parent / "fixtures" / "mock_draft_1396298350046760960.json.gz"


@pytest.fixture(scope="module")
def fx() -> ReplayFixture:
    return ReplayFixture.load(FIXTURE)


def _engine(fx: ReplayFixture, picks: int = 0) -> DraftEngine:
    eng = DraftEngine(_board(fx), RULES, draft_doc=_doc(fx), user_id=ME)
    spec = eng.state.spec
    for p in fx.picks[:picks]:
        eng.on_pick(_ev(p, spec))
    return eng


# --- payload ------------------------------------------------------------------------------------


def test_state_payload_describes_clock_roster_and_rows(fx: ReplayFixture) -> None:
    eng = _engine(fx, picks=7)  # pick 8 = my first turn (slot 8)
    out = state_payload(eng, fx.draft_id)
    assert out["draft_id"] == fx.draft_id
    assert out["spec"] == {"teams": 12, "rounds": 15, "type": "snake", "total_picks": 180}
    clock = out["clock"]
    assert clock["current_pick"] == 8 and clock["round"] == 1 and clock["on_the_clock"] == 8
    assert clock["my_slot"] == 8 and clock["my_turn"] and clock["picks_until_my_turn"] == 0
    assert clock["my_next_pick"] == 8 and clock["picks_made"] == 7 and not clock["complete"]
    roster = out["my_roster"]
    assert roster["slot"] == 8 and roster["picks"] == [] and roster["open_bench"] == 5
    assert roster["open_starters"]["RB"] == 2 and roster["needs"]["RB"] > roster["needs"]["K"]
    rc = out["recompute"]
    assert rc["seq"] == 7 and rc["pick_no"] == 8 and not rc["stale"] and rc["error"] is None
    assert rc["count"] == 7 and rc["avg_ms"] >= 0 and rc["max_ms"] >= rc["avg_ms"]
    assert out["board"]["rows"] == 180 and out["board"]["available"] == 173
    rows = out["rows"]
    assert len(rows) == 173 and [r["rank"] for r in rows] == list(range(1, 174))
    assert set(rows[0]) == set(ROW_FIELDS)
    taken = {p["player_id"] for p in fx.picks[:7]}
    assert not taken & {r["sleeper_id"] for r in rows}
    scores = [r["pick_score"] for r in rows]
    assert scores == sorted(scores, reverse=True)


def test_state_payload_position_filter_keeps_overall_rank(fx: ReplayFixture) -> None:
    eng = _engine(fx, picks=20)
    full = state_payload(eng, fx.draft_id)["rows"]
    rb = state_payload(eng, fx.draft_id, position="rb", limit=5)["rows"]
    assert len(rb) == 5 and all(r["position"] == "RB" for r in rb)
    assert [r["rank"] for r in rb] == [r["rank"] for r in full if r["position"] == "RB"][:5]


def test_state_payload_reflects_the_latest_recompute_and_error_flag(
    fx: ReplayFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _engine(fx, picks=3)
    spec = eng.state.spec
    before = state_payload(eng, fx.draft_id)
    eng.on_pick(_ev(fx.picks[3], spec))
    after = state_payload(eng, fx.draft_id)
    assert after["recompute"]["seq"] == before["recompute"]["seq"] + 1
    assert after["clock"]["current_pick"] == 5
    assert after["recompute"]["computed_at"] >= before["recompute"]["computed_at"]
    monkeypatch.setattr(
        "lazy_sleeper.draft.engine.advise", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    eng.on_pick(_ev(fx.picks[4], spec))
    bad = state_payload(eng, fx.draft_id)
    assert bad["recompute"]["stale"] and bad["recompute"]["error"] == "RuntimeError: x"
    assert bad["clock"]["current_pick"] == 6  # state moved on; rows are the previous good ones
    assert [r["sleeper_id"] for r in bad["rows"]] == [r["sleeper_id"] for r in after["rows"]]


def test_state_payload_without_my_slot(fx: ReplayFixture) -> None:
    eng = DraftEngine(_board(fx), RULES, draft_doc={"teams": 12, "rounds": 15})
    eng.on_pick(_ev(fx.picks[0], eng.state.spec))
    out = state_payload(eng, fx.draft_id)
    assert out["my_roster"] is None and out["clock"]["my_slot"] is None
    assert not out["clock"]["my_turn"] and out["clock"]["picks_until_my_turn"] is None


# --- host ---------------------------------------------------------------------------------------


class _Replay:
    """Injected factories: replay source + memory sink, gated so the test controls the polls."""

    def __init__(self, fx: ReplayFixture, *, gate: threading.Event | None = None) -> None:
        self.fx = fx
        self.sink = MemorySink(fx.draft_id)
        self.gate = gate
        self.engines = 0

    def engine(self, draft_id: str, season: int) -> DraftEngine:
        self.engines += 1
        eng = DraftEngine(_board(self.fx), RULES, draft_doc=_doc(self.fx), user_id=ME)
        eng.rebuild(self.rows(draft_id))
        return eng

    def poller(self, draft_id: str) -> DraftPoller:
        def sleep(_s: float) -> None:
            if self.gate is not None:
                self.gate.wait(5)
                self.gate.clear()

        return DraftPoller(ReplaySource(self.fx), self.sink, draft_id, sleep=sleep)

    def rows(self, draft_id: str) -> list[dict]:
        return list(self.sink.rows.values())

    def host(self) -> DraftHost:
        return DraftHost(self.engine, self.poller, self.rows, clock=lambda: 1.0)


def test_host_runs_the_replay_to_completion_and_serves_state(fx: ReplayFixture) -> None:
    host = _Replay(fx).host()
    assert host.state(fx.draft_id) is None and host.ids() == []
    run = host.start(fx.draft_id, 2026)
    assert run.started_at == 1.0 and host.ids() == [fx.draft_id]
    run.runner.join(30)
    st = host.state(fx.draft_id)
    assert st is not None and st["running"] is False and st["clock"]["complete"]
    assert st["clock"]["picks_made"] == 180 and st["poller"]["summary"]["events"] == 180
    assert st["poller"]["status"] in ("drafting", "complete")
    assert st["poller"]["expected_picks"] == 180
    assert len(st["my_roster"]["picks"]) == 15 and st["board"]["available"] == 0


def test_host_start_is_idempotent_while_running_and_restarts_after(fx: ReplayFixture) -> None:
    gate = threading.Event()
    rp = _Replay(fx, gate=gate)
    host = rp.host()
    a = host.start(fx.draft_id, 2026)
    b = host.start(fx.draft_id, 2026)
    assert a is b and rp.engines == 1 and a.runner.running
    host.stop(fx.draft_id)  # sets the stop event; the gated sleep wakes on the next gate.set
    gate.set()
    a.runner.join(5)
    assert not a.runner.running and host.state(fx.draft_id)["running"] is False
    c = host.start(fx.draft_id, 2026)
    assert c is not a and rp.engines == 2
    host.stop_all()
    gate.set()
    c.runner.join(5)


# --- API ----------------------------------------------------------------------------------------


@pytest.fixture
def client(fx: ReplayFixture) -> TestClient:
    app = create_app(
        Settings(database_url="postgresql+psycopg://x:y@localhost:1/none"),
        draft_host=_Replay(fx).host(),
    )
    return TestClient(app)


def test_api_state_404_until_started_then_serves_and_filters(
    client: TestClient, fx: ReplayFixture
) -> None:
    did = fx.draft_id
    r = client.get(f"/draft/{did}/state")
    assert r.status_code == 404 and "start" in r.json()["detail"]
    assert client.get("/draft").json() == []
    r = client.post(f"/draft/{did}/start", json={"season": 2026})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["draft_id"] == did and body["my_slot"] == 8 and body["board_rows"] == 180
    assert body["already_running"] is False
    host = client.app.state.draft_host
    host.get(did).runner.join(30)
    r = client.get(f"/draft/{did}/state", params={"position": "WR", "limit": 3})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["clock"]["complete"] and st["running"] is False
    assert len(st["rows"]) == 0  # replay drafted every board player
    assert st["clock"]["picks_made"] == 180 and st["recompute"]["error"] is None
    assert st["recompute"]["seq"] >= 1 and st["recompute"]["count"] >= 1
    assert client.get("/draft").json() == [{"draft_id": did, "running": False, "season": 2026}]
    r = client.post(f"/draft/{did}/stop")
    assert r.status_code == 200 and r.json()["running"] is False


def test_api_state_mid_draft_has_rows_and_is_documented(
    client: TestClient, fx: ReplayFixture
) -> None:
    did = fx.draft_id
    host = client.app.state.draft_host
    run = host.start(did, 2026)
    run.runner.join(30)
    # rewind the engine to a mid-draft position to exercise the row schema through the API
    run.engine.rebuild(_rows(fx.picks[:50]))
    st = client.get(f"/draft/{did}/state", params={"limit": 10}).json()
    assert st["clock"]["current_pick"] == 51 and len(st["rows"]) == 10
    row = st["rows"][0]
    assert set(row) == set(ROW_FIELDS) and row["rank"] == 1 and row["pick_score"] is not None
    assert st["my_roster"]["picks"][0]["seat"] in ("QB", "RB", "WR", "TE", "K", "DEF", "FLEX", "BN")
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert "/draft/{draft_id}/state" in paths and "/draft/{draft_id}/start" in paths
    schemas = spec["components"]["schemas"]
    assert set(schemas["DraftRowOut"]["properties"]) == set(ROW_FIELDS)
    assert {"clock", "my_roster", "recompute", "rows"} <= set(
        schemas["DraftStateOut"]["properties"]
    )
