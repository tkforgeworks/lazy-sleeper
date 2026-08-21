"""Waiver-aware K/DEF (LS-33 follow-up): streaming baseline, cheaper seats, fill-them-last."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lazy_sleeper.board.baselines import RosterShape, derive_baselines
from lazy_sleeper.board.tiers import BoardRow, TierConfig
from lazy_sleeper.board.vorp import PlayerValue
from lazy_sleeper.draft.poller import PickEvent
from lazy_sleeper.draft.signals import advise
from lazy_sleeper.draft.state import DEFAULT_WEIGHTS, DraftSpec, DraftState, NeedWeights
from lazy_sleeper.scoring.league import ScoringRules

SHAPE = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF") + ("BN",) * 5


@pytest.fixture
def spec() -> DraftSpec:
    return DraftSpec.build(ScoringRules(weights={}, roster_positions=SHAPE, total_rosters=12))


def _row(sid: str, pos: str, vorp: float, adp: float) -> BoardRow:
    return BoardRow(
        PlayerValue(sid, pos, "X", 100 + vorp, 100, vorp, 1, {}), 1, False, None, adp=adp
    )


def _ev(pick_no: int, pos: str, spec: DraftSpec) -> PickEvent:
    return PickEvent("d", pick_no, spec.round_of(pick_no), spec.slot_for_pick(pick_no), f"t{pick_no}",
                     None, datetime.now(UTC), pick_no, 1, {"position": pos})  # fmt: skip


def test_stream_depth_moves_the_kicker_baseline_up() -> None:
    shape = RosterShape(teams=12, dedicated={"K": 1, "RB": 1}, flex=())
    rows = [(f"k{i}", "K", 150.0 - i) for i in range(20)] + [
        (f"r{i}", "RB", 300.0 - i) for i in range(20)
    ]
    last_starter = derive_baselines(rows, shape)
    streaming = derive_baselines(
        rows, shape, stream_depth={"K": 6, "DEF": 6}
    )  # DEF absent: ignored
    assert last_starter["K"].cutoff_rank == 12 and last_starter["K"].points == 150.0 - 11
    assert streaming["K"].cutoff_rank == 6 and streaming["K"].points == 150.0 - 5
    assert streaming["RB"] == last_starter["RB"]  # untouched
    assert derive_baselines(rows, shape, stream_depth={"K": 0})["K"].cutoff_rank == 12  # 0 = off


def test_open_k_def_seats_count_a_quarter_of_a_real_starter(spec: DraftSpec) -> None:
    r = DraftState(spec).roster(1)
    needs = r.needs()
    assert needs["K"] == 0.25 and needs["DEF"] == 0.25 and needs["QB"] >= 1.0
    full = r.needs(NeedWeights(starter_by_position={}))
    assert full["K"] == 1.0
    assert DEFAULT_WEIGHTS.starter_by_position == {"K": 0.25, "DEF": 0.25}


def test_k_def_need_bonus_only_in_the_last_rounds(spec: DraftSpec) -> None:
    cfg = TierConfig()
    rows = [_row("k", "K", 4.0, 160.0), _row("rb", "RB", 4.0, 160.0)]
    adp = {"k": 160.0, "rb": 160.0}

    def score_at(pick_no: int) -> dict[str, float]:
        st = DraftState(spec, my_slot=1)
        for n in range(1, pick_no):
            st.apply(_ev(n, "WR", spec))  # nobody has a K; my RB seats are still open too
        return {r.value.sleeper_id: r.pick_score for r in advise(rows, st, adp, cfg)}

    # round 10 of 15: K bonus suppressed → the RB (open starter) wins
    early = score_at(spec.pick_for(1, 10))
    assert early["rb"] > early["k"]
    # round 13 (rounds − late_rounds + 1): K bonus applies (0.25 × 8 = +2 over the RB's bench-only)
    late = score_at(spec.pick_for(1, 13))
    assert late["k"] > early["k"]
    # dial off → bonus in every round
    always = {r.value.sleeper_id: r.pick_score for r in advise(
        rows, DraftState(spec, my_slot=1), adp, TierConfig(late_rounds=0)
    )}  # fmt: skip
    assert always["k"] > 4.0 - 1e-9


def test_config_accepts_zero_for_off_dials() -> None:
    from lazy_sleeper.board.config import FIELDS, NONNEG_INT_FIELDS

    assert set(NONNEG_INT_FIELDS) <= set(FIELDS)
    assert TierConfig(stream_depth=0, late_rounds=0).stream_depth == 0
