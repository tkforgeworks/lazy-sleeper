"""Flex-aware VORP for the draft board (LS-27).

VORP = league-scored points − the replacement baseline for the player's position. The flex
awareness lives entirely in the LS-26 baselines: flex demand raises the RB/WR/TE cutoffs (and
thus lowers nobody's baseline by accident), so no per-player adjustment happens here.

The default baseline is the *live* one — derived from the same projection table being ranked —
so a provider's systematic bias cancels: the 2025 benchmark measured projections running +45–70
points hot on QB/WR, which would inflate those positions' VORP against an actuals-derived
baseline. The 2023–25 actuals average remains available as an x-check
(``historical_baselines(...).average`` fed straight into ``vorp_board``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from lazy_sleeper.board.baselines import RosterShape, derive_baselines
from lazy_sleeper.providers.base import PlayerProjection, ProjectionProvider


@dataclass(frozen=True)
class PlayerValue:
    sleeper_id: str
    position: str
    team: str | None
    points: float
    baseline: float
    vorp: float
    pos_rank: int  # rank by points within the position (1 = best)
    components: dict[str, float]  # ensemble members' points — disagreement input for LS-29


def vorp_board(
    projections: Iterable[PlayerProjection], baselines: Mapping[str, float]
) -> list[PlayerValue]:
    """Every projected player at a baselined position, sorted by VORP descending.

    Players whose position has no baseline (or none at all) are dropped — they have no seat
    in the starting lineup, so value over replacement is undefined for them.
    """
    by_pos: dict[str, list[PlayerProjection]] = defaultdict(list)
    for p in projections:
        if p.position in baselines:
            by_pos[p.position].append(p)
    out: list[PlayerValue] = []
    for pos, players in by_pos.items():
        players.sort(key=lambda p: (-p.points, p.sleeper_id))
        base = baselines[pos]
        out.extend(
            PlayerValue(
                sleeper_id=p.sleeper_id,
                position=pos,
                team=p.team,
                points=p.points,
                baseline=base,
                vorp=p.points - base,
                pos_rank=rank,
                components=dict(p.components),
            )
            for rank, p in enumerate(players, start=1)
        )
    out.sort(key=lambda v: (-v.vorp, -v.points, v.sleeper_id))
    return out


def live_vorp(provider: ProjectionProvider, shape: RosterShape, season: int) -> list[PlayerValue]:
    """VORP against the live baseline, derived from the same projection pull it ranks.

    By construction the last starter at each position lands at VORP 0 and everyone behind the
    cutoff goes negative.
    """
    projections = provider.projections(season)
    baselines = derive_baselines([(p.sleeper_id, p.position, p.points) for p in projections], shape)
    return vorp_board(projections, {pos: b.points for pos, b in baselines.items()})
