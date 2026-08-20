"""Scoring engine (LS-18): league `scoring_settings` applied to Sleeper-vocabulary stat lines."""

from __future__ import annotations

import json

import pytest

from lazy_sleeper.scoring import (
    OFFENSE_POSITIONS,
    Scorer,
    ScoringRules,
    breakdown,
    score,
)


@pytest.fixture
def rules(sleeper_league_payload: dict) -> ScoringRules:
    return ScoringRules.from_league(sleeper_league_payload)


# --- rules -----------------------------------------------------------------


def test_rules_from_league_payload(rules: ScoringRules) -> None:
    assert rules.league_id == "1392685475625443328"
    assert rules.roster_positions[:3] == ("QB", "RB", "RB")
    assert rules.roster_positions.count("FLEX") == 2
    assert rules.total_rosters == 12
    # the league's known values
    assert rules["pass_td"] == 4.0
    assert rules["pass_yd"] == pytest.approx(0.04)
    assert rules["pass_int"] == -1.0
    assert rules["rec"] == 1.0
    assert rules["fum_lost"] == -2.0


def test_rules_unknown_key_weight_is_zero(rules: ScoringRules) -> None:
    assert rules.weight("no_such_stat") == 0.0
    assert "no_such_stat" not in rules
    assert "rec" in rules


def test_rules_reject_prescored_keys() -> None:
    with pytest.raises(ValueError, match="pts_ppr"):
        ScoringRules({"rec": 1.0, "pts_ppr": 1.0})


def test_rules_require_scoring_settings() -> None:
    with pytest.raises(ValueError):
        ScoringRules.from_league({"league_id": "x"})


def test_rules_drop_null_and_coerce_numeric() -> None:
    r = ScoringRules({"rec": "0.5", "rec_yd": None, "rec_td": 6})
    assert r.weights == {"rec": 0.5, "rec_td": 6.0}


# --- each stat category independently, at the league's values ------------------


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        # QB
        ({"pass_yd": 300}, 12.0),
        ({"pass_td": 3}, 12.0),
        ({"pass_int": 2}, -2.0),
        ({"pass_2pt": 1}, 2.0),
        # rushing (any position)
        ({"rush_yd": 100}, 10.0),
        ({"rush_td": 2}, 12.0),
        ({"rush_2pt": 1}, 2.0),
        # receiving — full PPR
        ({"rec": 7}, 7.0),
        ({"rec_yd": 85}, 8.5),
        ({"rec_td": 1}, 6.0),
        ({"rec_2pt": 1}, 2.0),
        # fumbles: only lost fumbles cost; total fumbles are weighted 0 in this league
        ({"fum_lost": 1}, -2.0),
        ({"fum": 3}, 0.0),
        # special teams / misc TDs available to offense players
        ({"st_td": 1}, 6.0),
        ({"fum_rec_td": 1}, 6.0),
    ],
)
def test_single_category(rules: ScoringRules, stats: dict, expected: float) -> None:
    assert score(stats, rules) == pytest.approx(expected)


def test_full_qb_line(rules: ScoringRules) -> None:
    line = {"pass_yd": 275, "pass_td": 2, "pass_int": 1, "rush_yd": 40, "rush_td": 1, "fum_lost": 1}
    # 11 + 8 - 1 + 4 + 6 - 2
    assert score(line, rules) == pytest.approx(26.0)


def test_full_wr_line(rules: ScoringRules) -> None:
    line = {"rec": 6, "rec_yd": 94, "rec_td": 1, "rush_yd": 12, "rec_tgt": 9}
    # 6 + 9.4 + 6 + 1.2 ; targets are not scored
    assert score(line, rules) == pytest.approx(22.6)


# --- what must NOT count -------------------------------------------------------


def test_prescored_and_unscored_keys_are_ignored(rules: ScoringRules) -> None:
    line = {
        "rec": 5,
        "pts_ppr": 999.0,
        "pts_std": 999.0,
        "pts_half_ppr": 999.0,
        "adp_ppr": 12.0,
        "gp": 17,
        "rec_tgt": 8,
        "bonus_rec_te": 5,
    }
    assert score(line, rules) == pytest.approx(5.0)


def test_non_numeric_values_are_ignored(rules: ScoringRules) -> None:
    line = {"rec": None, "rec_yd": "abc", "rec_td": True, "rush_yd": "50"}
    # bools are not stats; numeric strings are tolerated
    assert score(line, rules) == pytest.approx(5.0)


def test_empty_line_scores_zero(rules: ScoringRules) -> None:
    assert score({}, rules) == 0.0
    assert breakdown({}, rules) == {}


# --- breakdown ---------------------------------------------------------------


def test_breakdown_lists_only_contributing_keys(rules: ScoringRules) -> None:
    parts = breakdown({"rec": 4, "rec_yd": 0, "fum": 2, "pass_int": 1, "rec_tgt": 6}, rules)
    assert parts == {"rec": 4.0, "pass_int": -1.0}
    assert sum(parts.values()) == pytest.approx(score({"rec": 4, "pass_int": 1}, rules))


# --- league-agnostic: any Sleeper scoring map works ------------------------------


def test_engine_has_no_hardcoded_constants() -> None:
    six_pt_td_half_ppr = ScoringRules({"pass_td": 6, "rec": 0.5, "pass_int": -2})
    line = {"pass_td": 2, "rec": 4, "pass_int": 1}
    assert score(line, six_pt_td_half_ppr) == pytest.approx(12 + 2 - 2)


# --- Scorer + normalizer hook (K / DEF plug in via LS-19 / LS-20) --------------


def test_scorer_applies_position_normalizer(rules: ScoringRules) -> None:
    def fake_k(stats):  # noqa: ANN001, ANN202
        return {**stats, "xpm": stats.get("xpa", 0)}

    scorer = Scorer(rules, normalizers={"K": fake_k})
    assert scorer.score({"xpa": 3}, "K") == pytest.approx(3.0)
    assert scorer.score({"xpa": 3}, "QB") == 0.0
    assert scorer.score({"xpa": 3}) == 0.0
    ex = scorer.explain({"xpa": 3}, "K")
    assert ex.points == pytest.approx(3.0) and ex.breakdown == {"xpm": 3.0}


def test_offense_positions_constant() -> None:
    assert {"QB", "RB", "WR", "TE"} == OFFENSE_POSITIONS


# --- parity vs Sleeper's own projected points on the trimmed real payload ------
# This league uses Sleeper's default PPR map, so our score reproduces `pts_ppr` for RB/WR/TE.
# Sleeper's *weekly* QB `pts_ppr` does not match its own map (it implies 0.05/pass yd, not
# 0.04) — a live example of why pre-scored provider points are never ingested as truth.


def test_parity_with_sleeper_pts_ppr(rules: ScoringRules, sleeper_proj_payload: bytes) -> None:
    rows = json.loads(sleeper_proj_payload)
    checked = 0
    for row in rows:
        stats = row.get("stats") or {}
        if "pts_ppr" not in stats:
            continue
        pos = (row.get("player") or {}).get("position")
        if pos not in OFFENSE_POSITIONS - {"QB"}:
            continue
        assert score(stats, rules) == pytest.approx(stats["pts_ppr"], abs=0.05), row["player_id"]
        checked += 1
    assert checked > 0
