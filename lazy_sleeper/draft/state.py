"""Draft-state model (LS-32): per-team rosters, open seats, positional needs, pick order.

Pure and in-memory — fed by ``PickEvent``s from the poller (``apply``) or rebuilt from
``core.draft_picks`` on startup (``rebuild``). No DB access. Everything the survival model
(LS-33) and the recompute loop (LS-34) ask about the draft lives here:

* **Order** — snake/linear pick ↔ slot math, who's on the clock, my next pick, the window of
  opponents' picks before it. ``my_slot`` may be ``None`` (Sleeper assigns ``draft_order`` late —
  mid-draft on the 2026-08-21 mock); every "my …" query then returns ``None`` and the rest works.
* **Rosters** — each pick is seated greedily in pick order: dedicated seat for its position if
  open → an eligible flex seat → bench. That is what a drafter experiences (a 3rd RB is the FLEX,
  a 4th is bench), and it makes "open starters" unambiguous.
* **Needs** — two views. ``open_starters``/``open_flex``/``open_bench`` are raw counts (unambiguous,
  for the UI). ``needs(weights)`` is a per-position score: open dedicated seat × ``starter`` +
  each open flex seat's weight split across its eligible positions + open bench × ``bench`` split
  by ``bench_mix``. The weights are a ``NeedWeights`` so LS-33 can tune them; the defaults are a
  reasonable prior, not a fitted model.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from lazy_sleeper.board.baselines import RosterShape
from lazy_sleeper.draft.poller import PickEvent
from lazy_sleeper.scoring.league import ScoringRules

POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K", "DEF")
BENCH = "BN"
FLEX = "FLEX"


# --- spec --------------------------------------------------------------------


@dataclass(frozen=True)
class DraftSpec:
    """Static facts: how many seats, who may sit where, how the order runs."""

    teams: int
    rounds: int
    shape: RosterShape
    bench: int
    type: str = "snake"  # snake | linear

    @classmethod
    def build(cls, rules: ScoringRules, draft: Mapping[str, Any] | None = None) -> DraftSpec:
        """From league rules (+ the parsed ``core.drafts`` row / ``/draft`` doc when available)."""
        draft = draft or {}
        shape = RosterShape.from_rules(rules)
        teams = int(draft.get("teams") or shape.teams)
        bench = rules.roster_positions.count(BENCH)
        rounds = int(draft.get("rounds") or (len(rules.roster_positions)))
        return cls(
            teams=teams,
            rounds=rounds,
            shape=RosterShape(teams=teams, dedicated=shape.dedicated, flex=shape.flex),
            bench=bench,
            type=str(draft.get("type") or "snake"),
        )

    @property
    def total_picks(self) -> int:
        return self.teams * self.rounds

    # -- order math --------------------------------------------------------------
    def round_of(self, pick_no: int) -> int:
        return (pick_no - 1) // self.teams + 1

    def slot_for_pick(self, pick_no: int) -> int:
        i = (pick_no - 1) % self.teams
        if self.type == "snake" and self.round_of(pick_no) % 2 == 0:
            return self.teams - i
        return i + 1

    def pick_for(self, slot: int, round_no: int) -> int:
        base = (round_no - 1) * self.teams
        if self.type == "snake" and round_no % 2 == 0:
            return base + (self.teams - slot) + 1
        return base + slot

    def picks_for_slot(self, slot: int) -> list[int]:
        return [self.pick_for(slot, r) for r in range(1, self.rounds + 1)]


# --- needs ---------------------------------------------------------------------


@dataclass(frozen=True)
class NeedWeights:
    """How much an open seat of each kind "pulls" toward a position. Tunable by LS-33."""

    starter: float = 1.0
    flex: float = 0.5
    # an open K/DEF seat is not an open RB1 seat: waivers refill it any week
    starter_by_position: Mapping[str, float] = field(
        default_factory=lambda: {"K": 0.25, "DEF": 0.25}
    )
    bench: float = 0.25
    bench_mix: Mapping[str, float] = field(
        default_factory=lambda: {"RB": 0.4, "WR": 0.4, "TE": 0.1, "QB": 0.1}
    )


DEFAULT_WEIGHTS = NeedWeights()


# --- rosters -------------------------------------------------------------------


@dataclass(frozen=True)
class Seated:
    pick_no: int
    sleeper_id: str | None
    position: str | None
    seat: str  # "QB".."DEF" (dedicated) | "FLEX" | "BN"


class TeamRoster:
    """One seat's roster, seats filled greedily in pick order."""

    def __init__(self, slot: int, spec: DraftSpec) -> None:
        self.slot = slot
        self._spec = spec
        self.picks: list[Seated] = []
        self._open_dedicated: Counter[str] = Counter(spec.shape.dedicated)
        self._open_flex: list[tuple[str, ...]] = list(spec.shape.flex)
        self._bench_used = 0

    def add(self, pick_no: int, sleeper_id: str | None, position: str | None) -> Seated:
        seat = BENCH
        if position and self._open_dedicated.get(position, 0) > 0:
            self._open_dedicated[position] -= 1
            seat = position
        elif position:
            for i, elig in enumerate(self._open_flex):
                if position in elig:
                    del self._open_flex[i]
                    seat = FLEX
                    break
        if seat == BENCH:
            self._bench_used += 1
        s = Seated(pick_no, sleeper_id, position, seat)
        self.picks.append(s)
        return s

    # -- counts ------------------------------------------------------------------
    @property
    def counts(self) -> Counter[str]:
        """Players per position, regardless of seat."""
        return Counter(p.position for p in self.picks if p.position)

    @property
    def open_starters(self) -> dict[str, int]:
        return {pos: n for pos, n in self._open_dedicated.items() if n > 0}

    @property
    def open_flex(self) -> int:
        return len(self._open_flex)

    @property
    def open_bench(self) -> int:
        return max(0, self._spec.bench - self._bench_used)

    @property
    def open_seats(self) -> int:
        return sum(self._open_dedicated.values()) + self.open_flex + self.open_bench

    # -- score -------------------------------------------------------------------
    def needs(self, weights: NeedWeights = DEFAULT_WEIGHTS) -> dict[str, float]:
        score: dict[str, float] = dict.fromkeys(POSITIONS, 0.0)
        for pos, n in self._open_dedicated.items():
            if n > 0:
                factor = weights.starter_by_position.get(pos, 1.0)
                score[pos] = score.get(pos, 0.0) + n * weights.starter * factor
        for elig in self._open_flex:
            share = weights.flex / len(elig)
            for pos in elig:
                score[pos] = score.get(pos, 0.0) + share
        bench = self.open_bench * weights.bench
        if bench:
            total_mix = sum(weights.bench_mix.values()) or 1.0
            for pos, w in weights.bench_mix.items():
                score[pos] = score.get(pos, 0.0) + bench * w / total_mix
        return {pos: round(v, 4) for pos, v in score.items() if v > 0}


