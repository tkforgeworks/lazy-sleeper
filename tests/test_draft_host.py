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
        return DraftHost(self.engine, self.poller, clock=lambda: 1.0)


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
    host.stop_all(timeout=0.1)
    gate.set()
    c.runner.join(5)


# --- API ----------------------------------------------------------------------------------------


@pytest.fixture
def client(fx: ReplayFixture) -> TestClient:
    app = create_app(
        Settings(database_url="postgresql+psycopg://x:y@127.0.0.1:1/none", db_connect_timeout_s=1),
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
    r = client.post(f"/draft/{did}/start", json={"season": 2026, "interval_s": 1.5})
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
    assert st["poller"]["interval_s"] == 1.5  # the start body's cadence reached the poller
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


# --- HTML fallback (LS-37) ---------------------------------------------------------------------


def test_draft_page_is_self_contained_and_targets_the_state_endpoint(fx: ReplayFixture) -> None:
    from lazy_sleeper.draft.render import POSITIONS, draft_page

    html = draft_page('x"<id>', season=2026, limit=25, refresh_s=3)
    assert (
        html.startswith("<!doctype html>")
        and "<script src" not in html
        and "http" not in html.split("<style>")[0]
    )
    assert 'const DID="x\\"<id>"' in html  # json-encoded draft id inside the script
    assert (
        "LIMIT=25,EVERY=3000" in html
        and "/draft/${DID}/state" in html
        and "/draft/${DID}/start" in html
    )
    assert "x&quot;&lt;id&gt;" in html  # escaped in the title/header
    assert all(f'data-pos="{p}"' in html for p in POSITIONS) and 'data-pos="ALL"' in html
    assert "name='viewport'" in html and "/board.html?season=2026" in html
    # LS-63: health is ungated, the table gate only moves forward and resets per runner
    assert "drawClock(st);drawHealth(st);" in html and "st.recompute.seq>lastSeq" in html
    assert "if(run!==lastRun){lastRun=run;lastSeq=-1;}" in html and "if(inflight)return" in html
    assert "runner_error" in html and "failures_in_a_row" in html and "persist" in html
    # LS-67: scroll wrapper, sticky thead, wake-up tick, thumb-sized buttons
    assert '<div class="wrap">' in html and "thead th{position:sticky;top:0" in html
    assert "visibilitychange" in html and "min-height:36px" in html


def test_api_serves_the_fallback_page_for_any_draft_and_the_configured_one(
    client: TestClient, fx: ReplayFixture
) -> None:
    r = client.get(f"/draft/{fx.draft_id}/state.html", params={"limit": 12, "refresh": 2})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert f'const DID="{fx.draft_id}"' in r.text and "LIMIT=12,EVERY=2000" in r.text
    assert "interval_s:2.0" in r.text  # default poll cadence the start button will request
    r = client.get("/draft.html")
    assert r.status_code == 200 and 'const DID="1392685476523024384"' in r.text  # Settings default
    assert (
        client.get(f"/draft/{fx.draft_id}/state.html", params={"refresh": 0.1}).status_code == 422
    )


# --- LS-36: second replay fixture (2026-08-23 mock) + fixture builder ------------------------------

FIXTURE_2 = Path(__file__).parent / "fixtures" / "mock_draft_1397325850717749248.json.gz"


def test_second_fixture_is_the_recorded_0823_mock() -> None:
    from lazy_sleeper.draft.fixture import FixtureBuild

    fx2 = ReplayFixture.load(FIXTURE_2)
    assert fx2.draft_id == "1397325850717749248" and len(fx2.picks) == 180
    assert fx2.draft["status"] == "complete" and fx2.draft["draft_order"] == {ME: 8}
    counts = [p["count"] for p in fx2.polls]
    assert len(counts) == 118 and counts[0] == 7 and counts[-1] == 180
    assert counts == sorted(counts)  # every poll was a prefix of the next
    assert set(fx2.picks[0]["metadata"]) <= {
        "first_name",
        "last_name",
        "position",
        "team",
        "player_id",
    }
    # round-trips through the builder's writer unchanged
    b = FixtureBuild(fx2.draft, fx2.polls, fx2.picks)
    assert b.as_dict() == {"draft": fx2.draft, "polls": fx2.polls, "picks": fx2.picks}


def test_fixture_builder_prefix_rule_and_trim() -> None:
    from lazy_sleeper.draft.fixture import _is_prefix, _trim_pick

    a = [{"pick_no": 1, "player_id": "x"}, {"pick_no": 2, "player_id": "y"}]
    assert _is_prefix(a[:1], a) and _is_prefix(a, a) and not _is_prefix(a, a[:1])
    assert not _is_prefix([{"pick_no": 1, "player_id": "z"}], a)  # undo + different pick
    t = _trim_pick({"pick_no": 3, "player_id": "p", "reactions": None, "metadata": {"first_name": "A",
                    "injury_status": "Q", "position": "RB"}})  # fmt: skip
    assert t["metadata"] == {"first_name": "A", "position": "RB"} and "reactions" not in t


def test_second_fixture_replays_through_the_runner() -> None:
    fx2 = ReplayFixture.load(FIXTURE_2)
    from tests.test_draft_engine import _runner

    runner, seen = _runner(fx2)
    summary = runner.run()
    assert summary.complete and summary.events == 180 and summary.failures == 0
    st = runner.engine.state
    assert st.my_slot == 8 and st.complete and len(st.my_roster().picks) == 15
    turns = [a for a in seen if a.my_turn]
    assert len(turns) >= 14  # advice was published for (nearly) every one of my 15 turns
    assert runner.engine.timing.max_s < 1.0


# --- tuning: set_config + page + routes -------------------------------------------------------


def test_set_config_changes_signal_dials_and_recomputes(fx: ReplayFixture) -> None:
    from dataclasses import replace

    from lazy_sleeper.board.tiers import TierConfig

    eng = _engine(fx, picks=20)
    before = eng.latest
    adv = eng.set_config(replace(TierConfig(), need_bonus=0.0, run_threshold=1))
    assert adv.seq == before.seq + 1 and eng.board.cfg.need_bonus == 0.0
    # no need bonus → pick_score == vorp − option value, i.e. never above vorp
    assert all(r.pick_score <= r.value.vorp + 1e-9 for r in adv.rows)
    assert any(r.run for r in adv.rows)  # threshold 1 → the last pick's position is "a run"


def test_tuning_page_lists_every_dial(client: TestClient, fx: ReplayFixture) -> None:
    from lazy_sleeper.board.config import FIELDS
    from lazy_sleeper.draft.render import DIALS

    assert {d[0] for d in DIALS} == set(FIELDS)  # the page and the repo agree on the dials
    r = client.get("/board/config.html", params={"draft_id": fx.draft_id})
    assert r.status_code == 200 and all(f'name="{f}"' in r.text for f in FIELDS)
    assert f'const DID="{fx.draft_id}"' in r.text and "/board/config" in r.text
    assert "<script src" not in r.text
    r = client.get("/board/config.html")
    assert 'const DID="1392685476523024384"' in r.text


def test_apply_config_404_until_running_then_restart_rebuilds(
    client: TestClient, fx: ReplayFixture
) -> None:
    did = fx.draft_id
    assert client.post(f"/draft/{did}/config").status_code == 404
    host = client.app.state.draft_host
    host.start(did, 2026).runner.join(30)
    first = host.get(did)
    r = client.post(f"/draft/{did}/config", params={"restart": "true"})
    assert r.status_code == 200, r.text
    assert r.json()["restarted"] is True
    host.get(did).runner.join(30)
    assert host.get(did) is not first and host.get(did).engine.state.picks_made == 180


# --- API contract export ---------------------------------------------------------------------------


def test_committed_api_docs_match_the_app(client: TestClient) -> None:
    """docs/api/{openapi.json,README.md} are the hand-off contract — regenerate with
    `lazy api export` whenever a route or model changes."""
    import json

    from lazy_sleeper.api.export import openapi_dict, to_markdown

    docs = Path(__file__).parent.parent / "docs" / "api"
    spec = openapi_dict(client.app)
    assert json.loads((docs / "openapi.json").read_text(encoding="utf-8")) == spec
    md = to_markdown(spec)
    assert (docs / "README.md").read_text(encoding="utf-8") == md
    for path in spec["paths"]:
        assert f"`{path}`" in md


def test_injury_status_flows_from_board_context_to_rows_and_page(fx: ReplayFixture) -> None:
    """A player's injury_status (core.players via load_board_context) reaches /state rows; the
    fallback page renders it as an .inj tag next to the name."""
    from dataclasses import replace as dc_replace

    from lazy_sleeper.draft.render import draft_page

    eng = _engine(fx, picks=0)
    hurt = fx.picks[0]["player_id"]
    eng.board = dc_replace(eng.board, injuries={hurt: "Questionable"})
    eng.recompute()
    rows = state_payload(eng, fx.draft_id)["rows"]
    by_id = {r["sleeper_id"]: r for r in rows}
    assert by_id[hurt]["injury_status"] == "Questionable"
    healthy = fx.picks[1]["player_id"]
    assert by_id[healthy]["injury_status"] is None
    html = draft_page("x", season=2026)
    assert "x.injury_status" in html and 'class="inj"' in html


def test_state_reports_poller_health_and_the_writer(fx: ReplayFixture) -> None:
    host = _Replay(fx).host()
    run = host.start(fx.draft_id, 2026)
    run.runner.join(30)
    p = host.state(fx.draft_id)["poller"]
    assert p["failures_in_a_row"] == 0 and p["last_error"] is None and p["degraded"] is False
    assert p["last_ok_at"] is not None and p["last_poll_at"] is not None
    w = p["persist"]
    assert w["pending"] == 0 and w["applied"] >= 1 and w["failures"] == 0
    assert w["dropped"] == 0 and w["last_error"] is None


def test_state_exposes_a_dead_runner(fx: ReplayFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """LS-64: Running.error is the runner's error, and /state carries it."""
    host = _Replay(fx).host()
    run = host.start(fx.draft_id, 2026)
    run.runner.join(30)
    assert run.error is None and host.state(fx.draft_id)["poller"]["runner_error"] is None
    monkeypatch.setattr(run.runner, "error", "RuntimeError: poller bug")
    st = host.state(fx.draft_id)
    assert st["poller"]["runner_error"] == "RuntimeError: poller bug" and st["running"] is False
    assert run.error == "RuntimeError: poller bug"


# --- LS-69: a wedged database must not hang the API or serialize starts --------------------------


def test_db_unreachable_is_a_503_not_a_hang(client: TestClient) -> None:
    """The client fixture points at a refused port: the DB-touching routes answer 503 with a
    clear detail (the handler is what turns the timeout into a response); non-DB routes are
    unaffected."""
    for path in ("/board?limit=1", "/board/config"):
        r = client.get(path)
        assert r.status_code == 503, path
        assert r.json()["detail"].startswith("database unavailable: ")
    assert client.get("/draft").status_code == 200


def test_start_reports_db_failure_as_503(fx: ReplayFixture) -> None:
    from sqlalchemy.exc import OperationalError

    def broken_engine(draft_id: str, season: int) -> DraftEngine:
        raise OperationalError("select 1", None, ConnectionRefusedError("connection refused"))

    rp = _Replay(fx)
    host = DraftHost(broken_engine, rp.poller)
    app = create_app(
        Settings(database_url="postgresql+psycopg://x:y@127.0.0.1:1/none"), draft_host=host
    )
    r = TestClient(app).post(f"/draft/{fx.draft_id}/start", json={"season": 2026})
    assert r.status_code == 503 and "connection refused" in r.json()["detail"]
    assert host.get(fx.draft_id) is None


def test_slow_start_for_one_draft_does_not_block_another(fx: ReplayFixture) -> None:
    rp = _Replay(fx, gate=threading.Event())
    release = threading.Event()
    entered = threading.Event()

    def engine(draft_id: str, season: int) -> DraftEngine:
        if draft_id == "slow":
            entered.set()
            assert release.wait(10)
        return rp.engine(draft_id, season)

    host = DraftHost(engine, rp.poller)
    t = threading.Thread(target=host.start, args=("slow", 2026), daemon=True)
    t.start()
    assert entered.wait(5)
    fast = host.start(fx.draft_id, 2026)  # returns while "slow" is still inside make_engine
    assert fast.runner.running and t.is_alive() and host.get("slow") is None
    release.set()
    t.join(10)
    assert host.get("slow") is not None and host.ids() == [fx.draft_id, "slow"]
    host.stop_all(timeout=0.1)
    rp.gate.set()


# --- LS-70: a missing draft stops its runner; shutdown stops every runner ------------------------


def _forever_host(fx: ReplayFixture) -> tuple[_Replay, DraftHost]:
    """A runner that keeps polling (replay exhausted → retry loop) until it is told to stop."""
    import time

    rp = _Replay(fx)

    def poller(draft_id: str) -> DraftPoller:
        return DraftPoller(ReplaySource(fx), rp.sink, draft_id, sleep=lambda _s: time.sleep(0.02))

    return rp, DraftHost(rp.engine, poller)


def test_missing_draft_stops_the_runner_and_the_api_reports_it(fx: ReplayFixture) -> None:
    from lazy_sleeper.draft.poller import DraftNotFound

    class Missing:
        def picks(self) -> None:
            raise DraftNotFound("draft 1234 not found on Sleeper (404)")

        def draft(self) -> None:
            return None

    rp = _Replay(fx)
    host = DraftHost(
        rp.engine, lambda did: DraftPoller(Missing(), rp.sink, did, sleep=lambda _s: None)
    )
    app = create_app(
        Settings(database_url="postgresql+psycopg://x:y@127.0.0.1:1/none"), draft_host=host
    )
    c = TestClient(app)
    assert c.post("/draft/1234/start", json={"season": 2026}).status_code == 200
    host.get("1234").runner.join(10)
    assert c.get("/draft").json() == [{"draft_id": "1234", "running": False, "season": 2026}]
    st = c.get("/draft/1234/state").json()
    assert st["running"] is False
    assert st["poller"]["runner_error"] == "DraftNotFound: draft 1234 not found on Sleeper (404)"
    assert st["poller"]["summary"]["fatal"] == st["poller"]["runner_error"]
    assert st["poller"]["failures_in_a_row"] == 2 and st["poller"]["summary"]["polls"] == 0


def test_app_shutdown_stops_live_runners(fx: ReplayFixture) -> None:
    import time

    _rp, host = _forever_host(fx)
    app = create_app(
        Settings(database_url="postgresql+psycopg://x:y@127.0.0.1:1/none"), draft_host=host
    )
    with TestClient(app) as c:  # runs the lifespan: shutdown fires on exit
        r = c.post(f"/draft/{fx.draft_id}/start", json={"season": 2026, "forever": True})
        assert r.json()["running"] is True
        run = host.get(fx.draft_id)
        time.sleep(0.2)
        assert run.runner.running
        t0 = time.monotonic()
    assert time.monotonic() - t0 < 5
    assert run.runner.stop.is_set() and not run.runner.running
    assert run.runner.summary is not None and run.runner.summary.stopped


# --- LS-56: pick clock, on-the-clock team, recent picks in the payload -----------------------


def test_state_payload_has_clock_team_name_and_recent_picks(fx: ReplayFixture) -> None:
    from datetime import UTC, datetime, timedelta

    from lazy_sleeper.draft.poller import PickEvent

    doc = {**_doc(fx), "pick_timer": 120, "draft_order": {ME: 8, "111": 7}}
    names = {ME: "Lazy Sleepers", "111": "Rivals"}
    eng = DraftEngine(_board(fx), RULES, draft_doc=doc, user_id=ME, team_names=names)
    spec = eng.state.spec
    t0 = datetime(2026, 9, 4, 20, 0, tzinfo=UTC)
    for p in fx.picks[:7]:
        n = p["pick_no"]
        eng.on_pick(PickEvent("d", n, spec.round_of(n), p["draft_slot"], p["player_id"], None,
                              t0 + timedelta(seconds=n), n, 1,
                              {"position": p["metadata"]["position"]}))  # fmt: skip
    out = state_payload(eng, fx.draft_id)
    clock = out["clock"]
    assert clock["on_the_clock"] == 8 and clock["on_the_clock_team_name"] == "Lazy Sleepers"
    assert clock["pick_timer_s"] == 120
    assert clock["pick_deadline"] == t0 + timedelta(seconds=7 + 120)
    feed = out["recent_picks"]
    assert [p["pick_no"] for p in feed] == [7, 6, 5, 4, 3, 2, 1]
    assert feed[0]["slot"] == 7 and feed[0]["team_name"] == "Rivals"
    assert feed[1]["team_name"] is None  # slot 6 has no draft_order entry
    assert feed[0]["sleeper_id"] == fx.picks[6]["player_id"]
    assert feed[0]["position"] == fx.picks[6]["metadata"]["position"]
    assert set(feed[0]) == {"pick_no", "slot", "team_name", "sleeper_id", "name", "position"}
    eng.on_pick(_ev(fx.picks[7], spec))
    out = state_payload(eng, fx.draft_id)
    feed = out["recent_picks"]
    assert len(feed) == 8 and feed[0]["pick_no"] == 8 and feed[-1]["pick_no"] == 1
    assert out["clock"]["on_the_clock_team_name"] is None  # slot 9: no user row


def test_api_state_types_the_clock_and_feed(client: TestClient, fx: ReplayFixture) -> None:
    assert client.post(f"/draft/{fx.draft_id}/start", json={"season": 2026}).status_code == 200
    # the fixture's pick_timer arrives with the runner's first draft-doc read: wait for the
    # replay to finish so the assertion doesn't race the thread (it did on CI)
    client.app.state.draft_host.get(fx.draft_id).runner.join(30)
    r = client.get(f"/draft/{fx.draft_id}/state?limit=1")
    assert r.status_code == 200
    body = r.json()
    for key in ("on_the_clock_team_name", "pick_timer_s", "pick_deadline"):
        assert key in body["clock"]
    assert body["clock"]["pick_timer_s"] == 120  # the fixture's Sleeper doc
    assert isinstance(body["recent_picks"], list) and len(body["recent_picks"]) <= 8
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    assert "RecentPickOut" in schema
    assert "pick_deadline" in schema["DraftClockOut"]["properties"]
    assert schema["DraftStateOut"]["properties"]["recent_picks"]["items"]["$ref"].endswith(
        "RecentPickOut"
    )


def test_fallback_page_ticks_the_countdown_and_shows_the_feed(fx: ReplayFixture) -> None:
    from lazy_sleeper.draft.render import draft_page

    html = draft_page(fx.draft_id, season=2026)
    assert 'id="feed"' in html and 'id="cd"' in html
    assert "pick_deadline" in html and "on_the_clock_team_name" in html and "recent_picks" in html
    assert "setInterval(drawCountdown,1000)" in html
