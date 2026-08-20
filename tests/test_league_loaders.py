"""Sleeper league-state payloads → core row dicts (LS-16). DB-free: parse layer only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lazy_sleeper.ingest.league_loaders import (
    parse_draft,
    parse_picks,
    parse_rosters,
    parse_users,
)

FIXTURES = Path(__file__).parent / "fixtures"
DRAFT_ID = "1392685476523024384"


def _fx(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_parse_draft_lifts_timer_rounds_and_slot_order() -> None:
    row = parse_draft(_fx("sleeper_draft_sample.json"))
    assert row["draft_id"] == DRAFT_ID and row["league_id"] == "1392685475625443328"
    assert row["season"] == 2026 and row["type"] == "snake" and row["status"] == "pre_draft"
    assert (row["rounds"], row["teams"], row["pick_timer"]) == (15, 12, 120)
    assert row["slot_to_roster_id"]["7"] == 7 and len(row["slot_to_roster_id"]) == 12
    assert row["draft_order"] is None  # Sleeper sends null until the order is set
    assert row["metadata_"]["scoring_type"] == "ppr"
    assert row["start_time"] == 1788566459000


def test_parse_draft_rejects_non_draft_payloads() -> None:
    with pytest.raises(ValueError):
        parse_draft(b"[]")
    with pytest.raises(ValueError):
        parse_draft(b'{"league_id": "x"}')


def test_parse_picks_orders_by_pick_no_and_maps_player_and_autopick() -> None:
    rows = parse_picks(_fx("sleeper_picks_sample.json"), DRAFT_ID)
    assert [r["pick_no"] for r in rows] == [1, 2, 3]
    assert rows[0]["sleeper_id"] == "9221" and rows[0]["roster_id"] == 1
    assert rows[0]["picked_by"] == "1264388068945694720" and rows[0]["round"] == 1
    assert rows[2]["picked_by"] is None  # "" = autopick/CPU
    assert rows[0]["metadata_"]["last_name"] == "Gibbs"
    assert all(r["draft_id"] == DRAFT_ID for r in rows)


def test_parse_picks_drops_other_drafts_and_handles_empty() -> None:
    picks = json.loads(_fx("sleeper_picks_sample.json"))
    picks[1]["draft_id"] = "999"
    rows = parse_picks(json.dumps(picks).encode(), DRAFT_ID)
    assert [r["pick_no"] for r in rows] == [1, 3]
    assert parse_picks(b"[]", DRAFT_ID) == []  # pre-draft: the real 2026 payload today
    with pytest.raises(ValueError):
        parse_picks(b"{}", DRAFT_ID)


def test_parse_rosters_keeps_lists_and_settings() -> None:
    rows = parse_rosters(_fx("sleeper_rosters_sample.json"))
    assert len(rows) == 2
    r = rows[0]
    assert (r["league_id"], r["roster_id"]) == ("1392685475625443328", 1)
    assert r["owner_id"] == "1264388068945694720"
    assert r["players"] == [] and r["starters"] == ["0"] * 10
    assert r["settings"]["waiver_position"] == 12


def test_parse_users_reads_team_name_from_metadata() -> None:
    users = json.loads(_fx("sleeper_users_sample.json"))
    users[0]["metadata"]["team_name"] = "Gibbs Me A Break"
    users[1]["is_owner"] = True
    rows = parse_users(json.dumps(users).encode())
    assert rows[0]["display_name"] == "dougkr" and rows[0]["team_name"] == "Gibbs Me A Break"
    assert rows[0]["is_owner"] is None and rows[1]["is_owner"] is True
    assert rows[0]["league_id"] == "1392685475625443328"
