"""ESPN kona stat-id decoder → Sleeper stat vocabulary.

The mapping below was verified empirically on 2026-08-16 by diffing ESPN 2025 season *actuals*
(statSourceId=0, statSplitTypeId=0) against nflverse 2025 totals for a QB, RB, WR, K and a D/ST
(e.g. id 3 = pass_yd 3587 for Mahomes, 24 = rush_yd 1478 for B. Robinson, 42 = rec_yd 1412 for
Chase, 83/84 = fgm/fga 36/42 for Aubrey, 99 = sack 30 for BAL). Ids not listed are ignored.

Where ESPN's buckets do not align with Sleeper's (K distance buckets under 40 yds; DEF
points-allowed 14-17/18-21/22-27/35-45/46+), the ESPN-native bucket is kept under a descriptive
key so the scoring engine can decide how to approximate — nothing is silently merged.
"""

from __future__ import annotations

from typing import Any

# ESPN stat id → Sleeper key (or ESPN-native key when no exact Sleeper equivalent)
STAT_IDS: dict[int, str] = {
    # passing
    0: "pass_att",
    1: "pass_cmp",
    2: "pass_inc",
    3: "pass_yd",
    4: "pass_td",
    19: "pass_2pt",
    20: "pass_int",
    64: "pass_sack",
    # rushing
    23: "rush_att",
    24: "rush_yd",
    25: "rush_td",
    26: "rush_2pt",
    # receiving
    53: "rec",
    42: "rec_yd",
    43: "rec_td",
    44: "rec_2pt",
    58: "rec_tgt",
    # fumbles
    68: "fum",
    72: "fum_lost",
    # kicking
    83: "fgm",
    84: "fga",
    85: "fgmiss",
    74: "fgm_50p",
    75: "fga_50p",
    76: "fgmiss_50p",
    77: "fgm_40_49",
    78: "fga_40_49",
    79: "fgmiss_40_49",
    80: "fgm_0_39",  # ESPN merges Sleeper's 0_19 / 20_29 / 30_39
    81: "fga_0_39",
    82: "fgmiss_0_39",
    86: "xpm",
    87: "xpa",
    88: "xpmiss",
    # defense / special teams (team D/ST)
    99: "sack",
    95: "int",
    96: "fum_rec",
    106: "ff",
    98: "safe",
    97: "blk_kick",
    94: "def_td",
    93: "def_st_td",  # blocked kick returned for TD
    101: "def_kr_td",
    102: "def_pr_td",
    103: "def_fum_td",
    104: "def_int_td",
    120: "pts_allow",
    89: "pts_allow_0",
    90: "pts_allow_1_6",
    91: "pts_allow_7_13",
    92: "pts_allow_14_17",  # ESPN-native; Sleeper uses 14_20
    121: "pts_allow_18_21",  # ESPN-native
    122: "pts_allow_22_27",  # ESPN-native; Sleeper uses 21_27
    123: "pts_allow_28_34",
    124: "pts_allow_35_45",  # ESPN-native; Sleeper uses 35p
    125: "pts_allow_46p",  # ESPN-native
    127: "yds_allow",
    128: "yds_allow_0_100",
    129: "yds_allow_100_199",
    130: "yds_allow_200_299",
    131: "yds_allow_300_349",
    132: "yds_allow_350_399",
    133: "yds_allow_400_449",
    134: "yds_allow_450_499",
    135: "yds_allow_500_549",
    136: "yds_allow_550p",
    # games
    210: "gp",
}

POSITIONS: dict[int, str] = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}

# ESPN proTeamId → Sleeper team abbreviation (Sleeper DEF player_id == team abbreviation)
TEAMS: dict[int, str] = {
    1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN", 8: "DET",
    9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA", 16: "MIN",
    17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC",
    25: "SF", 26: "SEA", 27: "TB", 28: "WAS", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}  # fmt: skip

SOURCE_ACTUAL = 0
SOURCE_PROJ = 1
SPLIT_SEASON = 0
SPLIT_WEEK = 1


def decode_stats(raw: dict[str, Any]) -> dict[str, float]:
    """ESPN {stat_id_str: value} → {sleeper_key: value}, dropping unknown ids and zeros."""
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            key = STAT_IDS[int(k)]
        except (KeyError, ValueError):
            continue
        if v is None:
            continue
        fv = float(v)
        if fv != 0.0:
            out[key] = fv
    return out
