"""Tiers and cliffs (LS-28): adaptive gap breaks, absolute cliff flag, depth handling."""

from __future__ import annotations

from lazy_sleeper.board import TierConfig, assign_tiers, vorp_board
from lazy_sleeper.providers.base import PlayerProjection


def _board(pos_points: dict[str, list[float]], baselines: dict[str, float] | None = None):
    projections = [
        PlayerProjection(
            sleeper_id=f"{pos.lower()}{i}", position=pos, team=None, points=pts, source="test"
        )
        for pos, points in pos_points.items()
        for i, pts in enumerate(points)
    ]
    return vorp_board(projections, baselines or dict.fromkeys(pos_points, 0.0))


def _by_id(rows):
    return {r.value.sleeper_id: r for r in rows}


def test_tier_breaks_on_unusually_large_gaps() -> None:
    # Gaps: 2, 2, 20, 2 — median of the window is 2, so only the 20 breaks (≥ max(4, 2×2)).
    rows = _by_id(
        assign_tiers(
            _board({"RB": [100.0, 98.0, 96.0, 76.0, 74.0]}),
            TierConfig(depth={"RB": 5}),
        )
    )
    assert [rows[f"rb{i}"].tier for i in range(5)] == [1, 1, 1, 2, 2]


def test_min_gap_floor_stops_micro_tiers() -> None:
    # Dense field: median gap 1 → adaptive threshold would be 2, but min_gap=4 keeps one tier.
    rows = _by_id(
        assign_tiers(
            _board({"WR": [50.0, 49.0, 47.0, 46.0, 45.0]}),
            TierConfig(depth={"WR": 5}, min_gap=4.0),
        )
    )
    assert {r.tier for r in rows.values()} == {1}


def test_thresholds_scale_with_the_position() -> None:
    # The same shape ×10 tiers identically — the multiplier rides the position's median gap.
    small = assign_tiers(
        _board({"TE": [10.0, 9.0, 8.0, 4.0, 3.0]}), TierConfig(depth={"TE": 5}, min_gap=0.5)
    )
    big = assign_tiers(
        _board({"QB": [100.0, 90.0, 80.0, 40.0, 30.0]}), TierConfig(depth={"QB": 5}, min_gap=5.0)
    )
    assert [r.tier for r in small] == [r.tier for r in big] == [1, 1, 1, 2, 2]


def test_cliff_is_absolute_and_independent_of_tiers() -> None:
    rows = _by_id(
        assign_tiers(
            _board({"RB": [100.0, 98.0, 80.0, 79.0]}),
            TierConfig(depth={"RB": 4}, cliff_gap=15.0),
        )
    )
    assert rows["rb1"].cliff is True  # 18-point drop to rb2
    assert rows["rb1"].gap_to_next == 18.0
    assert rows["rb0"].cliff is False
    assert rows["rb3"].cliff is False and rows["rb3"].gap_to_next is None  # last at position


def test_players_past_depth_get_no_tier() -> None:
    rows = _by_id(assign_tiers(_board({"K": [140.0, 130.0, 120.0]}), TierConfig(depth={"K": 2})))
    assert rows["k0"].tier == 1 and rows["k1"].tier is not None
    assert rows["k2"].tier is None


def test_output_preserves_vorp_order_across_positions() -> None:
    board = _board({"RB": [100.0, 60.0], "WR": [90.0, 80.0]})
    rows = assign_tiers(board, TierConfig(depth={"RB": 2, "WR": 2}))
    assert [r.value.sleeper_id for r in rows] == [v.sleeper_id for v in board]
