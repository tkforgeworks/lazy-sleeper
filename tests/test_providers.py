"""Providers (LS-25): inverse-MAE weight fitting, normalization, and the ensemble blend."""

from datetime import UTC, datetime

import pytest

from lazy_sleeper.providers import (
    SEASON,
    WEEKLY,
    EnsembleProvider,
    PlayerProjection,
    StaticWeights,
    fit_weights,
    normalize,
    to_json,
)
from lazy_sleeper.providers.weights import pooled_mae


def test_normalize_drops_nonpositive_and_sums_to_one():
    assert normalize({"a": 3, "b": 1, "c": 0, "d": -2}) == {"a": 0.75, "b": 0.25}
    assert normalize({"a": 0}) == {}


def test_pooled_mae_is_n_weighted_and_skips_empty():
    rows = [
        {"position": "RB", "provider": "sleeper", "mae": "10", "n": "10"},
        {"position": "RB", "provider": "sleeper", "mae": "20", "n": "30"},
        {"position": "RB", "provider": "espn", "mae": "", "n": "0"},  # no data
        {"position": "DEF", "provider": "naive", "mae": "", "n": "0"},
    ]
    assert pooled_mae(rows) == {("RB", "sleeper"): (17.5, 40)}


def test_fit_weights_inverse_mae_and_ignores_naive():
    season = [
        {"position": "RB", "provider": "sleeper", "mae": 50.0, "n": 60},
        {"position": "RB", "provider": "espn", "mae": 100.0, "n": 60},
        {"position": "RB", "provider": "naive", "mae": 10.0, "n": 60},  # not a member
        {"position": "K", "provider": "sleeper", "mae": 30.0, "n": 20},  # espn missing → 100%
    ]
    weekly = [
        {"position": "RB", "provider": "sleeper", "mae": 5.0, "n": 800},
        {"position": "RB", "provider": "espn", "mae": 5.0, "n": 800},
    ]
    fitted = {
        (f.horizon, f.position, f.provider): f
        for f in fit_weights({SEASON: season, WEEKLY: weekly})
    }
    assert fitted[(SEASON, "RB", "sleeper")].weight == pytest.approx(2 / 3)
    assert fitted[(SEASON, "RB", "espn")].weight == pytest.approx(1 / 3)
    assert (SEASON, "RB", "naive") not in fitted
    assert fitted[(SEASON, "K", "sleeper")].weight == pytest.approx(1.0)
    assert fitted[(WEEKLY, "RB", "sleeper")].weight == pytest.approx(0.5)
    assert (
        fitted[(SEASON, "RB", "sleeper")].mae == 50.0 and fitted[(SEASON, "RB", "sleeper")].n == 60
    )

    js = to_json(list(fitted.values()), version=3, fitted_at=datetime(2026, 8, 19, tzinfo=UTC))
    assert js["version"] == 3
    assert js["weights"][SEASON]["RB"]["espn"] == {
        "weight": pytest.approx(1 / 3),
        "mae": 100.0,
        "n": 60,
    }


class _Fake:
    def __init__(self, name: str, rows: dict[int | None, list[PlayerProjection]]) -> None:
        self._name, self._rows = name, rows

    @property
    def name(self) -> str:
        return self._name

    def projections(self, season: int, week: int | None = None) -> list[PlayerProjection]:
        return self._rows.get(week, [])


def _p(sid: str, pos: str, pts: float, src: str) -> PlayerProjection:
    return PlayerProjection(sid, pos, "T", pts, src)


def test_ensemble_blends_by_position_and_falls_back_for_single_source_players():
    sleeper = _Fake(
        "sleeper", {None: [_p("a", "RB", 100, "sleeper"), _p("rookie", "WR", 80, "sleeper")]}
    )
    espn = _Fake("espn", {None: [_p("a", "RB", 200, "espn"), _p("b", "TE", 50, "espn")]})
    weights = StaticWeights({SEASON: {"RB": {"sleeper": 0.75, "espn": 0.25}}})
    ens = EnsembleProvider([sleeper, espn], weights)
    out = {p.sleeper_id: p for p in ens.projections(2026)}

    assert out["a"].points == pytest.approx(125.0)  # 0.75·100 + 0.25·200
    assert out["a"].components == {"sleeper": 100, "espn": 200}
    assert out["a"].source == "ensemble"
    assert out["rookie"].points == 80  # only sleeper has him → sleeper at 100 %
    assert out["b"].points == 50  # TE has no weights → equal split over the one member present


def test_ensemble_equal_split_without_weights_and_weekly_horizon_lookup():
    sleeper = _Fake("sleeper", {3: [_p("a", "RB", 10, "sleeper")]})
    espn = _Fake("espn", {3: [_p("a", "RB", 20, "espn")]})
    weights = StaticWeights({WEEKLY: {"RB": {"sleeper": 1.0, "espn": 0.0}}})
    ens = EnsembleProvider([sleeper, espn], weights)
    assert ens.projections(2025, week=3)[0].points == pytest.approx(10.0)  # weekly weights used
    assert ens.projections(2025)[0:0] == []  # nothing for season horizon
    no_weights = EnsembleProvider([sleeper, espn], StaticWeights({}))
    assert no_weights.projections(2025, week=3)[0].points == pytest.approx(15.0)


def test_ensemble_requires_members():
    with pytest.raises(ValueError):
        EnsembleProvider([], StaticWeights({}))
