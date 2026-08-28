"""Client URL/param construction, verified against a mock transport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from lazy_sleeper.ingest.espn import EspnClient
from lazy_sleeper.ingest.http import HttpClient
from lazy_sleeper.ingest.sleeper import SleeperClient


def _capture() -> tuple[list[httpx.Request], httpx.MockTransport]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"[]")

    return seen, httpx.MockTransport(handler)


def test_sleeper_projection_urls() -> None:
    seen, transport = _capture()
    c = SleeperClient(HttpClient(delay_ms=0, transport=transport))
    c.projections(2026)
    c.projections(2025, 7)
    c.stats(2025, 1)
    assert seen[0].url.path == "/projections/nfl/2026"
    assert seen[1].url.path == "/projections/nfl/2025/7"
    assert seen[2].url.path == "/stats/nfl/2025/1"
    params = seen[0].url.params
    assert params["season_type"] == "regular"
    assert params.get_list("position[]") == ["QB", "RB", "WR", "TE", "K", "DEF"]


def test_sleeper_documented_urls() -> None:
    seen, transport = _capture()
    c = SleeperClient(HttpClient(delay_ms=0, transport=transport))
    c.draft_picks("123")
    c.league_rosters("456")
    c.players()
    assert str(seen[0].url) == "https://api.sleeper.app/v1/draft/123/picks"
    assert str(seen[1].url) == "https://api.sleeper.app/v1/league/456/rosters"
    assert str(seen[2].url) == "https://api.sleeper.app/v1/players/nfl"


def test_espn_kona_filter_header() -> None:
    seen, transport = _capture()
    c = EspnClient(HttpClient(delay_ms=0, transport=transport), limit=1500)
    c.kona(2026)
    req = seen[0]
    assert "seasons/2026/segments/0/leaguedefaults/3" in str(req.url)
    assert req.url.params["view"] == "kona_player_info"
    flt = json.loads(req.headers["X-Fantasy-Filter"])
    assert flt["players"]["limit"] == 1500


def test_http_retries_then_succeeds(monkeypatch) -> None:  # noqa: ANN001
    import lazy_sleeper.ingest.http as http_mod

    monkeypatch.setattr(http_mod.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, content=b"ok")

    http = HttpClient(delay_ms=0, retries=3, transport=httpx.MockTransport(handler))
    assert http.get_bytes("https://x.test/y") == b"ok"
    assert calls["n"] == 3


def test_http_with_zero_retries_fails_fast_and_never_sleeps(monkeypatch) -> None:  # noqa: ANN001
    """The draft poll's client (LS-65): one attempt, no courtesy pause, the error is the caller's
    (the poller's capped backoff owns retrying)."""
    import lazy_sleeper.ingest.http as http_mod

    slept: list[float] = []
    monkeypatch.setattr(http_mod.time, "sleep", slept.append)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("dead network")

    http = HttpClient(timeout_s=5.0, retries=0, delay_ms=0, transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.ConnectTimeout):
        http.get_bytes("https://x.test/y")
    assert calls["n"] == 1 and slept == []


def test_draft_http_settings_are_separate_from_the_daily_pull() -> None:
    from lazy_sleeper.config import Settings

    s = Settings(_env_file=None)
    assert (s.http_timeout_s, s.http_retries) == (60.0, 3)
    assert (s.draft_http_timeout_s, s.draft_max_backoff_s) == (5.0, 15.0)
