"""Draft-time signals (LS-33): survival, runs, pick_score advice. DB-free."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from lazy_sleeper.board.tiers import BoardRow, TierConfig
from lazy_sleeper.board.vorp import PlayerValue
from lazy_sleeper.draft.poller import PickEvent, ReplayFixture
from lazy_sleeper.draft.signals import (
    SearchRankAdp,
    advise,
    demand_stretch,
    detect_runs,
    effective_adp,
    expected_best_available,
    survival,
)
from lazy_sleeper.draft.state import DraftSpec, DraftState
from lazy_sleeper.scoring.league import ScoringRules

FIXTURE = Path(__file__).parent / "fixtures" / "mock_draft_1396298350046760960.json.gz"
SHAPE = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX", "K", "DEF") + ("BN",) * 5
CFG = TierConfig()


@pytest.fixture
def spec() -> DraftSpec:
    return DraftSpec.build(ScoringRules(weights={}, roster_positions=SHAPE, total_rosters=12))


def _row(
    sid: str, pos: str, vorp: float, *, adp: float | None = None, cliff: bool = False
) -> BoardRow:
    return BoardRow(
        PlayerValue(sid, pos, "X", 100 + vorp, 100, vorp, 1, {}), tier=1, cliff=cliff, gap_to_next=None, adp=adp
    )  # fmt: skip


def _ev(pick_no: int, pos: str, spec: DraftSpec, sid: str | None = None) -> PickEvent:
    return PickEvent("d", pick_no, spec.round_of(pick_no), spec.slot_for_pick(pick_no), sid or f"t{pick_no}",
                     None, datetime.now(UTC), pick_no, 1, {"position": pos})  # fmt: skip


# --- survival ------------------------------------------------------------------------


def test_survival_monotone_in_adp_and_pick() -> None:
    assert survival(10, 10, CFG) == pytest.approx(0.55, abs=0.02)  # at his ADP: a coin flip
    assert survival(10, 30, CFG) < 0.01 and survival(10, 2, CFG) > 0.97
    assert survival(20, 25, CFG) < survival(40, 25, CFG)  # later ADP survives longer
    assert survival(40, 25, CFG) > survival(40, 35, CFG)  # further away → less likely
    # scatter grows with ADP: the same 8-pick gap is less decisive at ADP 120 than at ADP 20
    assert survival(120, 128, CFG) > survival(20, 28, CFG)


def test_search_rank_pseudo_adp_is_monotone_and_interpolates() -> None:
    m = SearchRankAdp(
        [(1, 1.5), (2, 2.0), (10, 12.0), (10, 14.0), (50, 60.0), (60, 55.0), (70, 999)]
    )
    assert m.adp_for(1) == 1.5 and m.adp_for(10) == 13.0
    assert m.adp_for(6) == pytest.approx(2.0 + (13.0 - 2.0) * 4 / 8)
    assert m.adp_for(60) == 60.0  # cumulative max: rank 60 can't map below rank 50
    assert m.adp_for(65) is None and m.adp_for(None) is None  # past the fitted ranks / unknown
    assert not SearchRankAdp([]) and SearchRankAdp([]).adp_for(5) is None


def test_effective_adp_prefers_adp_then_rank_then_none() -> None:
    m = SearchRankAdp([(1, 1.0), (100, 100.0)])
    adp = {"a": 12.0, "b": 999.0}
    ranks = {"b": 50, "c": 20}
    assert effective_adp("a", adp, ranks, m) == 12.0
    assert effective_adp("b", adp, ranks, m) == pytest.approx(50.0)  # 999 sentinel → rank fallback
    assert effective_adp("c", adp, ranks, m) == pytest.approx(20.0)
    assert effective_adp("d", adp, ranks, m) is None
    assert effective_adp("b", adp, None, None) is None


def test_demand_stretch() -> None:
    needs = {"RB": 3.0, "WR": 1.0}  # mean 2 → RB +50 %, WR −50 %
    assert demand_stretch("RB", needs, CFG) == pytest.approx(1.25)
    assert demand_stretch("WR", needs, CFG) == pytest.approx(0.75)
    assert demand_stretch("QB", needs, CFG) == pytest.approx(0.5)  # no demand at all
    assert demand_stretch("RB", {}, CFG) == 1.0


# --- runs ------------------------------------------------------------------------------


def test_detect_runs_count_and_streak() -> None:
    r = detect_runs(["RB", "WR", "RB", "QB", "RB", "WR", "RB", "TE"], CFG)
    assert r["RB"].count == 4 and r["RB"].run and r["RB"].streak == 0
    assert r["TE"].streak == 1 and not r["TE"].run
    s = detect_runs(["WR", "WR", "QB", "QB", "QB"], CFG)
    assert s["QB"].streak == 3 and s["QB"].run and not s["WR"].run
    assert detect_runs([], CFG) == {}
    strict = TierConfig(run_threshold=5, run_streak=4)
    assert not any(
        x.run
        for x in detect_runs(["RB", "WR", "RB", "QB", "RB", "WR", "RB", "TE"], strict).values()
    )
    # only the last `run_window` picks count
    assert "WR" not in detect_runs(["WR"] + ["RB"] * 8, CFG)


# --- pick score ------------------------------------------------------------------------


def test_expected_best_available_chain() -> None:
    assert expected_best_available([(50, 1.0), (40, 1.0)]) == 50.0
    assert expected_best_available([(50, 0.0), (40, 1.0)]) == 40.0
    assert expected_best_available([(50, 0.5), (40, 0.5)]) == pytest.approx(25 + 10)
    assert expected_best_available([]) == 0.0


def test_advise_prefers_the_player_who_will_not_survive(spec: DraftSpec) -> None:
    st = DraftState(spec, my_slot=1)
    st.apply(_ev(1, "QB", spec))  # my first pick made; now pick 2, my next is 24
    rows = [
        _row("stud", "RB", 60, adp=80.0),  # everyone passes on him: survives to 24 ≈ 1
        _row("hot", "RB", 58, adp=8.0),  # gone before 24
        _row("wr", "WR", 30, adp=200.0),
    ]
    out = advise(rows, st, {r.value.sleeper_id: r.adp for r in rows}, CFG)
    by = {r.value.sleeper_id: r for r in out}
    assert by["hot"].survival < 0.05 and by["stud"].survival > 0.95
    assert out[0].value.sleeper_id == "hot"  # lower VORP, but the stud will still be there
    bonus = CFG.need_bonus * st.my_needs()["RB"]
    # hot has no option value (the stud is the fallback either way) → full VORP + need bonus;
    # stud's option value = 60 − E[best without him] = 60 − wr 30 → scores 30 + bonus
    assert by["hot"].pick_score == pytest.approx(58 + bonus, abs=0.5)
    assert by["stud"].pick_score == pytest.approx(30 + bonus, abs=0.5)
    assert "t1" not in by  # taken players are dropped


def test_advise_on_the_clock_looks_past_my_current_pick(spec: DraftSpec) -> None:
    st = DraftState(spec, my_slot=3)
    st.apply(_ev(1, "RB", spec))
    st.apply(_ev(2, "RB", spec))
    assert st.on_the_clock == 3  # deciding pick 3; waiting means pick 22
    rows = [_row("a", "WR", 40, adp=12.0), _row("b", "WR", 39, adp=40.0)]
    out = advise(rows, st, {"a": 12.0, "b": 40.0}, CFG)
    by = {r.value.sleeper_id: r for r in out}
    assert by["a"].survival < 0.1 and by["b"].survival > 0.9
    assert out[0].value.sleeper_id == "a"


def test_advise_without_slot_uses_horizon_and_no_need_bonus(spec: DraftSpec) -> None:
    st = DraftState(spec)
    rows = [_row("a", "RB", 50, adp=5.0), _row("b", "RB", 45, adp=30.0), _row("nomarket", "K", 5)]
    out = advise(rows, st, {"a": 5.0, "b": 30.0}, CFG)  # horizon = 12 picks
    by = {r.value.sleeper_id: r for r in out}
    assert by["a"].survival < 0.05 and by["b"].survival > 0.95
    # K with no market data survives for sure, but b (45) is the fallback either way, so the K
    # has no option value: taking him now costs nothing -> score = his VORP
    assert by["nomarket"].survival is None and by["nomarket"].pick_score == 5.0
    # b is the sure fallback: option value = 45 - (K 5 without him) = 40 -> score 5; a keeps his VORP
    assert by["b"].pick_score == pytest.approx(5.0, abs=1.5)
    assert by["a"].pick_score == pytest.approx(50.0, abs=0.5) and out[0] is by["a"]
    far = advise(rows, st, {"a": 5.0, "b": 30.0}, CFG, horizon=40)
    assert {r.value.sleeper_id: r for r in far}["b"].survival < 0.1


def test_advise_attaches_runs_and_need_bonus(spec: DraftSpec) -> None:
    st = DraftState(spec, my_slot=1)  # now pick 12; my next is 24 → 12 opponent picks
    for n in range(1, 12):
        st.apply(_ev(n, "RB" if n > 6 else "WR", spec))  # last five picks all RB
    rows = [_row("rb", "RB", 20, adp=30.0), _row("te", "TE", 20, adp=30.0)]
    out = advise(rows, st, {"rb": 30.0, "te": 30.0}, CFG)
    by = {r.value.sleeper_id: r for r in out}
    assert by["rb"].run and by["rb"].run_count == 5 and not by["te"].run
    # demand: every opponent still needs RB more than TE → RBs die faster in the window
    assert by["rb"].survival < by["te"].survival
    # my need bonus: RB need (2 starters + flex + bench) > TE need (1 starter + flex + bench)
    assert st.my_needs()["RB"] > st.my_needs()["TE"]


def test_replay_mock_draft_advice_is_sane(spec: DraftSpec) -> None:
    """At each of my 15 turns on the recorded mock: recommended players are available, and
    survival is lower on average for players the room took before my next turn than for those
    still there — a light calibration check on 180 real picks."""
    fx = ReplayFixture.load(FIXTURE)
    picks = {p["pick_no"]: p for p in fx.picks}
    # board = every drafted player with VORP ∝ (181 − first pick) so the market and board agree-ish
    rows = [
        _row(p["player_id"], p["metadata"]["position"], float(181 - n), adp=float(n))
        for n, p in picks.items()
    ]
    adp = {r.value.sleeper_id: r.adp for r in rows}
    st = DraftState(spec, my_slot=8)
    taken_lo, left_hi = [], []
    for n in range(1, 181):
        if st.on_the_clock == 8:
            out = advise(rows, st, adp, CFG)
            assert all(r.value.sleeper_id not in st.taken() for r in out)
            assert out[0].pick_score is not None and out[0].pick_score >= out[-1].pick_score
            nxt = next((p for p in spec.picks_for_slot(8) if p > n), 181)
            gone = {picks[m]["player_id"] for m in range(n + 1, nxt)}
            for r in out:
                (taken_lo if r.value.sleeper_id in gone else left_hi).append(r.survival)
        p = picks[n]
        st.apply(_ev(n, p["metadata"]["position"], spec, p["player_id"]))
    assert sum(taken_lo) / len(taken_lo) < sum(left_hi) / len(left_hi)
