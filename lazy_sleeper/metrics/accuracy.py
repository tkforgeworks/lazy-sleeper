"""Paired-error metrics: predicted vs actual over the same players.

All functions take two equal-length sequences and return NaN for degenerate inputs (empty, or
fewer than two pairs / zero variance for the rank correlation) rather than raising, so callers can
tabulate sparse positions without special-casing.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def _check(pred: Sequence[float], actual: Sequence[float]) -> None:
    if len(pred) != len(actual):
        raise ValueError(f"length mismatch: {len(pred)} predictions vs {len(actual)} actuals")


def mae(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Mean absolute error."""
    _check(pred, actual)
    if not pred:
        return math.nan
    return sum(abs(p - a) for p, a in zip(pred, actual, strict=True)) / len(pred)


def bias(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Mean signed error (pred − actual): positive means the provider projects too high."""
    _check(pred, actual)
    if not pred:
        return math.nan
    return sum(p - a for p, a in zip(pred, actual, strict=True)) / len(pred)


def rmse(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Root mean squared error."""
    _check(pred, actual)
    if not pred:
        return math.nan
    return math.sqrt(sum((p - a) ** 2 for p, a in zip(pred, actual, strict=True)) / len(pred))


def average_ranks(values: Sequence[float]) -> list[float]:
    """1-based ranks with ties averaged (the convention scipy.stats.rankdata uses by default)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(pred: Sequence[float], actual: Sequence[float]) -> float:
    """Spearman rank correlation = Pearson correlation of the average ranks."""
    _check(pred, actual)
    n = len(pred)
    if n < 2:
        return math.nan
    rp, ra = average_ranks(pred), average_ranks(actual)
    mp, ma = sum(rp) / n, sum(ra) / n
    cov = sum((x - mp) * (y - ma) for x, y in zip(rp, ra, strict=True))
    vp = sum((x - mp) ** 2 for x in rp)
    va = sum((y - ma) ** 2 for y in ra)
    if vp == 0 or va == 0:
        return math.nan
    return cov / math.sqrt(vp * va)
