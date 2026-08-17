"""Kicker normalization: express any source's FG line in the league's distance buckets (LS-19).

The league scores field goals by distance bucket (`fgm_0_19` … `fgm_50_59`, `fgm_60p`), misses as a
flat `fgmiss`, and XPs as `xpm` / `xpmiss`. Sources disagree on granularity:

* nflverse actuals — full splits (plus a redundant `fgm_50p`); score exactly.
* ESPN — `fgm` total + `fgm_0_39` / `fgm_40_49` / `fgm_50p`; the coarse ranges are split by the mix.
* Sleeper weekly — `fgm` total + most buckets, `fgm_50p` for the long range.
* Sleeper season — **only** `fgm_40_49` + `fgm_50p` (no total, nothing under 40 yd). The short range
  is unobserved rather than zero; with `impute_unobserved=True` (default) it is estimated from the
  observed long range using the same mix.

Accepted approximation: projected FGs are distributed across buckets using the 2023–25 league-wide
distance mix from `core.actuals` (nflverse). Kicker points are pick 14 of 15 — precision here does
not move the draft board. Every stat key is parsed as a yard interval, so the same code handles any
bucket scheme a provider invents.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from lazy_sleeper.scoring.engine import Stats

# League bucket edges (Sleeper vocabulary): (lo, hi) inclusive yards; hi=None means "and longer".
BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("0_19", 0, 19),
    ("20_29", 20, 29),
    ("30_39", 30, 39),
    ("40_49", 40, 49),
    ("50_59", 50, 59),
    ("60p", 60, None),
)
BUCKET_NAMES: tuple[str, ...] = tuple(b[0] for b in BUCKETS)

# 2023–2025 nflverse regular+post season, all kickers: made FGs by distance bucket.
# Derived from core.actuals on 2026-08-17 (`lazy score kmix`); refresh after each season.
DEFAULT_MIX_COUNTS: Mapping[str, float] = {
    "0_19": 7,
    "20_29": 668,
    "30_39": 852,
    "40_49": 713,
    "50_59": 515,
    "60p": 21,
}

_RANGE_RE = re.compile(r"^(?P<prefix>fgm|fgmiss|fga)(?:_(?P<lo>\d+)(?:_(?P<hi>\d+)|(?P<plus>p)))?$")


@dataclass(frozen=True)
class DistanceMix:
    """Share of made FGs per league bucket; sums to 1."""

    shares: Mapping[str, float]

    @classmethod
    def from_counts(cls, counts: Mapping[str, float]) -> DistanceMix:
        total = float(sum(counts.get(b, 0.0) for b in BUCKET_NAMES))
        if total <= 0:
            raise ValueError("distance mix needs positive counts")
        return cls({b: counts.get(b, 0.0) / total for b in BUCKET_NAMES})

    def mass(self, buckets: Iterable[str]) -> float:
        return sum(self.shares[b] for b in buckets)

    def spread(self, amount: float, buckets: list[str]) -> dict[str, float]:
        """Split `amount` over `buckets` proportional to their shares (equal if all zero)."""
        m = self.mass(buckets)
        if m > 0:
            return {b: amount * self.shares[b] / m for b in buckets}
        return {b: amount / len(buckets) for b in buckets}


DEFAULT_MIX = DistanceMix.from_counts(DEFAULT_MIX_COUNTS)


def _parse_range(key: str) -> tuple[str, int, int | None] | None:
    """`fgm_50p` → ('fgm', 50, None); `fgm` → ('fgm', 0, None); `fgm_0_39` → ('fgm', 0, 39)."""
    m = _RANGE_RE.match(key)
    if not m:
        return None
    if m.group("lo") is None:
        return m.group("prefix"), 0, None
    lo = int(m.group("lo"))
    hi = None if m.group("plus") else int(m.group("hi"))
    return m.group("prefix"), lo, hi


def _covers(lo: int, hi: int | None, b_lo: int, b_hi: int | None) -> bool:
    b_hi_v = math.inf if b_hi is None else b_hi
    hi_v = math.inf if hi is None else hi
    return lo <= b_lo and b_hi_v <= hi_v


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def split_buckets(
    stats: Stats, prefix: str, mix: DistanceMix, *, impute_unobserved: bool
) -> dict[str, float]:
    """Resolve every `{prefix}*` key in `stats` into per-bucket values.

    Finest ranges are applied first (exact buckets, then `_0_39`/`_50p`, then the bare total); each
    coarser range only fills the buckets it covers that are still unassigned, with its residual over
    what finer keys already accounted for. Buckets no key covers are *unobserved*: 0 unless
    `impute_unobserved`, in which case they are scaled from the observed buckets via the mix.
    """
    ranges: list[tuple[float, int, int | None, float]] = []  # (width, lo, hi, value)
    for key, raw in stats.items():
        parsed = _parse_range(key)
        if parsed is None or parsed[0] != prefix:
            continue
        v = _num(raw)
        if v is None:
            continue
        _, lo, hi = parsed
        width = math.inf if hi is None else hi - lo
        # unbounded ranges sort by how much they cover: `_50p` before `_40p` before bare total
        ranges.append((width if hi is not None else 10_000 - lo, lo, hi, v))
    ranges.sort(key=lambda r: r[0])

    assigned: dict[str, float] = {}
    covered: set[str] = set()
    for _, lo, hi, value in ranges:
        sub = [b for b, b_lo, b_hi in BUCKETS if _covers(lo, hi, b_lo, b_hi)]
        if not sub:
            continue
        covered.update(sub)
        free = [b for b in sub if b not in assigned]
        if not free:
            continue
        residual = max(value - sum(assigned.get(b, 0.0) for b in sub), 0.0)
        assigned.update(mix.spread(residual, free))

    out = {b: assigned.get(b, 0.0) for b in BUCKET_NAMES}
    unobserved = [b for b in BUCKET_NAMES if b not in covered]
    if impute_unobserved and unobserved and covered:
        seen_mass = mix.mass(covered)
        seen_total = sum(out[b] for b in covered)
        if seen_mass > 0 and seen_total > 0:
            scale = seen_total / seen_mass
            for b in unobserved:
                out[b] = scale * mix.shares[b]
    return out


@dataclass(frozen=True)
class KickerNormalizer:
    """Callable stat normalizer for K rows — plug into `Scorer(normalizers={"K": ...})`."""

    mix: DistanceMix = DEFAULT_MIX
    impute_unobserved: bool = True

    def __call__(self, stats: Stats) -> dict[str, Any]:
        out: dict[str, Any] = dict(stats)
        made = split_buckets(stats, "fgm", self.mix, impute_unobserved=self.impute_unobserved)
        for b, v in made.items():
            out[f"fgm_{b}"] = v
        out["fgm_50p"] = made["50_59"] + made["60p"]
        out["fgm"] = sum(made.values())

        # Misses: flat total in the league map. Never imputed — a miss we didn't see isn't a miss.
        if _num(stats.get("fgmiss")) is None:
            miss = split_buckets(stats, "fgmiss", self.mix, impute_unobserved=False)
            fga, fgm = _num(stats.get("fga")), _num(stats.get("fgm"))
            if fga is not None and fgm is not None:
                out["fgmiss"] = max(fga - fgm, 0.0)
            elif any(miss.values()):
                out["fgmiss"] = sum(miss.values())
        xpa, xpm, xpmiss = _num(stats.get("xpa")), _num(stats.get("xpm")), _num(stats.get("xpmiss"))
        if xpmiss is None and xpa is not None and xpm is not None:
            out["xpmiss"] = max(xpa - xpm, 0.0)
        return out
