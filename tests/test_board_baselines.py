"""Replacement baselines (LS-26): roster-derived cutoffs, value-greedy flex, season averaging."""

from __future__ import annotations

import pytest

from lazy_sleeper.board import (
    PositionBaseline,
    RosterShape,
    average_baselines,
    derive_baselines,
)
from lazy_sleeper.scoring import ScoringRules


def _rows(**by_position: list[float]) -> list[tuple[str, str, float]]:
    """Points lists per position → (sleeper_id, position, points) rows, ids pos0, pos1, ..."""
    return [
        (f"{pos}{i}", pos, pts)
        for pos, points in by_position.items()
        for i, pts in enumerate(points)
    ]


# --- RosterShape -----------------------------------------------------------


def test_shape_from_league_payload(sleeper_league_payload: dict) -> None:
    shape = RosterShape.from_rules(ScoringRules.from_league(sleeper_league_payload))
    assert shape.teams == 12
    assert shape.dedicated == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}
    assert shape.flex == (("RB", "WR", "TE"), ("RB", "WR", "TE"))


def test_shape_requires_team_count(sleeper_league_payload: dict) -> None:
    payload = dict(sleeper_league_payload, total_rosters=None)
    with pytest.raises(ValueError, match="total_rosters"):
        RosterShape.from_rules(ScoringRules.from_league(payload))


# --- derive_baselines ------------------------------------------------------


def test_dedicated_cutoff_is_last_starter() -> None:
    shape = RosterShape(teams=2, dedicated={"QB": 1}, flex=())
    baselines = derive_baselines(_rows(QB=[50.0, 40.0, 30.0]), shape)
    assert baselines["QB"] == PositionBaseline("QB", cutoff_rank=2, points=40.0, flex_fills=0)


def test_flex_goes_to_best_remaining_value() -> None:
    # After dedicated starters (2 RB / 2 WR), the two flex seats should take WR 84 and RB 80.
    shape = RosterShape(teams=2, dedicated={"RB": 1, "WR": 1}, flex=(("RB", "WR", "TE"),))
    baselines = derive_baselines(
        _rows(RB=[100.0, 90.0, 80.0, 70.0], WR=[95.0, 85.0, 84.0, 60.0]), shape
    )
    assert baselines["RB"] == PositionBaseline("RB", cutoff_rank=3, points=80.0, flex_fills=1)
    assert baselines["WR"] == PositionBaseline("WR", cutoff_rank=3, points=84.0, flex_fills=1)


def test_flex_share_skews_to_the_deeper_position() -> None:
    # WR depth dominates the flex pool → WR absorbs both seats, RB cutoff stays dedicated-only.
    shape = RosterShape(teams=2, dedicated={"RB": 1, "WR": 1}, flex=(("RB", "WR", "TE"),))
    baselines = derive_baselines(
        _rows(RB=[100.0, 90.0, 10.0, 9.0], WR=[95.0, 85.0, 84.0, 83.0]), shape
    )
    assert baselines["RB"].cutoff_rank == 2
    assert baselines["RB"].flex_fills == 0
    assert baselines["WR"] == PositionBaseline("WR", cutoff_rank=4, points=83.0, flex_fills=2)


def test_restrictive_flex_seats_fill_first() -> None:
    # WRRB seat must claim RB 90 before the open FLEX would; FLEX then takes the next TE (80).
    # Filling the open seat first would waste it on the RB and strand WRRB with the 10-point WR.
    shape = RosterShape(
        teams=1,
        dedicated={"RB": 1, "WR": 1, "TE": 1},
        flex=(("RB", "WR", "TE"), ("RB", "WR")),
    )
    baselines = derive_baselines(_rows(RB=[100.0, 90.0], WR=[95.0, 10.0], TE=[80.0, 85.0]), shape)
    assert baselines["RB"].flex_fills == 1
    assert baselines["TE"].flex_fills == 1
    assert baselines["WR"].flex_fills == 0
    assert baselines["TE"] == PositionBaseline("TE", cutoff_rank=2, points=80.0, flex_fills=1)


def test_short_position_clamps_at_last_player() -> None:
    shape = RosterShape(teams=12, dedicated={"DEF": 1}, flex=())
    baselines = derive_baselines(_rows(DEF=[120.0, 110.0]), shape)
    assert baselines["DEF"] == PositionBaseline("DEF", cutoff_rank=2, points=110.0, flex_fills=0)


def test_positions_outside_the_roster_are_ignored() -> None:
    shape = RosterShape(teams=2, dedicated={"QB": 1}, flex=())
    rows = _rows(QB=[50.0, 40.0]) + [("x1", "K", 150.0), ("x2", None, 99.0)]
    assert set(derive_baselines(rows, shape)) == {"QB"}


# --- average_baselines -----------------------------------------------------


def test_average_over_seasons_that_produced_a_baseline() -> None:
    b = lambda pos, pts: PositionBaseline(pos, 12, pts, 0)  # noqa: E731
    avg = average_baselines(
        {
            2023: {"QB": b("QB", 280.0), "TE": b("TE", 140.0)},
            2024: {"QB": b("QB", 290.0)},
        }
    )
    assert avg == {"QB": 285.0, "TE": 140.0}
