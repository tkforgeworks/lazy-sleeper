"""Draft-time signals (LS-33): survival to my next pick, position runs, and a pick-now score.

All pure; the inputs are the pre-draft board (``BoardRow`` list from ``build_board``), the live
``DraftState`` (LS-32), market data (Sleeper ``adp_ppr`` + ``search_rank``) and the dials on
``TierConfig`` (stored in ``derived.board_config``).

**Survival** — P(player still available at my next pick). Market prior: a player is taken around
his ADP with scatter growing later in the draft, so ``P(available at pick n) = 1 − Φ((n − ½ − adp)
/ σ)`` with ``σ = max(survival_sigma_min, survival_sigma_pct × adp)``. Demand adjustment: the
window to my next pick is stretched for positions the teams ahead of me need more than average —
``n_eff = now + (n − now) × (1 + demand_shift × (need[pos] / mean need − 1))``. No ADP (rookies,
the unsigned tail at 999) → a pseudo-ADP from ``search_rank`` via a monotone lookup fitted on the
players that have both; neither → ``None`` (never silently 1.0).

**Runs** — over the last ``run_window`` picks, a position is "on a run" when ``count ≥
run_threshold`` or the most recent ``streak ≥ run_streak`` picks are all that position. Both
dials ship; which one matters is a draft-night config call.

**Pick score** — the recommendation. Taking ``i`` now is worth his VORP plus whatever I can
still get at my next pick *without* him: ``vorp_i + E[best VORP available at my next pick
excluding i]``. Since the second term is the same for everyone except for ``i``'s own
contribution, this is ``pick_score = vorp_i − option_value_i + need_bonus × my_need[P]`` where
``option_value_i = E[best] − E[best without i]`` is how much ``i`` is worth as a "take him next
time" fallback. The expectation is the survival-ordered chain over all available players (best
survives with s₁; else the next with s₂(1−s₁); …), across positions — one pick, best option.
A stud who will surely be there next turn has option value ≈ his VORP and scores ≈ 0: take
someone who won't be. A player with 20 % survival keeps ≈ 80 % of his VORP. Sorting by
``pick_score`` is "who should I take now".
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from statistics import median

from lazy_sleeper.board.tiers import BoardRow, TierConfig
from lazy_sleeper.draft.state import DraftState

ADP_UNDRAFTED = 999.0  # Sleeper's sentinel for "nobody drafts him"


# --- survival ----------------------------------------------------------------------


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def survival(adp: float, pick_no: float, cfg: TierConfig) -> float:
    """P(a player with this ADP is still on the board when ``pick_no`` comes up)."""
    sigma = max(cfg.survival_sigma_min, cfg.survival_sigma_pct * adp)
    return 1.0 - _phi((pick_no - 0.5 - adp) / sigma)


class SearchRankAdp:
    """Monotone search_rank → ADP lookup fitted on players with both (pseudo-ADP fallback)."""

    def __init__(self, pairs: Iterable[tuple[int, float]]) -> None:
        by_rank: dict[int, list[float]] = {}
        for rank, adp in pairs:
            if adp is not None and adp < ADP_UNDRAFTED and rank is not None:
                by_rank.setdefault(int(rank), []).append(float(adp))
        ranks = sorted(by_rank)
        adps: list[float] = []
        running = 0.0
        for r in ranks:  # cumulative max keeps it monotone through the noisy tail
            running = max(running, median(by_rank[r]))
            adps.append(running)
        self._ranks, self._adps = ranks, adps

    def __bool__(self) -> bool:
        return bool(self._ranks)

    def adp_for(self, search_rank: int | None) -> float | None:
        if search_rank is None or not self._ranks:
            return None
        i = bisect_left(self._ranks, search_rank)
        if i >= len(self._ranks):
            return None  # beyond every fitted rank: treat as undrafted
        if i == 0 or self._ranks[i] == search_rank:
            return self._adps[i]
        r0, r1 = self._ranks[i - 1], self._ranks[i]
        a0, a1 = self._adps[i - 1], self._adps[i]
        return a0 + (a1 - a0) * (search_rank - r0) / (r1 - r0)


def effective_adp(
    sleeper_id: str,
    adp_by_id: Mapping[str, float],
    search_rank_by_id: Mapping[str, int] | None,
    rank_map: SearchRankAdp | None,
) -> float | None:
    adp = adp_by_id.get(sleeper_id)
    if adp is not None and adp < ADP_UNDRAFTED:
        return adp
    if rank_map and search_rank_by_id:
        return rank_map.adp_for(search_rank_by_id.get(sleeper_id))
    return None


def demand_stretch(position: str, window_needs: Mapping[str, float], cfg: TierConfig) -> float:
    """Multiplier on the picks-until-my-turn for this position (1.0 = average demand)."""
    if not window_needs:
        return 1.0
    mean = sum(window_needs.values()) / len(window_needs)
    if mean <= 0:
        return 1.0
    rel = window_needs.get(position, 0.0) / mean - 1.0
    return max(0.0, 1.0 + cfg.demand_shift * rel)


# --- runs ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSignal:
    position: str
    count: int  # picks at this position in the window
    streak: int  # consecutive most-recent picks at this position
    run: bool


def detect_runs(recent_positions: Sequence[str | None], cfg: TierConfig) -> dict[str, RunSignal]:
    """``recent_positions`` = positions of the last picks, most recent **last**."""
    window = [p for p in recent_positions[-cfg.run_window :] if p]
    counts = Counter(window)
    streak_pos, streak = None, 0
    for p in reversed(window):
        if streak_pos is None:
            streak_pos = p
        if p != streak_pos:
            break
        streak += 1
    out: dict[str, RunSignal] = {}
    for pos, n in counts.items():
        s = streak if pos == streak_pos else 0
        out[pos] = RunSignal(pos, n, s, n >= cfg.run_threshold or s >= cfg.run_streak)
    return out


# --- the recommendation --------------------------------------------------------------


def expected_best_available(vorps_and_survival: Iterable[tuple[float, float]]) -> float:
    """E[best VORP still there] over a position's players, best first: Σ v_j s_j Π_{i<j}(1−s_i)."""
    expected, none_yet = 0.0, 1.0
    for v, s in sorted(vorps_and_survival, key=lambda t: -t[0]):
        expected += v * s * none_yet
        none_yet *= 1.0 - s
    return expected


