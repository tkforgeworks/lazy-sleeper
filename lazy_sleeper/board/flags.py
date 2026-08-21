"""Market and disagreement flags over a tiered board (LS-29).

Two signals that live *beside* VORP rather than inside it:

- **ADP delta** — the board's overall rank (1 = top VORP) vs Sleeper ``adp_ppr`` (the mock-draft
  market). ``adp_delta = adp − rank``: positive means the room lets him fall past where we rank
  him (**value**), negative means taking him at his ADP is a **reach** by our numbers. The flag
  threshold scales with draft position — ``|delta| ≥ max(adp_min_delta, adp_pct × adp)`` — because
  a 10-pick gap at pick 6 is a real signal while at pick 150 it's mock-draft noise.
- **Provider disagreement** — the spread between the ensemble members' league-scored points on
  the same player (``components`` on each row). Flag at ``spread ≥ max(disagree_min_pts,
  disagree_pct × blended points)``. Sleeper and ESPN carry *systematic* position-level offsets
  (Sleeper DEF runs ~20% under ESPN for lack of points-allowed data; 2025 showed QB bias too), so
  by default each member is first rescaled by its position-median ratio to the blend
  (``debias_disagreement``) and only disagreement *relative to the position* flags. Turn that off
  to see raw spreads.

Both passes are pure functions over ``BoardRow`` lists and preserve order; thresholds ride on
``TierConfig`` and therefore on ``derived.board_config``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lazy_sleeper.board.baselines import RosterShape, derive_baselines
from lazy_sleeper.board.tiers import BoardRow, TierConfig, assign_tiers
from lazy_sleeper.board.vorp import vorp_board
from lazy_sleeper.providers.base import ProjectionProvider

STREAM_POSITIONS = ("K", "DEF")  # replaceable from waivers: baseline at config.stream_depth
DEFAULT_MEMBERS = ("sleeper", "espn")


def flag_adp(
    rows: Sequence[BoardRow], adp_by_id: Mapping[str, float], config: TierConfig | None = None
) -> list[BoardRow]:
    """Attach ADP, delta and value/reach flag.

    ``rows`` must be the full VORP-ordered board — the board rank is the row's 1-based position,
    so filter by position *after* flagging.
    """
    config = config or TierConfig()
    out: list[BoardRow] = []
    for rank, row in enumerate(rows, start=1):
        adp = adp_by_id.get(row.value.sleeper_id)
        if adp is None:
            out.append(replace(row, adp=None, adp_delta=None, adp_flag=None))
            continue
        delta = adp - rank
        threshold = max(config.adp_min_delta, config.adp_pct * adp)
        flag = None
        if delta >= threshold:
            flag = "value"
        elif -delta >= threshold:
            flag = "reach"
        out.append(replace(row, adp=adp, adp_delta=delta, adp_flag=flag))
    return out


def position_bias(
    rows: Iterable[BoardRow], members: Sequence[str] = DEFAULT_MEMBERS
) -> dict[tuple[str, str], float]:
    """Median ratio of each member's points to the blend, per (position, member).

    Only players every member projected count (so rookie fallbacks don't skew it); a position
    with no such player gets no entry, i.e. ratio 1.0 / no adjustment.
    """
    ratios: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        comps = row.value.components
        if row.value.points <= 0 or not all(m in comps for m in members):
            continue
        for m in members:
            ratios[(row.value.position, m)].append(comps[m] / row.value.points)
    return {key: median(vals) for key, vals in ratios.items() if vals}


def flag_disagreement(
    rows: Sequence[BoardRow],
    config: TierConfig | None = None,
    members: Sequence[str] = DEFAULT_MEMBERS,
) -> list[BoardRow]:
    """Attach the member spread and disagreement flag; rows lacking a member stay unflagged."""
    config = config or TierConfig()
    bias = position_bias(rows, members) if config.debias_disagreement else {}
    out: list[BoardRow] = []
    for row in rows:
        comps = row.value.components
        if not all(m in comps for m in members):
            out.append(replace(row, spread=None, disagree=False))
            continue
        adjusted = [comps[m] / (bias.get((row.value.position, m)) or 1.0) for m in members]
        spread = max(adjusted) - min(adjusted)
        threshold = max(config.disagree_min_pts, config.disagree_pct * row.value.points)
        out.append(replace(row, spread=spread, disagree=spread >= threshold))
    return out


def latest_adp(session: Session, season: int, field: str = "adp_ppr") -> dict[str, float]:
    """Sleeper ADP (overall pick) per player from the newest ``core.adp`` snapshot for a season."""
    from lazy_sleeper.db.models import Adp

    snap_id = session.scalar(select(func.max(Adp.snapshot_id)).where(Adp.season == season))
    if snap_id is None:
        return {}
    col = getattr(Adp, field)
    rows = session.execute(
        select(Adp.sleeper_id, col).where(Adp.snapshot_id == snap_id, col.is_not(None))
    )
    return {sid: float(adp) for sid, adp in rows}


def build_board(
    provider: ProjectionProvider,
    shape: RosterShape,
    season: int,
    config: TierConfig | None = None,
    adp_by_id: Mapping[str, float] | None = None,
    baselines: Mapping[str, float] | None = None,
) -> list[BoardRow]:
    """The full board in one call: VORP → tiers/cliffs → ADP flags → disagreement flags.

    ``baselines`` defaults to the live baseline derived from the same projections (LS-27).
    """
    config = config or TierConfig()
    projections = provider.projections(season)
    if baselines is None:
        live = derive_baselines(
            [(p.sleeper_id, p.position, p.points) for p in projections],
            shape,
            stream_depth=(
                dict.fromkeys(STREAM_POSITIONS, config.stream_depth)
                if config.stream_depth
                else None
            ),
        )
        baselines = {pos: b.points for pos, b in live.items()}
    rows = assign_tiers(vorp_board(projections, baselines), config)
    rows = flag_adp(rows, adp_by_id or {}, config)
    return flag_disagreement(rows, config)
