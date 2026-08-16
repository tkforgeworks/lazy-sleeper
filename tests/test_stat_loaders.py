from __future__ import annotations

import json
from datetime import UTC, datetime

from lazy_sleeper.db.models import Snapshot
from lazy_sleeper.ingest.espn_stats import POSITIONS, TEAMS, decode_stats
from lazy_sleeper.ingest.stat_loaders import SleeperIdResolver, espn_stat_rows, sleeper_stat_rows


def _snap(source: str, kind: str, season: int | None, week: int | None, sid: int = 7) -> Snapshot:
    return Snapshot(
        id=sid,
        source=source,
        kind=kind,
        season=season,
        week=week,
        pulled_at=datetime(2026, 8, 16, tzinfo=UTC),
        sha256="x" * 64,
        byte_size=1,
        storage_path="p",
    )


# --- ESPN decoder ---------------------------------------------------------------------------
def test_decode_stats_maps_verified_ids_and_drops_unknown_and_zero() -> None:
    raw = {"3": 3587.0, "4": 22.0, "20": 11.0, "24": 422.0, "72": 0.0, "999": 5.0, "210": 17}
    out = decode_stats(raw)
    assert out == {
        "pass_yd": 3587.0,
        "pass_td": 22.0,
        "pass_int": 11.0,
        "rush_yd": 422.0,
        "gp": 17.0,
    }


def test_decode_kicker_and_dst_keys() -> None:
    out = decode_stats({"83": 36, "84": 42, "74": 11, "80": 15, "99": 30, "89": 1, "121": 1})
    assert out["fgm"] == 36 and out["fga"] == 42
    assert out["fgm_50p"] == 11 and out["fgm_0_39"] == 15  # ESPN-native bucket kept
    assert out["sack"] == 30 and out["pts_allow_0"] == 1 and out["pts_allow_18_21"] == 1


def test_position_and_team_tables() -> None:
    assert POSITIONS[1] == "QB" and POSITIONS[16] == "DEF"
    assert TEAMS[33] == "BAL" and TEAMS[28] == "WAS" and len(TEAMS) == 32


# --- Sleeper transform ----------------------------------------------------------------------
def test_sleeper_weekly_rows(sleeper_proj_payload: bytes) -> None:
    snap = _snap("sleeper", "projections_week", 2025, 1)
    stat_rows, adp_rows = sleeper_stat_rows(sleeper_proj_payload, snap)
    payload_rows = json.loads(sleeper_proj_payload)
    non_empty = [e for e in payload_rows if e.get("stats")]
    assert len(stat_rows) == len(non_empty)  # empty-stat rows are dropped
    r = stat_rows[0]
    assert r["source"] == "sleeper" and r["category"] == "proj"
    assert r["season"] == 2025 and r["week"] == 1
    assert r["sleeper_id"] == r["source_player_id"]
    assert r["snapshot_id"] == 7
    # adp_*/pts_*/gp are stripped out of stats
    assert not any(k.startswith(("adp_", "pts_")) or k == "gp" for k in r["stats"])
    assert adp_rows == []  # weekly files carry no season ADP


def test_sleeper_season_rows_emit_adp() -> None:
    entries = [
        {
            "player_id": "4046",
            "season": "2026",
            "week": None,
            "team": "KC",
            "player": {"position": "QB"},
            "stats": {
                "pass_yd": 4300.5,
                "pass_td": 30,
                "adp_ppr": 45.2,
                "pts_ppr": 320.1,
                "gp": 17,
            },
        },
        {
            "player_id": "KC",
            "season": "2026",
            "week": None,
            "team": "KC",
            "player": {"position": "DEF"},
            "stats": {"sack": 40, "pts_allow_0": 1},
        },
        {
            "player_id": "9999",
            "season": "2026",
            "week": None,
            "team": None,
            "player": {"position": "WR"},
            "stats": {"adp_ppr": 250.0, "gp": 0},  # unprojected: ADP only, no stat line
        },
    ]
    snap = _snap("sleeper", "projections_season", 2026, None)
    stat_rows, adp_rows = sleeper_stat_rows(json.dumps(entries).encode(), snap)
    assert stat_rows[0]["stats"] == {"pass_yd": 4300.5, "pass_td": 30.0}
    assert stat_rows[0]["gp"] == 17.0 and stat_rows[0]["provider_points"] == 320.1
    assert stat_rows[0]["week"] is None
    assert stat_rows[1]["position"] == "DEF" and stat_rows[1]["sleeper_id"] == "KC"
    assert len(stat_rows) == 2  # the unprojected player produced no stat line
    assert [a["sleeper_id"] for a in adp_rows] == ["4046", "9999"]
    assert adp_rows[0]["adp_ppr"] == 45.2 and adp_rows[1]["adp_ppr"] == 250.0


# --- ESPN transform -------------------------------------------------------------------------
def test_espn_rows_from_fixture(espn_kona_payload: bytes) -> None:
    data = json.loads(espn_kona_payload)
    first = data["players"][0]["player"]
    resolver = SleeperIdResolver({str(first["id"]): "SLEEP1"})
    snap = _snap("espn", "kona", 2025, None, sid=9)
    rows = espn_stat_rows(espn_kona_payload, snap, resolver)
    assert rows, "fixture should yield stat lines"
    mine = [r for r in rows if r["source_player_id"] == str(first["id"])]
    assert mine and all(r["sleeper_id"] == "SLEEP1" for r in mine)
    cats = {r["category"] for r in mine}
    assert cats <= {"proj", "actual"}
    # every row has decoded (Sleeper-vocab) keys only
    for r in rows:
        assert r["stats"], "empty stat lines must be dropped"
        assert all(not k.isdigit() for k in r["stats"])
        assert r["snapshot_id"] == 9
    # season rows have week None; weekly rows carry scoringPeriodId
    assert any(r["week"] is None for r in rows)


def test_espn_resolver_tracks_unresolved_and_maps_dst() -> None:
    resolver = SleeperIdResolver({"1": "A"})
    assert resolver.resolve("1", "WR", 12) == "A"
    assert resolver.resolve("2", "WR", 12) is None
    assert resolver.unresolved == {"2"}
    assert resolver.resolve("-16033", "DEF", 33) == "BAL"