def advise(
    rows: Sequence[BoardRow],
    state: DraftState,
    adp_by_id: Mapping[str, float],
    cfg: TierConfig,
    *,
    search_rank_by_id: Mapping[str, int] | None = None,
    rank_map: SearchRankAdp | None = None,
    horizon: int | None = None,
) -> list[BoardRow]:
    """Available board rows with survival / run / pick_score attached, best pick first.

    ``horizon`` = picks until my next turn to assume when my slot is unknown (default: one full
    round, i.e. ``teams``); ignored when the state knows my next pick.
    """
    taken = state.taken()
    now = state.current_pick
    on_clock = state.my_slot is not None and state.on_the_clock == state.my_slot
    # "next pick" = the one after the pick I'm deciding now; survival is about waiting.
    later = (
        [p for p in state.spec.picks_for_slot(state.my_slot) if p > now] if state.my_slot else []
    )
    if state.my_slot is None:
        nxt = now + (horizon if horizon is not None else state.spec.teams)
    elif later:
        nxt = later[0]
    else:
        nxt = state.spec.total_picks + 1  # no pick left for me: survival to the end of the draft
    window_needs: dict[str, float] = {}
    if state.my_slot is not None:
        acc: Counter[str] = Counter()
        for p in range(now + 1 if on_clock else now, min(nxt, state.spec.total_picks + 1)):
            acc.update(state.roster(state.spec.slot_for_pick(p)).needs())
        window_needs = dict(acc)
    my_needs = state.my_needs() or {}
    recent = [p for _, _, p in (state.pick_at(n) for n in range(max(1, now - cfg.run_window), now))]
    runs = detect_runs(recent, cfg)

    # survival per available player
    available = [r for r in rows if r.value.sleeper_id not in taken]
    surv: dict[str, float | None] = {}
    for r in available:
        adp = effective_adp(r.value.sleeper_id, adp_by_id, search_rank_by_id, rank_map)
        if adp is None:
            surv[r.value.sleeper_id] = None
            continue
        n_eff = now + (nxt - now) * demand_stretch(r.value.position, window_needs, cfg)
        surv[r.value.sleeper_id] = survival(adp, n_eff, cfg)

    # option value: E[best at my next pick] - E[best without him]. Unknown survival -> 1.0 (the
    # market doesn't draft him, so he'll be there). Chain is over all positions: one pick.
    vs = [
        (
            r.value.sleeper_id,
            r.value.vorp,
            s if (s := surv[r.value.sleeper_id]) is not None else 1.0,
        )
        for r in available
    ]
    vs.sort(key=lambda t: -t[1])
    e_all = expected_best_available((v, s) for _, v, s in vs)
    option: dict[str, float] = {}
    for sid, _, _ in vs:
        option[sid] = e_all - expected_best_available((v, s) for i, v, s in vs if i != sid)

    out: list[BoardRow] = []
    for r in available:
        pos = r.value.position
        run = runs.get(pos)
        score = r.value.vorp - option[r.value.sleeper_id] + cfg.need_bonus * my_needs.get(pos, 0.0)
        out.append(
            replace(
                r,
                survival=surv[r.value.sleeper_id],
                run=bool(run and run.run),
                run_count=run.count if run else 0,
                pick_score=round(score, 2),
            )
        )
    out.sort(key=lambda r: (-(r.pick_score or 0.0), -r.value.vorp))
    return out


__all__ = [
    "ADP_UNDRAFTED",
    "RunSignal",
    "SearchRankAdp",
    "advise",
    "demand_stretch",
    "detect_runs",
    "effective_adp",
    "expected_best_available",
    "survival",
]
