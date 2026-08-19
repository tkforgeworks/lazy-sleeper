"""CSV / terminal rendering shared by the benchmark commands."""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable, Sequence
from dataclasses import fields
from pathlib import Path
from typing import Any

from lazy_sleeper.benchmark.season import PlayerRow


def cell(v: Any, digits: int = 4) -> Any:
    """CSV cell: floats fixed-precision, NaN → empty, everything else as-is."""
    if isinstance(v, float):
        return "" if math.isnan(v) else f"{v:.{digits}f}"
    return "" if v is None else v


def write_rows(path: Path, rows: Sequence[Any]) -> None:
    """Write a sequence of same-typed dataclass rows to CSV (header = field names)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        if not rows:
            return
        names = [f.name for f in fields(rows[0])]
        w.writerow(names)
        for r in rows:
            w.writerow([cell(getattr(r, n)) for n in names])


def write_players(path: Path, detail: Iterable[PlayerRow]) -> None:
    """Per-player detail: one column per provider (empty = not projected)."""
    detail = list(detail)
    providers = sorted({k for d in detail for k in d.projected})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["season", "week", "position", "sleeper_id", "adp", "actual", *providers])
        for d in detail:
            w.writerow(
                [
                    d.season,
                    cell(d.week),
                    d.position,
                    d.sleeper_id,
                    d.adp,
                    cell(d.actual, 2),
                    *(cell(d.projected.get(p), 2) for p in providers),
                ]
            )


def fmt(v: float, width: int, digits: int = 2) -> str:
    """Fixed-width terminal number; NaN renders as '-'."""
    return f"{'-':>{width}}" if math.isnan(v) else f"{v:>{width}.{digits}f}"
