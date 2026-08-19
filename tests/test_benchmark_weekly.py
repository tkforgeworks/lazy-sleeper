"""Weekly scoreboard (LS-24) — per-week pool, naive baseline, and the season roll-up."""

import math

import pytest

from lazy_sleeper.benchmark.season import PoolPlayer
from lazy_sleeper.benchmark.weekly import aggregate, naive_weekly, run_season, weekly_pool


def test_weekly_pool_keeps_players_some_provider_expects_to_play():
    pool = [PoolPlayer("a", "RB", 1), PoolPlayer("b", "RB", 2), PoolPlayer("c", "RB", 3)]
    week = {"sleeper": {"a": 12.0, "b": 0.0}, "espn": {"b": 0.0, "c": 9.0}, "naive": {"b": 8.0}}
    # b: both providers project 0 (bye) → out, even though naive has a number for him
    assert [p.sleeper_id for p in weekly_pool(pool, week)] == ["a", "c"]


def test_naive_weekly_is_trailing_mean_with_prior_season_for_week_one():
    actuals = {1: {"a": 10.0, "b": 4.0}, 2: {"a": 20.0}, 3: {"a": 30.0, "b": 8.0}}
    prior = {1: {"a": 5.0}, 2: {"a": 7.0}}  # a averaged 6.0 last season; b was not around
    naive = naive_weekly(actuals, prior, weeks=[1, 2, 3, 4])
    assert naive[1] == {"a": 6.0}
    assert naive[2] == {"a": 10.0, "b": 4.0}
    assert naive[3] == {"a": 15.0, "b": 4.0}  # b missed week 2 → still 1 game
    assert naive[4] == {"a": 20.0, "b": 6.0}


def _season():
    pool = [PoolPlayer("a", "RB", 1), PoolPlayer("b", "RB", 2), PoolPlayer("c", "RB", 3)]
    providers = {
        "sleeper": {1: {"a": 20.0, "b": 10.0, "c": 5.0}, 2: {"a": 20.0, "b": 10.0}},
        "espn": {1: {"a": 18.0, "b": 12.0, "c": 6.0}, 2: {"a": 15.0, "b": 12.0, "c": 0.0}},
    }
    actuals = {1: {"a": 25.0, "b": 8.0, "c": 6.0}, 2: {"a": 10.0, "b": 14.0}}
    return pool, providers, actuals


def test_run_season_and_aggregate():
    pool, providers, actuals = _season()
    week_rows, detail = run_season(2025, pool, providers, actuals, weeks=[1, 2])
    assert {r.week for r in week_rows} == {1, 2}
    # week 2: c projected 0 by espn and absent from sleeper → out of the pool
    assert {d.sleeper_id for d in detail if d.week == 2} == {"a", "b"}

    rolled = aggregate(week_rows, detail)
    by = {r.provider: r for r in rolled}
    assert [r.provider for r in rolled] == ["sleeper", "espn"]  # first-seen order, not alpha
    slp = by["sleeper"]
    assert (slp.weeks, slp.n_pool, slp.n) == (2, 5, 5)
    # errors: wk1 −5, +2, −1 ; wk2 +10, −4
    assert slp.mae == pytest.approx((5 + 2 + 1 + 10 + 4) / 5)
    assert slp.bias == pytest.approx((-5 + 2 - 1 + 10 - 4) / 5)
    # per-week ρ: wk1 perfect (1.0), wk2 inverted (−1.0) → mean 0, min −1
    assert slp.spearman == pytest.approx(0.0)
    assert slp.spearman_min == pytest.approx(-1.0)
    assert slp.mean_actual == pytest.approx((25 + 8 + 6 + 10 + 14) / 5)

    espn = by["espn"]
    assert espn.n == 5  # projected 0 for c in wk2 but c isn't in that week's pool
    assert espn.spearman == pytest.approx(0.0)


def test_aggregate_handles_provider_with_nothing_scored():
    pool, providers, actuals = _season()
    providers = {**providers, "naive": {}}
    week_rows, detail = run_season(2025, pool, providers, actuals, weeks=[1, 2])
    naive = next(r for r in aggregate(week_rows, detail) if r.provider == "naive")
    assert naive.n == 0 and naive.weeks == 0
    assert math.isnan(naive.mae) and math.isnan(naive.spearman)
