from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sleeper_proj_payload() -> bytes:
    return (FIXTURES / "sleeper_proj_sample.json").read_bytes()


@pytest.fixture
def espn_kona_payload() -> bytes:
    return (FIXTURES / "espn_kona_sample.json").read_bytes()


@pytest.fixture
def sleeper_players_payload() -> bytes:
    return (FIXTURES / "sleeper_players_sample.json").read_bytes()


@pytest.fixture
def sleeper_league_payload() -> dict:
    import json

    return json.loads((FIXTURES / "sleeper_league_sample.json").read_text())
