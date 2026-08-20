"""Flex-aware VORP (LS-27): ranking behavior, live-baseline invariants, 2025 skew sanity check.

The skew check aggregates the LS-21 parity fixture (all 5,712 real 2025 weekly offense actuals)
into season totals and re-derives the baselines — reproducing the plan doc's 2025 flex-fill skew
(17 WR / 5 RB / 2 TE observed in real lineups) within one seat: value-greedy fill over season
totals lands at 16 WR / 6 RB / 2 TE, because weekly start/sit decisions aren't recoverable from
season sums. The heavy-WR skew and the TE share are exact.
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from pathlib import Path

import pytest

from lazy_sleeper.board import RosterShape, derive_baselines, live_vorp, vorp_board
from lazy_sleeper.providers.base import PlayerProjection
from lazy_sleeper.scoring import ScoringRules, default_scorer

PARITY_FIXTURE = Path(__file__).parent / "fixtures" / "nflverse_actuals_2025_weekly.json.gz"


def _proj(sid: str, pos: str | None, pts: float, **kw) -> PlayerProjection:
    kw.setdefault("team", None)
    return PlayerProjection(sleeper_id=sid, position=pos, points=pts, source="test", **kw)


class _FakeProvider:
    name = "test"

    def __init__(self, projections: list[PlayerProjection]) -> None:
        self._projections = projections

    def projections(self, season: int, week: int | None = None) -> list[PlayerProjection]:
        return list(self._projections)


# --- vorp_board --------------------------------------------------------------


def test_vorp_is_points_minus_position_baseline() -> None:
    board = vorp_board(
        [_proj("rb1", "RB", 100.0), _proj("wr1", "WR", 95.0)], {"RB": 80.0, "WR": 60.0}
    )
    assert [(v.sleeper_id, v.vorp) for v in board] == [("wr1", 35.0), ("rb1", 20.0)]
    assert board[0].baseline == 60.0
    assert board[0].points == 95.0


def test_position_rank_and_vorp_ordering_are_independent() -> None:
    # rb1 out-ranks wr1 on points within-position terms, but WR's lower baseline wins the board.
    board = vorp_board(
        [_proj("rb1", "RB", 100.0), _proj("rb2", "RB", 90.0), _proj("wr1", "WR", 95.0)],
        {"RB": 85.0, "WR": 55.0},
    )
    assert [v.sleeper_id for v in board] == ["wr1", "rb1", "rb2"]
    assert [v.pos_rank for v in board] == [1, 1, 2]


def test_unbaselined_positions_are_dropped() -> None:
    board = vorp_board(
        [_proj("qb1", "QB", 300.0), _proj("k1", "K", 140.0), _proj("x", None, 99.0)],
        {"QB": 280.0},
    )
    assert [v.sleeper_id for v in board] == ["qb1"]


def test_ensemble_components_pass_through() -> None:
    p = _proj("rb1", "RB", 100.0, components={"sleeper": 98.0, "espn": 102.0})
    board = vorp_board([p], {"RB": 80.0})
    assert board[0].components == {"sleeper": 98.0, "espn": 102.0}


# --- live_vorp ---------------------------------------------------------------


def test_live_baseline_puts_the_last_starter_at_zero() -> None:
    shape = RosterShape(teams=2, dedicated={"RB": 1, "WR": 1}, flex=(("RB", "WR", "TE"),))
    provider = _FakeProvider(
        [_proj(f"rb{i}", "RB", p) for i, p in enumerate([100.0, 90.0, 80.0, 70.0])]
        + [_proj(f"wr{i}", "WR", p) for i, p in enumerate([95.0, 85.0, 84.0, 60.0])]
    )
    board = live_vorp(provider, shape, season=2026)
    by_id = {v.sleeper_id: v for v in board}
    # LS-26 test pool: flex takes WR 84 and RB 80 → those last starters sit at exactly 0.
    assert by_id["rb2"].vorp == 0.0
    assert by_id["wr2"].vorp == 0.0
    assert all(by_id[sid].vorp > 0 for sid in ("rb0", "rb1", "wr0", "wr1"))
    assert by_id["rb3"].vorp < 0 and by_id["wr3"].vorp < 0


# --- 2025 flex-skew sanity check (real actuals, no DB) ------------------------


def test_2025_actuals_reproduce_the_flex_skew_within_one_seat(
    sleeper_league_payload: dict,
) -> None:
    rules = ScoringRules.from_league(sleeper_league_payload)
    scorer = default_scorer(rules)
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for r in json.loads(gzip.decompress(PARITY_FIXTURE.read_bytes())):
        if r["sleeper_id"] is None:  # unresolved nflverse ids, filtered by the DB loader too
            continue
        totals[(str(r["sleeper_id"]), r["position"])] += scorer.score(r["stats"], r["position"])
    rows = [(sid, pos, pts) for (sid, pos), pts in totals.items()]

    baselines = derive_baselines(rows, RosterShape.from_rules(rules))
    fills = {pos: b.flex_fills for pos, b in baselines.items()}
    # Plan doc observed 17 WR / 5 RB / 2 TE in real 2025 lineups; value-greedy over season
    # totals lands one seat away on the RB/WR edge and exact on TE.
    assert fills == {"QB": 0, "RB": 6, "WR": 16, "TE": 2}
    assert baselines["QB"].cutoff_rank == 12
    assert baselines["RB"].cutoff_rank == 30
    assert baselines["WR"].cutoff_rank == 40
    assert baselines["TE"].cutoff_rank == 14
    # the plan's "QB12 ≈ 283, RB/WR/TE ≈ 146–150" anchors
    assert baselines["QB"].points == pytest.approx(282.9, abs=0.5)
    assert baselines["RB"].points == pytest.approx(146.7, abs=0.5)
    assert baselines["WR"].points == pytest.approx(150.1, abs=0.5)
    assert baselines["TE"].points == pytest.approx(147.3, abs=0.5)
