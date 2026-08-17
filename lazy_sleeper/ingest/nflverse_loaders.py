"""nflverse / ffverse CSV snapshots → core.actuals, core.snap_counts, core.expected_points.

Pure transforms return row dicts; writes happen in stat_loaders.write_* helpers.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from typing import Any

from lazy_sleeper.db.models import Snapshot

# nflverse stats_player_week column → Sleeper stat key. Verified 2026-08-16 against the file.
STAT_COLUMNS: dict[str, str] = {
    # passing
    "attempts": "pass_att",
    "completions": "pass_cmp",
    "passing_yards": "pass_yd",
    "passing_tds": "pass_td",
    "passing_interceptions": "pass_int",
    "passing_2pt_conversions": "pass_2pt",
    "sacks_suffered": "pass_sack",
    "passing_first_downs": "pass_fd",
    "passing_air_yards": "pass_air_yd",
    # rushing
    "carries": "rush_att",
    "rushing_yards": "rush_yd",
    "rushing_tds": "rush_td",
    "rushing_2pt_conversions": "rush_2pt",
    "rushing_first_downs": "rush_fd",
    # receiving
    "receptions": "rec",
    "targets": "rec_tgt",
    "receiving_yards": "rec_yd",
    "receiving_tds": "rec_td",
    "receiving_2pt_conversions": "rec_2pt",
    "receiving_first_downs": "rec_fd",
    "receiving_air_yards": "rec_air_yd",
    "receiving_yards_after_catch": "rec_yac",
    "target_share": "target_share",
    "air_yards_share": "air_yards_share",
    "wopr": "wopr",
    # fumbles / special teams
    "fumbles_total": "fum",
    "fumbles_lost_total": "fum_lost",
    "fumble_recovery_tds": "fum_rec_td",
    "special_teams_tds": "st_td",
    "kickoff_return_yards": "kr_yd",
    "punt_return_yards": "pr_yd",
    # kicking
    "fg_made": "fgm",
    "fg_att": "fga",
    "fg_missed": "fgmiss",
    "fg_blocked": "fg_blocked",
    "fg_made_0_19": "fgm_0_19",
    "fg_made_20_29": "fgm_20_29",
    "fg_made_30_39": "fgm_30_39",
    "fg_made_40_49": "fgm_40_49",
    "fg_made_50_59": "fgm_50_59",
    "fg_made_60_": "fgm_60p",
    "fg_missed_0_19": "fgmiss_0_19",
    "fg_missed_20_29": "fgmiss_20_29",
    "fg_missed_30_39": "fgmiss_30_39",
    "fg_missed_40_49": "fgmiss_40_49",
    "fg_missed_50_59": "fgmiss_50_59",
    "fg_missed_60_": "fgmiss_60p",
    "fg_made_distance": "fgm_yds",
    "pat_made": "xpm",
    "pat_att": "xpa",
    "pat_missed": "xpmiss",
}

FANTASY_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "FB"})
_NULL = frozenset({"", "NA", "NaN", "null"})
# nflverse team codes that differ from Sleeper's
TEAM_FIX = {"LA": "LAR"}


def _team(v: str | None) -> str | None:
    if v is None or v in _NULL:
        return None
    return TEAM_FIX.get(v, v)


def _id(v: str | None) -> str | None:
    return None if v is None or v in _NULL else v


def _num(v: str | None) -> float | None:
    if v is None or v in _NULL:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _rows(payload: bytes) -> Iterator[dict[str, str]]:
    yield from csv.DictReader(io.StringIO(payload.decode("utf-8", errors="replace")))


def nflverse_actual_rows(
    payload: bytes, snapshot: Snapshot, gsis_to_sleeper: dict[str, str], unresolved: set[str]
) -> list[dict[str, Any]]:
    """stats_player_week CSV → core.actuals rows (REG season, fantasy positions only)."""
    out: list[dict[str, Any]] = []
    for r in _rows(payload):
        if r.get("season_type", "REG") != "REG":
            continue
        pos = r.get("position") or ""
        if pos not in FANTASY_POSITIONS:
            continue
        stats: dict[str, float] = {}
        for col, key in STAT_COLUMNS.items():
            v = _num(r.get(col))
            if v:
                stats[key] = v
        # Sleeper has fgm_50p as well as the 50_59/60p split; provide both for scorer convenience
        fg50 = (stats.get("fgm_50_59") or 0) + (stats.get("fgm_60p") or 0)
        if fg50:
            stats["fgm_50p"] = fg50
        if not stats:
            continue
        gsis = _id(r.get("player_id"))
        if gsis is None:
            continue
        sid = gsis_to_sleeper.get(gsis)
        if sid is None:
            unresolved.add(gsis)
        out.append(
            {
                "category": "actual",
                "snapshot_id": snapshot.id,
                "source": "nflverse",
                "season": int(r["season"]),
                "week": int(r["week"]),
                "source_player_id": gsis,
                "sleeper_id": sid,
                "position": "RB" if pos == "FB" else pos,
                "team": _team(r.get("team")),
                "gp": 1.0,
                "provider_points": _num(r.get("fantasy_points_ppr")),
                "stats": stats,
            }
        )
    return out


def snap_count_rows(
    payload: bytes, snapshot: Snapshot, pfr_to_sleeper: dict[str, str]
) -> list[dict[str, Any]]:
    """snap_counts CSV → core.snap_counts rows (REG season, fantasy positions only)."""
    out: list[dict[str, Any]] = []
    for r in _rows(payload):
        if r.get("game_type", "REG") != "REG":
            continue
        if (r.get("position") or "") not in FANTASY_POSITIONS:
            continue
        pfr = _id(r.get("pfr_player_id"))
        if pfr is None:
            continue
        out.append(
            {
                "snapshot_id": snapshot.id,
                "season": int(r["season"]),
                "week": int(r["week"]),
                "pfr_player_id": pfr,
                "sleeper_id": pfr_to_sleeper.get(pfr),
                "player": r.get("player") or None,
                "position": r.get("position") or None,
                "team": _team(r.get("team")),
                "opponent": _team(r.get("opponent")),
                "offense_snaps": _int(r.get("offense_snaps")),
                "offense_pct": _num(r.get("offense_pct")),
                "defense_snaps": _int(r.get("defense_snaps")),
                "defense_pct": _num(r.get("defense_pct")),
                "st_snaps": _int(r.get("st_snaps")),
                "st_pct": _num(r.get("st_pct")),
            }
        )
    return out


def expected_points_rows(
    payload: bytes, snapshot: Snapshot, gsis_to_sleeper: dict[str, str]
) -> list[dict[str, Any]]:
    """ff_opportunity ep_weekly CSV → core.expected_points rows. Drops _team/_diff columns."""
    out: list[dict[str, Any]] = []
    for r in _rows(payload):
        gsis = _id(r.get("player_id"))
        if gsis is None:
            continue  # unattributed team plays carry player_id NA
        ep: dict[str, float] = {}
        for k, v in r.items():
            if k.endswith("_team") or k.endswith("_diff"):
                continue
            if k in ("season", "week", "game_id", "player_id", "full_name", "position", "posteam"):
                continue
            n = _num(v)
            if n:
                ep[k] = n
        out.append(
            {
                "snapshot_id": snapshot.id,
                "season": int(r["season"]),
                "week": int(r["week"]),
                "gsis_id": gsis,
                "sleeper_id": gsis_to_sleeper.get(gsis),
                "full_name": r.get("full_name") or None,
                "position": r.get("position") or None,
                "team": _team(r.get("posteam")),
                "total_fantasy_points": _num(r.get("total_fantasy_points")),
                "total_fantasy_points_exp": _num(r.get("total_fantasy_points_exp")),
                "ep": ep,
            }
        )
    return out


def _int(v: str | None) -> int | None:
    n = _num(v)
    return int(n) if n is not None else None
