import math

import pytest

from lazy_sleeper.metrics import bias, mae, rmse, spearman
from lazy_sleeper.metrics.accuracy import average_ranks


def test_mae_bias_rmse_hand_values():
    pred, act = [10.0, 20.0, 30.0], [12.0, 18.0, 36.0]
    assert mae(pred, act) == pytest.approx(10 / 3)
    assert bias(pred, act) == pytest.approx(-6 / 3)  # projects low on average
    assert rmse(pred, act) == pytest.approx(math.sqrt((4 + 4 + 36) / 3))


def test_average_ranks_ties():
    assert average_ranks([10, 30, 20, 20]) == [1.0, 4.0, 2.5, 2.5]


def test_spearman_perfect_and_inverse():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_known_value():
    # Ranks: pred 1..5, actual [1, 3, 2, 5, 4] → d² = 0+1+1+1+1 = 4 → 1 − 6·4/(5·24) = 0.8
    assert spearman([1, 2, 3, 4, 5], [1, 3, 2, 5, 4]) == pytest.approx(0.8)


def test_degenerate_inputs_are_nan_not_errors():
    assert math.isnan(mae([], []))
    assert math.isnan(spearman([1.0], [2.0]))
    assert math.isnan(spearman([1, 1, 1], [1, 2, 3]))  # zero variance in one side


def test_length_mismatch_raises():
    with pytest.raises(ValueError):
        mae([1.0], [1.0, 2.0])
