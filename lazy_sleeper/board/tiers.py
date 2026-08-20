"""Tier boundaries and cliff flags over a VORP board (LS-28).

Method — adaptive gap-based, per position (chosen over 1-D clustering, which needs an arbitrary
tier count per position and isn't explainable mid-draft):

- Sort a position's players by points (the LS-27 ``pos_rank`` order) and look at consecutive
  drops within its draftable depth. A new tier starts where the drop is unusually large *for
  that position*: ``gap ≥ max(min_gap, gap_multiplier × median gap over the depth window)``.
  Scaling by the position's own median gap keeps one config working everywhere — QB gaps run
  much larger than TE gaps, so an absolute threshold would over-tier one and under-tier the
  other; ``min_gap`` stops micro-tiers where the field is dense and the median gap tiny.
- The **cliff** flag is independent and absolute: a player is a cliff when the drop to the next
  player at their position is at least ``cliff_gap`` season points (default 15 ≈ 1 pt/week) —
  "last chair before the music stops", tangible at the table.

Thresholds live in ``derived.board_config`` (see ``board/config.py``) so the front end can turn
the dial mid-draft; ``TierConfig``'s defaults only apply when no row exists. Depth per position
reuses the benchmark's draft-relevant pool sizes and stays in code.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from statistics import median

from lazy_sleeper.benchmark.season import DEFAULT_POOL_SIZES
from lazy_sleeper.board.vorp import PlayerValue


@dataclass(frozen=True)
class TierConfig:
    cliff_gap: float = 15.0  # absolute season-points drop to the next player → cliff
    gap_multiplier: float = 2.0  # tier break at this multiple of the position's median gap
    min_gap: float = 4.0  # ...but never on a drop smaller than this
    depth: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_POOL_SIZES))
    # LS-29 market/disagreement flags (see board/flags.py); stored in derived.board_config too.
    adp_min_delta: float = 12.0  # picks: |ADP − board rank| floor for a value/reach flag
    adp_pct: float = 0.25  # ...or this fraction of the ADP, whichever is larger
    disagree_min_pts: float = 20.0  # season pts: member spread floor for a disagreement flag
    disagree_pct: float = 0.15  # ...or this fraction of the blended points, whichever is larger
    debias_disagreement: bool = True  # remove each member's position-level bias before comparing


@dataclass(frozen=True)
class BoardRow:
    value: PlayerValue
    tier: int | None  # None past the position's tiered depth
    cliff: bool
    gap_to_next: float | None  # points down to the next player at the position (None = last)
    # LS-29 — filled by board/flags.py; None/False until those passes run.
    adp: float | None = None  # Sleeper adp_ppr (overall pick)
    adp_delta: float | None = None  # adp − board rank: + = market drafts him later than we rank
    adp_flag: str | None = None  # "value" | "reach" | None
    spread: float | None = None  # |sleeper − espn| league-scored points (debiased if configured)
    disagree: bool = False


def assign_tiers(values: Sequence[PlayerValue], config: TierConfig | None = None) -> list[BoardRow]:
    """Tier + cliff for every board row, preserving the input (VORP-sorted) order."""
    config = config or TierConfig()
    by_pos: dict[str, list[PlayerValue]] = defaultdict(list)
    for v in values:
        by_pos[v.position].append(v)

    annotated: dict[tuple[str, int], tuple[int | None, bool, float | None]] = {}
    for pos, players in by_pos.items():
        players.sort(key=lambda v: v.pos_rank)
        depth = config.depth.get(pos, 0)
        gaps = [players[i].points - players[i + 1].points for i in range(len(players) - 1)]
        window = gaps[: max(depth - 1, 0)]
        threshold = max(config.min_gap, config.gap_multiplier * median(window)) if window else None
        tier = 1
        for i, v in enumerate(players):
            gap = gaps[i] if i < len(gaps) else None
            if 0 < i < depth and threshold is not None and gaps[i - 1] >= threshold:
                tier += 1
            annotated[(pos, v.pos_rank)] = (
                tier if i < depth else None,
                gap is not None and gap >= config.cliff_gap,
                gap,
            )

    out: list[BoardRow] = []
    for v in values:
        tier, cliff, gap = annotated[(v.position, v.pos_rank)]
        out.append(BoardRow(value=v, tier=tier, cliff=cliff, gap_to_next=gap))
    return out
