"""Bye weeks (LS-57): ESPN pro-team doc → rows → the `bye` on board and draft rows. DB-free."""

from __future__ import annotations

from pathlib import Path

import pytest

from lazy_sleeper.ingest.byes import bye_of, parse_pro_teams
from lazy_sleeper.ingest.validate import validate_espn_pro_teams

FIXTURE = Path(__file__).parent / "fixtures" / "espn_pro_teams_2026.json"

# Known 2026 byes, read off the live doc 2026-08-28 (ESPN abbreviations differ for WSH → WAS).
KNOWN = {"KC": 5, "DET": 6, "CIN": 6, "WAS": 7, "LAC": 7, "SF": 8, "LAR": 11, "ARI": 14, "DAL": 14}


def test_parse_maps_every_real_team_to_its_sleeper_abbreviation_and_bye() -> None:
    rows = parse_pro_teams(FIXTURE.read_bytes())
    assert len(rows) == 32  # the FA pseudo-team (id 0, byeWeek 0) is dropped
    by_team = {r["team"]: r for r in rows}
    for team, week in KNOWN.items():
        assert by_team[team]["bye_week"] == week, team
    assert by_team["WAS"]["espn_abbrev"] == "WSH" and by_team["WAS"]["espn_id"] == 28
    assert all(1 <= r["bye_week"] <= 18 for r in rows)
    assert set(rows[0]) == {"team", "espn_id", "espn_abbrev", "bye_week"}


def test_parse_rejects_payloads_without_pro_teams() -> None:
    with pytest.raises(ValueError):
        parse_pro_teams(b'{"settings": {}}')
    with pytest.raises(ValueError):
        parse_pro_teams(b"[]")
    assert parse_pro_teams(b'{"settings": {"proTeams": [{"id": 99, "byeWeek": 4}]}}') == []


def test_validator_wants_32_teams_with_a_bye() -> None:
    assert validate_espn_pro_teams(FIXTURE.read_bytes()).valid
    assert not validate_espn_pro_teams(b'{"settings": {"proTeams": []}}').valid
    assert not validate_espn_pro_teams(b"not json").valid


def test_row_lookup_is_null_for_free_agents_and_unknown_teams() -> None:
    byes = {r["team"]: r["bye_week"] for r in parse_pro_teams(FIXTURE.read_bytes())}
    assert bye_of(byes, "DET") == 6
    assert bye_of(byes, None) is None and bye_of(byes, "FA") is None
    assert bye_of({}, "DET") is None and bye_of(None, "DET") is None