# --- state -----------------------------------------------------------------------

PositionLookup = Callable[[str], str | None]


def resolve_my_slot(
    override: int | None, draft_order: Mapping[str, Any] | None, user_id: str | None
) -> int | None:
    """Config override → ``draft_order[user_id]`` → None."""
    if override:
        return int(override)
    if draft_order and user_id and draft_order.get(str(user_id)) is not None:
        return int(draft_order[str(user_id)])
    return None


class DraftState:
    def __init__(
        self,
        spec: DraftSpec,
        *,
        my_slot: int | None = None,
        position_of: PositionLookup | None = None,
    ) -> None:
        self.spec = spec
        self.my_slot = my_slot
        self._position_of = position_of
        self._picks: dict[int, tuple[int, str | None, str | None]] = {}  # pick_no → (slot, id, pos)
        self._rosters: dict[int, TeamRoster] = {
            s: TeamRoster(s, spec) for s in range(1, spec.teams + 1)
        }

    # -- feeding -------------------------------------------------------------------
    def apply(self, ev: PickEvent) -> None:
        """Seat one pick. Idempotent on pick_no; an out-of-order arrival re-seats that team."""
        pos = (ev.metadata or {}).get("position") if ev.metadata else None
        self.add(ev.pick_no, ev.sleeper_id, pos, slot=ev.draft_slot)

    def add(
        self, pick_no: int, sleeper_id: str | None, position: str | None, *, slot: int | None = None
    ) -> None:
        slot = slot or self.spec.slot_for_pick(pick_no)
        if position is None and sleeper_id and self._position_of:
            position = self._position_of(sleeper_id)
        if self._picks.get(pick_no) == (slot, sleeper_id, position):
            return
        self._picks[pick_no] = (slot, sleeper_id, position)
        roster = self._rosters[slot]
        if roster.picks and roster.picks[-1].pick_no > pick_no:
            self._reseat(slot)
        else:
            roster.add(pick_no, sleeper_id, position)

    def remove(self, pick_no: int) -> None:
        """Commissioner undo."""
        entry = self._picks.pop(pick_no, None)
        if entry:
            self._reseat(entry[0])

    def rebuild(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """From ``core.draft_picks`` rows (or ``parse_picks`` dicts): replaces all state."""
        self._picks.clear()
        for s in self._rosters:
            self._rosters[s] = TeamRoster(s, self.spec)
        for r in sorted(rows, key=lambda r: r["pick_no"]):
            meta = r.get("metadata_") or r.get("metadata") or {}
            self.add(
                r["pick_no"], r.get("sleeper_id"), meta.get("position"), slot=r.get("draft_slot")
            )

    def _reseat(self, slot: int) -> None:
        roster = TeamRoster(slot, self.spec)
        for pick_no in sorted(self._picks):
            s, sid, pos = self._picks[pick_no]
            if s == slot:
                roster.add(pick_no, sid, pos)
        self._rosters[slot] = roster

    # -- order -----------------------------------------------------------------------
    @property
    def picks(self) -> Mapping[int, tuple[int | None, str | None, str | None]]:
        """pick_no → (slot, sleeper_id, position) for every seated pick (read-only view)."""
        return MappingProxyType(self._picks)

    @property
    def picks_made(self) -> int:
        return len(self._picks)

    @property
    def current_pick(self) -> int:
        """Next pick_no to be made (``total_picks + 1`` when the draft is over)."""
        return (max(self._picks) + 1) if self._picks else 1

    @property
    def complete(self) -> bool:
        return self.current_pick > self.spec.total_picks

    @property
    def on_the_clock(self) -> int | None:
        return None if self.complete else self.spec.slot_for_pick(self.current_pick)

    def my_next_pick(self) -> int | None:
        if self.my_slot is None or self.complete:
            return None
        nxt = [p for p in self.spec.picks_for_slot(self.my_slot) if p >= self.current_pick]
        return nxt[0] if nxt else None

    def picks_until_my_turn(self) -> int | None:
        """0 = on the clock; None = slot unknown or no picks left for me."""
        nxt = self.my_next_pick()
        return None if nxt is None else nxt - self.current_pick

    def my_pick_window(self) -> list[int]:
        """Opponents' pick_nos between now and my next pick (empty if unknown/on the clock)."""
        nxt = self.my_next_pick()
        return [] if nxt is None else list(range(self.current_pick, nxt))

    # -- rosters / pool --------------------------------------------------------------
    def roster(self, slot: int) -> TeamRoster:
        return self._rosters[slot]

    @property
    def rosters(self) -> Mapping[int, TeamRoster]:
        return self._rosters

    def my_roster(self) -> TeamRoster | None:
        return self._rosters[self.my_slot] if self.my_slot else None

    def pick_at(self, pick_no: int) -> tuple[int | None, str | None, str | None]:
        """(slot, sleeper_id, position) of a made pick, or (None, None, None)."""
        return self._picks.get(pick_no, (None, None, None))

    def taken(self) -> frozenset[str]:
        """Drafted sleeper_ids — the available-pool filter."""
        return frozenset(sid for _, sid, _ in self._picks.values() if sid)

    def my_needs(self, weights: NeedWeights = DEFAULT_WEIGHTS) -> dict[str, float] | None:
        r = self.my_roster()
        return r.needs(weights) if r else None

    def window_open_starters(self) -> Counter[str]:
        """Open dedicated seats summed over the teams picking before my next turn (counts)."""
        out: Counter[str] = Counter()
        for p in self.my_pick_window():
            out.update(self._rosters[self.spec.slot_for_pick(p)].open_starters)
        return out

    def window_needs(self, weights: NeedWeights = DEFAULT_WEIGHTS) -> dict[str, float]:
        """Need score summed over the picks before my next turn — LS-33's demand-side input.
        A team with two picks in the window (snake turn) counts twice, as it should."""
        out: Counter[str] = Counter()
        for p in self.my_pick_window():
            out.update(self._rosters[self.spec.slot_for_pick(p)].needs(weights))
        return {pos: round(v, 4) for pos, v in out.items()}


__all__ = [
    "DEFAULT_WEIGHTS",
    "DraftSpec",
    "DraftState",
    "NeedWeights",
    "Seated",
    "TeamRoster",
    "resolve_my_slot",
]
