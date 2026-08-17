from __future__ import annotations

from datetime import UTC, datetime

from lazy_sleeper.db.models import Snapshot
from lazy_sleeper.ingest.nflverse_loaders import (
    expected_points_rows,
    nflverse_actual_rows,
    snap_count_rows,
)


def _snap(kind: str, season: int, sid: int = 5) -> Snapshot:
    return Snapshot(
        id=sid,
        source="nflverse",
        kind=kind,
        season=season,
        week=None,
        pulled_at=datetime(2026, 8, 16, tzinfo=UTC),
        sha256="x" * 64,
        byte_size=1,
        storage_path="p",
    )


def _csv(header: list[str], *rows: list[str]) -> bytes:
    return ("\n".join([",".join(header)] + [",".join(r) for r in rows]) + "\n").encode()


STATS_HEADER = [
    "player_id", "player_display_name", "position", "season", "week", "season_type", "team",
    "attempts", "completions", "passing_yards", "passing_tds", "passing_interceptions",
    "carries", "rushing_yards", "rushing_tds", "receptions", "targets", "receiving_yards",
    "receiving_tds", "fumbles_total", "fumbles_lost_total", "fg_made", "fg_att", "fg_made_50_59",
    "fg_made_60_", "fg_made_distance", "pat_made", "pat_att", "fantasy_points_ppr", "target_share",
]  # fmt: skip


def _stat_row(**over: str) -> list[str]:
    base = dict.fromkeys(STATS_HEADER, "0")
    base.update(
        player_id="00-0033873", player_display_name="P. Mahomes", position="QB", season="2025",
        week="1", season_type="REG", team="KC", attempts="39", completions="24",
        passing_yards="258", passing_tds="1", carries="6", rushing_yards="57", rushing_tds="1",
        fantasy_points_ppr="26.02", target_share="NA",
    )  # fmt: skip
    base.update(over)
    return [base[h] for h in STATS_HEADER]


def test_actual_rows_map_to_sleeper_vocab_and_resolve_ids() -> None:
    payload = _csv(STATS_HEADER, _stat_row())
    unresolved: set[str] = set()
    rows = nflverse_actual_rows(
        payload, _snap("stats_player_week", 2025), {"00-0033873": "4046"}, unresolved
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["category"] == "actual" and r["source"] == "nflverse"
    assert r["sleeper_id"] == "4046" and r["source_player_id"] == "00-0033873"
    assert r["season"] == 2025 and r["week"] == 1 and r["gp"] == 1.0
    assert r["stats"] == {
        "pass_att": 39.0, "pass_cmp": 24.0, "pass_yd": 258.0, "pass_td": 1.0,
        "rush_att": 6.0, "rush_yd": 57.0, "rush_td": 1.0,
    }  # fmt: skip
    assert r["provider_points"] == 26.02
    assert unresolved == set()


def test_actual_rows_filter_positions_postseason_and_na_ids() -> None:
    payload = _csv(
        STATS_HEADER,
        _stat_row(position="LB"),  # not a fantasy position
        _stat_row(season_type="POST"),  # playoffs
        _stat_row(player_id="NA"),  # unattributed
        _stat_row(player_id="00-0000001", position="FB", team="LA"),  # FB → RB, LA → LAR
    )
    unresolved: set[str] = set()
    rows = nflverse_actual_rows(payload, _snap("stats_player_week", 2025), {}, unresolved)
    assert len(rows) == 1
    assert rows[0]["position"] == "RB" and rows[0]["team"] == "LAR"
    assert rows[0]["sleeper_id"] is None and unresolved == {"00-0000001"}


def test_actual_rows_kicker_buckets_and_fgm_50p() -> None:
    payload = _csv(
        STATS_HEADER,
        _stat_row(
            player_id="00-0039999",
            position="K",
            attempts="0",
            completions="0",
            passing_yards="0",
            passing_tds="0",
            carries="0",
            rushing_yards="0",
            rushing_tds="0",
            fg_made="3",
            fg_att="4",
            fg_made_50_59="1",
            fg_made_60_="1",
            fg_made_distance="140",
            pat_made="2",
            pat_att="2",
            fantasy_points_ppr="0",
        ),  # fmt: skip
    )
    rows = nflverse_actual_rows(payload, _snap("stats_player_week", 2025), {}, set())
    st = rows[0]["stats"]
    assert st["fgm"] == 3 and st["fga"] == 4 and st["fgm_yds"] == 140
    assert st["fgm_50_59"] == 1 and st["fgm_60p"] == 1 and st["fgm_50p"] == 2
    assert st["xpm"] == 2 and st["xpa"] == 2


def test_snap_count_rows() -> None:
    header = [
        "game_id", "season", "game_type", "week", "player", "pfr_player_id", "position", "team",
        "opponent", "offense_snaps", "offense_pct", "defense_snaps", "defense_pct", "st_snaps", "st_pct",
    ]  # fmt: skip
    payload = _csv(
        header,
        ["2025_01_KC_LAC", "2025", "REG", "1", "T. Kelce", "KelcTr00", "TE", "KC", "LAC", "60", "0.9", "0", "0", "2", "0.1"],
        ["2025_01_KC_LAC", "2025", "REG", "1", "C. Humphrey", "HumpCr00", "C", "KC", "LAC", "66", "1", "0", "0", "0", "0"],
        ["2025_19_KC_LAC", "2025", "POST", "19", "T. Kelce", "KelcTr00", "TE", "KC", "LAC", "60", "0.9", "0", "0", "2", "0.1"],
    )  # fmt: skip
    rows = snap_count_rows(payload, _snap("snap_counts", 2025), {"KelcTr00": "1466"})
    assert len(rows) == 1  # C dropped (not fantasy), POST dropped
    r = rows[0]
    assert r["sleeper_id"] == "1466" and r["offense_snaps"] == 60 and r["offense_pct"] == 0.9
    assert r["week"] == 1 and r["team"] == "KC" and r["opponent"] == "LAC"


def test_expected_points_rows_drop_team_and_diff_columns() -> None:
    header = [
        "season", "posteam", "week", "game_id", "player_id", "full_name", "position",
        "rush_attempt", "rush_yards_gained_exp", "rush_yards_gained_diff", "rush_attempt_team",
        "total_fantasy_points", "total_fantasy_points_exp",
    ]  # fmt: skip
    payload = _csv(
        header,
        ["2025", "ATL", "1", "2025_01_TB_ATL", "00-0038542", "Bijan Robinson", "RB", "12", "47.37", "-23.37", "28", "24.4", "20.01"],
        ["2025", "LA", "1", "2025_01_HOU_LA", "NA", "NA", "NA", "0", "0", "0", "0", "0.97", "0.97"],
    )  # fmt: skip
    rows = expected_points_rows(payload, _snap("ff_opportunity", 2025), {"00-0038542": "9509"})
    assert len(rows) == 1  # NA player dropped
    r = rows[0]
    assert r["sleeper_id"] == "9509" and r["gsis_id"] == "00-0038542"
    assert r["total_fantasy_points_exp"] == 20.01
    assert r["ep"] == {
        "rush_attempt": 12.0,
        "rush_yards_gained_exp": 47.37,
        "total_fantasy_points": 24.4,
        "total_fantasy_points_exp": 20.01,
    }
    assert "rush_yards_gained_diff" not in r["ep"] and "rush_attempt_team" not in r["ep"]
