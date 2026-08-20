"""ADP-delta and provider-disagreement flags (LS-29)."""

from __future__ import annotations

from lazy_sleeper.board import (
    TierConfig,
    assign_tiers,
    flag_adp,
    flag_disagreement,
    position_bias,
    vorp_board,
)
from lazy_sleeper.providers.base import PlayerProjection


def _rows(players: list[tuple[str, str, float, dict[str, float] | None]]):
    """(id, pos, points, components) → tiered BoardRows in VORP order (baseline 0 everywhere)."""
    projections = [
        PlayerProjection(
            sleeper_id=sid,
            position=pos,
            team=None,
            points=pts,
            source="test",
            components=comps or {},
        )
        for sid, pos, pts, comps in players
    ]
    positions = {p.position for p in projections}
    return assign_tiers(vorp_board(projections, dict.fromkeys(positions, 0.0)), TierConfig())


def _by_id(rows):
    return {r.value.sleeper_id: r for r in rows}


# --- ADP delta ---------------------------------------------------------------


def test_adp_delta_is_adp_minus_board_rank() -> None:
    rows = _by_id(
        flag_adp(
            _rows([("a", "RB", 300.0, None), ("b", "RB", 200.0, None), ("c", "WR", 100.0, None)]),
            {"a": 1.0, "b": 30.0, "c": 2.5},
        )
    )
    assert rows["a"].adp_delta == 0.0
    assert rows["b"].adp_delta == 28.0  # ranked 2nd, market takes him at 30 → value
    assert rows["c"].adp_delta == -0.5


def test_flag_threshold_is_the_larger_of_floor_and_fraction_of_adp() -> None:
    config = TierConfig(adp_min_delta=12.0, adp_pct=0.25)
    board = _rows([(f"p{i}", "RB", 300.0 - i, None) for i in range(4)])
    rows = _by_id(flag_adp(board, {"p0": 14.0, "p1": 13.0, "p2": 100.0, "p3": 28.0}, config))
    assert rows["p0"].adp_flag == "value"  # delta 13 ≥ max(12, 3.5)
    assert rows["p1"].adp_flag is None  # delta 11 < 12 floor
    assert rows["p2"].adp_flag == "value"  # delta 97 ≥ max(12, 25)
    assert rows["p3"].adp_flag == "value"  # delta 24 ≥ max(12, 7)


def test_reach_when_market_takes_him_earlier_than_the_board() -> None:
    board = _rows([(f"p{i}", "WR", 300.0 - i, None) for i in range(30)])
    rows = _by_id(flag_adp(board, {"p29": 5.0}, TierConfig(adp_min_delta=12.0, adp_pct=0.25)))
    assert rows["p29"].adp_delta == -25.0
    assert rows["p29"].adp_flag == "reach"


def test_missing_adp_leaves_the_row_unflagged_and_keeps_order() -> None:
    board = _rows([("a", "RB", 300.0, None), ("b", "RB", 200.0, None)])
    flagged = flag_adp(board, {"b": 40.0})
    assert [r.value.sleeper_id for r in flagged] == ["a", "b"]
    assert flagged[0].adp is None and flagged[0].adp_flag is None
    assert flagged[0].tier == 1  # tier/cliff annotations survive the pass
    assert flagged[1].adp == 40.0


# --- disagreement -------------------------------------------------------------


def test_spread_and_flag_use_the_larger_of_floor_and_fraction() -> None:
    config = TierConfig(disagree_min_pts=20.0, disagree_pct=0.15, debias_disagreement=False)
    rows = _by_id(
        flag_disagreement(
            _rows(
                [
                    ("a", "RB", 200.0, {"sleeper": 210.0, "espn": 190.0}),  # 20 ≥ max(20, 30)? no
                    ("b", "RB", 200.0, {"sleeper": 216.0, "espn": 184.0}),  # 32 ≥ 30 → yes
                    ("c", "TE", 80.0, {"sleeper": 90.0, "espn": 70.0}),  # 20 ≥ max(20, 12) → yes
                    ("d", "TE", 80.0, {"sleeper": 85.0, "espn": 75.0}),  # 10 < 20 floor
                ]
            ),
            config,
        )
    )
    assert rows["a"].spread == 20.0 and not rows["a"].disagree
    assert rows["b"].spread == 32.0 and rows["b"].disagree
    assert rows["c"].spread == 20.0 and rows["c"].disagree
    assert rows["d"].spread == 10.0 and not rows["d"].disagree


def test_missing_member_means_no_spread_and_no_flag() -> None:
    rows = _by_id(
        flag_disagreement(
            _rows([("a", "RB", 200.0, {"sleeper": 200.0}), ("b", "RB", 100.0, {})]),
            TierConfig(debias_disagreement=False),
        )
    )
    assert rows["a"].spread is None and not rows["a"].disagree
    assert rows["b"].spread is None and not rows["b"].disagree


def test_debias_removes_a_systematic_position_offset() -> None:
    # Every DEF: Sleeper 20% under, ESPN 20% over the blend — pure bias, no real disagreement —
    # except "odd", whose members split the other way round on top of it.
    defs = [(f"d{i}", "DEF", 100.0, {"sleeper": 80.0, "espn": 120.0}) for i in range(5)] + [
        ("odd", "DEF", 100.0, {"sleeper": 120.0, "espn": 80.0})
    ]
    config = TierConfig(disagree_min_pts=20.0, disagree_pct=0.15)
    raw = _by_id(flag_disagreement(_rows(defs), TierConfig(debias_disagreement=False)))
    debiased = _by_id(flag_disagreement(_rows(defs), config))

    assert all(raw[f"d{i}"].disagree for i in range(5))  # raw spread flags the whole position
    assert not any(debiased[f"d{i}"].disagree for i in range(5))
    assert debiased["d0"].spread == 0.0
    assert debiased["odd"].disagree  # the genuine outlier still flags after debiasing
    assert debiased["odd"].spread > raw["odd"].spread


def test_position_bias_is_per_position_and_ignores_partial_rows() -> None:
    bias = position_bias(
        _rows(
            [
                ("a", "QB", 100.0, {"sleeper": 110.0, "espn": 90.0}),
                ("b", "QB", 100.0, {"sleeper": 110.0, "espn": 90.0}),
                ("c", "QB", 100.0, {"sleeper": 100.0}),  # rookie fallback — excluded
                ("d", "RB", 100.0, {"sleeper": 100.0, "espn": 100.0}),
            ]
        )
    )
    assert bias[("QB", "sleeper")] == 1.1
    assert bias[("QB", "espn")] == 0.9
    assert bias[("RB", "sleeper")] == 1.0 and bias[("RB", "espn")] == 1.0
