"""Draft-state model (LS-32): order math, seat filling, needs, my-turn queries. DB-free."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from lazy_sleeper.draft.poller import MemorySink, PickEvent, ReplayFixture, ReplaySource
from lazy_sleeper.draft.state import (
    DEFAULT_WEIGHTS,
    DraftSpec,
    DraftState,
    NeedWeights,
    resolve_my_slot,
)
from lazy_sleeper.scoring.league import ScoringRules

FIXTURE = Path(__file__).parent / "fixtures" / "mock_draft_1396298350046760960.json.gz"
LEAGUE_SHAPE = (
    "QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF", "BN", "BN", "BN", "BN", "BN",
)  # fmt: skip
TIM = "1268591266036203520"


def _rules() -> ScoringRules:
    return ScoringRules(weights={"pass_td": 4.0}, roster_positions=LEAGUE_SHAPE, total_rosters=12)


@pytest.fixture
def spec() -> DraftSpec:
    return DraftSpec.build(_rules(), {"teams": 12, "rounds": 15, "type": "snake"})


def _ev(pick_no: int, position: str, spec: DraftSpec, sleeper_id: str | None = None) -> PickEvent:
    return PickEvent(
        draft_id="d",
        pick_no=pick_no,
        round=spec.round_of(pick_no),
        draft_slot=spec.slot_for_pick(pick_no),
        sleeper_id=sleeper_id or f"p{pick_no}",
        picked_by=None,
        first_seen_at=datetime.now(UTC),
        total_picks=pick_no,
        poll_seq=1,
        metadata={"position": position},
    )


# --- spec / order ----------------------------------------------------------------


def test_spec_from_league_rules(spec: DraftSpec) -> None:
    assert (spec.teams, spec.rounds, spec.bench, spec.total_picks) == (12, 15, 5, 180)
    assert spec.shape.dedicated == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}
    assert spec.shape.flex == (("RB", "WR", "TE"), ("RB", "WR", "TE"))


def test_spec_falls_back_to_rules_when_draft_doc_is_missing() -> None:
    s = DraftSpec.build(_rules())
    assert (s.teams, s.rounds, s.type) == (12, 15, "snake")


def test_snake_slot_and_pick_round_trip(spec: DraftSpec) -> None:
    assert [spec.slot_for_pick(n) for n in (1, 12, 13, 24, 25)] == [1, 12, 12, 1, 1]
    assert spec.picks_for_slot(8) == [
        8,
        17,
        32,
        41,
        56,
        65,
        80,
        89,
        104,
        113,
        128,
        137,
        152,
        161,
        176,
    ]
    for n in range(1, 181):
        assert spec.pick_for(spec.slot_for_pick(n), spec.round_of(n)) == n


def test_linear_order() -> None:
    s = DraftSpec.build(_rules(), {"type": "linear"})
    assert [s.slot_for_pick(n) for n in (1, 12, 13, 24)] == [1, 12, 1, 12]
    assert s.picks_for_slot(3)[:3] == [3, 15, 27]


# --- seats ------------------------------------------------------------------------


def test_seats_fill_dedicated_then_flex_then_bench(spec: DraftSpec) -> None:
    st = DraftState(spec)
    # slot 1 picks: 1, 24, 25, 48, 49, 72, 73 ...
    seq = ["RB", "RB", "RB", "RB", "TE", "TE", "QB"]
    for pick_no, pos in zip(spec.picks_for_slot(1), seq, strict=False):
        st.apply(_ev(pick_no, pos, spec))
    r = st.roster(1)
    assert [p.seat for p in r.picks] == ["RB", "RB", "FLEX", "FLEX", "TE", "BN", "QB"]
    assert r.counts == {"RB": 4, "TE": 2, "QB": 1}
    assert r.open_starters == {"WR": 2, "K": 1, "DEF": 1}
    assert (r.open_flex, r.open_bench, r.open_seats) == (0, 4, 8)


def test_kicker_never_takes_a_flex_seat(spec: DraftSpec) -> None:
    st = DraftState(spec)
    picks = spec.picks_for_slot(5)
    st.apply(_ev(picks[0], "K", spec))
    st.apply(_ev(picks[1], "K", spec))
    assert [p.seat for p in st.roster(5).picks] == ["K", "BN"]
    assert st.roster(5).open_flex == 2


def test_needs_weighted_and_counts(spec: DraftSpec) -> None:
    r = DraftState(spec).roster(1)
    empty = r.needs()
    # RB: 2 starters + 2 flex × 0.5/3 + 5 bench × 0.25 × 0.4
    assert empty["RB"] == pytest.approx(2 + 2 * 0.5 / 3 + 5 * 0.25 * 0.4, abs=1e-4)
    assert empty["K"] == 1.0 and empty["DEF"] == 1.0
    assert set(empty) == {"QB", "RB", "WR", "TE", "K", "DEF"}

    st = DraftState(spec)
    for pick_no, pos in zip(spec.picks_for_slot(1), ["K", "DEF"], strict=False):
        st.apply(_ev(pick_no, pos, spec))
    after = st.roster(1).needs()
    assert "K" not in after and "DEF" not in after
    custom = st.roster(1).needs(NeedWeights(starter=2.0, flex=0.0, bench=0.0, bench_mix={}))
    assert custom == {"QB": 2.0, "RB": 4.0, "WR": 4.0, "TE": 2.0}


# --- my turn --------------------------------------------------------------------


def test_picks_until_my_turn_snake(spec: DraftSpec) -> None:
    st = DraftState(spec, my_slot=8)
    assert (st.current_pick, st.on_the_clock, st.my_next_pick(), st.picks_until_my_turn()) == (
        1, 1, 8, 7,
    )  # fmt: skip
    for n in range(1, 8):
        st.apply(_ev(n, "RB", spec))
    assert st.picks_until_my_turn() == 0 and st.on_the_clock == 8 and st.my_pick_window() == []
    st.apply(_ev(8, "WR", spec))
    assert (st.my_next_pick(), st.picks_until_my_turn()) == (17, 8)
    assert st.my_pick_window() == list(range(9, 17))
    # the turn: slot 12 picks 12 and 13 back to back → counted twice in the window
    st2 = DraftState(spec, my_slot=1)
    for n in range(1, 3):
        st2.apply(_ev(n, "RB", spec))
    assert st2.my_pick_window().count(12) == 1 and 13 in st2.my_pick_window()
    # window = picks 3..23: slots 3..12 (10) then 12..2 (11) = 21 opponent picks, all still need a QB
    assert len(st2.my_pick_window()) == 21 and st2.window_open_starters()["QB"] == 21


def test_unknown_slot_is_none_safe(spec: DraftSpec) -> None:
    st = DraftState(spec)
    st.apply(_ev(1, "RB", spec))
    assert st.my_slot is None and st.my_next_pick() is None
    assert st.picks_until_my_turn() is None and st.my_pick_window() == []
    assert st.my_roster() is None and st.my_needs() is None
    assert st.window_needs() == {} and st.taken() == {"p1"}
    assert st.on_the_clock == 2


def test_resolve_my_slot_precedence() -> None:
    assert resolve_my_slot(3, {TIM: 8}, TIM) == 3
    assert resolve_my_slot(None, {TIM: 8}, TIM) == 8
    assert resolve_my_slot(None, {TIM: 8}, "other") is None
    assert resolve_my_slot(None, None, TIM) is None
    assert resolve_my_slot(0, {TIM: "8"}, TIM) == 8


def test_window_needs_sum_over_opponents(spec: DraftSpec) -> None:
    st = DraftState(spec, my_slot=12)
    assert len(st.my_pick_window()) == 11
    w = st.window_needs()
    per_team_rb = 2 * DEFAULT_WEIGHTS.starter + 2 * DEFAULT_WEIGHTS.flex / 3 + 5 * 0.25 * 0.4
    assert w["K"] == 11.0 and w["RB"] == pytest.approx(11 * per_team_rb, abs=1e-3)


# --- mutation ----------------------------------------------------------------------


def test_apply_is_idempotent_and_remove_reseats(spec: DraftSpec) -> None:
    st = DraftState(spec, my_slot=1)
    p = spec.picks_for_slot(1)
    for pick_no, pos in zip(p[:3], ["RB", "RB", "RB"], strict=False):
        st.apply(_ev(pick_no, pos, spec))
        st.apply(_ev(pick_no, pos, spec))
    assert [s.seat for s in st.roster(1).picks] == ["RB", "RB", "FLEX"]
    assert st.picks_made == 3
    st.remove(p[1])  # undo the 2nd RB → the 3rd slides into the dedicated seat
    assert [s.seat for s in st.roster(1).picks] == ["RB", "RB"]
    assert st.current_pick == p[2] + 1 and st.taken() == {f"p{p[0]}", f"p{p[2]}"}


def test_out_of_order_arrival_reseats_deterministically(spec: DraftSpec) -> None:
    a, b = DraftState(spec), DraftState(spec)
    p = spec.picks_for_slot(4)
    a.apply(_ev(p[0], "TE", spec))
    a.apply(_ev(p[1], "TE", spec))
    b.apply(_ev(p[1], "TE", spec))
    b.apply(_ev(p[0], "TE", spec))
    assert (
        [s.seat for s in a.roster(4).picks] == [s.seat for s in b.roster(4).picks] == ["TE", "FLEX"]
    )


def test_rebuild_from_rows_matches_incremental(spec: DraftSpec) -> None:
    inc = DraftState(spec, my_slot=8)
    rows = []
    for n in range(1, 41):
        pos = ["RB", "WR", "TE", "QB"][n % 4]
        inc.apply(_ev(n, pos, spec))
        rows.append(
            {"pick_no": n, "draft_slot": spec.slot_for_pick(n), "sleeper_id": f"p{n}",
             "metadata_": {"position": pos}}
        )  # fmt: skip
    rb = DraftState(spec, my_slot=8)
    rb.rebuild(reversed(rows))
    for s in range(1, 13):
        assert [x.seat for x in rb.roster(s).picks] == [x.seat for x in inc.roster(s).picks]
    assert rb.current_pick == inc.current_pick == 41 and rb.taken() == inc.taken()


def test_position_lookup_fallback(spec: DraftSpec) -> None:
    st = DraftState(spec, position_of=lambda sid: {"x": "WR"}.get(sid))
    st.add(1, "x", None)
    st.add(2, "y", None)
    assert [s.seat for s in st.roster(1).picks] == ["WR"]
    assert [s.seat for s in st.roster(2).picks] == ["BN"]  # unknown position → bench, counted


# --- the recorded mock draft ----------------------------------------------------


def test_replay_mock_draft_fills_every_roster(spec: DraftSpec) -> None:
    fx = ReplayFixture.load(FIXTURE)
    st = DraftState(spec, my_slot=resolve_my_slot(None, fx.draft["draft_order"], TIM))
    poller_events: list[PickEvent] = []
    from lazy_sleeper.draft.poller import DraftPoller

    DraftPoller(ReplaySource(fx), MemorySink(fx.draft_id), fx.draft_id, sleep=lambda _: None).run(
        poller_events.append
    )
    for ev in poller_events:
        st.apply(ev)
    assert st.my_slot == 8 and st.complete and st.picks_made == 180
    assert st.on_the_clock is None and st.my_next_pick() is None
    assert len(st.taken()) == 180
    for s in range(1, 13):
        r = st.roster(s)
        # 15 seated; a CPU roster may leave a starter open while the bench overflows
        assert len(r.picks) == 15 and r.open_bench == 0 and r.open_flex == 0, s
        assert set(r.needs()) == set(r.open_starters), s
    mine = st.my_roster()
    assert mine is not None and [p.pick_no for p in mine.picks] == spec.picks_for_slot(8)
    assert mine.open_starters == {"DEF": 1} and set(mine.needs()) == {"DEF"}  # no DEF drafted
