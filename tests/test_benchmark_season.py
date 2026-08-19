"""Season scoreboard (LS-23) — pool selection and metric assembly over plain mappings."""

import math

import pytest

from lazy_sleeper.benchmark import PoolPlayer, SeasonInputs, scoreboard, select_pool


def test_select_pool_top_n_by_adp_per_position_within_max_adp():
    rows = [
        ("rb1", "RB", 3.0),
        ("rb2", "RB", 1.0),
        ("rb3", "RB", 2.0),
        ("rb_deep", "RB", 350.0),  # beyond max_adp
        ("wr1", "WR", 5.0),
        ("k_noadp", "K", None),
        ("fb1", "FB", 200.0),  # position not in sizes
    ]
    pool = select_pool(rows, sizes={"RB": 2, "WR": 5, "K": 3}, max_adp=300.0)
    assert [(p.sleeper_id, p.position) for p in pool] == [
        ("rb2", "RB"),
        ("rb3", "RB"),
        ("wr1", "WR"),
    ]
    assert pool[0].adp == 1.0


def _inputs() -> SeasonInputs:
    pool = [
        PoolPlayer("a", "RB", 1.0),
        PoolPlayer("b", "RB", 2.0),
        PoolPlayer("c", "RB", 3.0),
        PoolPlayer("bust", "RB", 4.0),  # no actual rows → 0 points
        PoolPlayer("t", "TE", 10.0),
    ]
    providers = {
        "sleeper": {"a": 300.0, "b": 200.0, "c": 100.0, "bust": 150.0, "t": 120.0},
        "espn": {"a": 250.0, "b": 210.0, "c": 90.0},  # skipped bust and the TE
        "naive": {},
    }
    actuals = {"a": 280.0, "b": 220.0, "c": 110.0, "t": 100.0, "not_in_pool": 999.0}
    return SeasonInputs(2025, pool, providers, actuals)


def test_scoreboard_rows_and_metrics():
    rows, detail = scoreboard(_inputs())
    by = {(r.position, r.provider): r for r in rows}

    slp = by[("RB", "sleeper")]
    assert (slp.n_pool, slp.n) == (4, 4)
    # errors: +20, −20, −10, +150 (bust scored 0)
    assert slp.mae == pytest.approx((20 + 20 + 10 + 150) / 4)
    assert slp.bias == pytest.approx((20 - 20 - 10 + 150) / 4)
    assert slp.mean_actual == pytest.approx((280 + 220 + 110 + 0) / 4)
    # sleeper ranks bust above c; actual ranks c above bust → not perfect
    assert slp.spearman == pytest.approx(0.8)

    espn = by[("RB", "espn")]
    assert (espn.n_pool, espn.n) == (4, 3)  # bust not projected → excluded from n, not scored 0
    assert espn.spearman == pytest.approx(1.0)
    assert espn.mae == pytest.approx((30 + 10 + 20) / 3)

    naive = by[("RB", "naive")]
    assert naive.n == 0 and math.isnan(naive.mae) and math.isnan(naive.spearman)

    assert ("TE", "sleeper") in by and by[("TE", "sleeper")].n == 1
    assert ("TE", "espn") in by and by[("TE", "espn")].n == 0
    assert not any(r.position == "QB" for r in rows)  # empty positions are omitted

    bust = next(d for d in detail if d.sleeper_id == "bust")
    assert bust.actual == 0.0 and bust.projected == {"sleeper": 150.0, "espn": None, "naive": None}
    assert not any(d.sleeper_id == "not_in_pool" for d in detail)


def test_scoreboard_row_order_follows_position_order():
    rows, _ = scoreboard(_inputs())
    assert [r.position for r in rows] == ["RB"] * 3 + ["TE"] * 3
