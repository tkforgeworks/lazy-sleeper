"""DEF scoring: brackets, TD roll-ups, streaming rank (LS-20)."""

from __future__ import annotations

import pytest

from lazy_sleeper.scoring import (
    DEFAULT_PA_PMF,
    DefenseNormalizer,
    PointsAllowedPmf,
    ScoringRules,
    default_scorer,
    streaming_ranks,
)
from lazy_sleeper.scoring.defense import (
    PA_BUCKET_NAMES,
    bucket_for_points,
    split_pts_allowed,
)


@pytest.fixture
def rules(sleeper_league_payload: dict) -> ScoringRules:
    return ScoringRules.from_league(sleeper_league_payload)


@pytest.fixture
def scorer(rules: ScoringRules):  # noqa: ANN201
    return default_scorer(rules)


# --- league bracket + counting stats from scoring_settings ------------------------


def test_league_def_values(rules: ScoringRules) -> None:
    assert [rules[f"pts_allow_{b}"] for b in PA_BUCKET_NAMES] == [10, 7, 4, 1, 0, -1, -4]
    assert (rules["sack"], rules["int"], rules["fum_rec"], rules["ff"]) == (1, 2, 2, 1)
    assert (rules["def_td"], rules["def_st_td"], rules["safe"], rules["blk_kick"]) == (6, 6, 2, 2)


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        ({"sack": 3}, 3.0),
        ({"int": 2}, 4.0),
        ({"fum_rec": 1}, 2.0),
        ({"ff": 2}, 2.0),
        ({"safe": 1}, 2.0),
        ({"blk_kick": 1}, 2.0),
        ({"def_td": 1}, 6.0),
        ({"def_st_td": 1}, 6.0),
        ({"pts_allow": 0, "pts_allow_0": 1}, 10.0),
        ({"pts_allow": 3, "pts_allow_1_6": 1}, 7.0),
        ({"pts_allow": 10, "pts_allow_7_13": 1}, 4.0),
        ({"pts_allow": 17, "pts_allow_14_20": 1}, 1.0),
        ({"pts_allow": 24, "pts_allow_21_27": 1}, 0.0),
        ({"pts_allow": 30, "pts_allow_28_34": 1}, -1.0),
        ({"pts_allow": 41, "pts_allow_35p": 1}, -4.0),
        ({"yds_allow": 250, "yds_allow_200_299": 1}, 0.0),  # not scored by this league
    ],
)
def test_single_category(scorer, stats: dict, expected: float) -> None:  # noqa: ANN001
    assert scorer.score(stats, "DEF") == pytest.approx(expected)


# --- points-allowed brackets ------------------------------------------------------


@pytest.mark.parametrize(
    ("points", "bucket"),
    [(0, "0"), (1, "1_6"), (6, "1_6"), (7, "7_13"), (13, "7_13"), (14, "14_20"), (20, "14_20"),
     (21, "21_27"), (27, "21_27"), (28, "28_34"), (34, "28_34"), (35, "35p"), (60, "35p")],
)  # fmt: skip
def test_bucket_for_points(points: int, bucket: str) -> None:
    assert bucket_for_points(points) == bucket


def test_espn_actual_uses_exact_points_even_in_straddling_bucket() -> None:
    # 21 allowed sits in ESPN's 18_21 but the league's 21_27 — the exact value wins.
    pa = split_pts_allowed({"pts_allow": 21, "pts_allow_18_21": 1}, DEFAULT_PA_PMF)
    assert pa["21_27"] == 1 and pa["14_20"] == 0
    pa = split_pts_allowed({"pts_allow": 19, "pts_allow_18_21": 1}, DEFAULT_PA_PMF)
    assert pa["14_20"] == 1 and pa["21_27"] == 0


def test_espn_projection_probabilities_split_by_pmf() -> None:
    line = {
        "pts_allow": 22.8,
        "pts_allow_0": 0.01,
        "pts_allow_1_6": 0.04,
        "pts_allow_7_13": 0.15,
        "pts_allow_14_17": 0.15,
        "pts_allow_18_21": 0.15,
        "pts_allow_22_27": 0.23,
        "pts_allow_28_34": 0.18,
        "pts_allow_35_45": 0.08,
        "pts_allow_46p": 0.01,
    }
    pa = split_pts_allowed(line, DEFAULT_PA_PMF)
    assert sum(pa.values()) == pytest.approx(1.0)
    assert pa["0"] == 0.01 and pa["1_6"] == 0.04 and pa["7_13"] == 0.15
    assert pa["28_34"] == 0.18 and pa["35p"] == pytest.approx(0.09)
    # 18_21 splits 18-20 → 14_20 and 21 → 21_27 by empirical mass
    m18_20, m21 = DEFAULT_PA_PMF.mass(18, 20), DEFAULT_PA_PMF.mass(21, 21)
    assert pa["14_20"] == pytest.approx(0.15 + 0.15 * m18_20 / (m18_20 + m21))
    assert pa["21_27"] == pytest.approx(0.23 + 0.15 * m21 / (m18_20 + m21))


def test_integral_expected_points_do_not_trigger_exact_path() -> None:
    # A projection whose expected pts_allow happens to be integral must still split by probabilities.
    line = {"pts_allow": 23.0, "pts_allow_14_17": 0.5, "pts_allow_22_27": 0.5}
    pa = split_pts_allowed(line, DEFAULT_PA_PMF)
    assert pa["14_20"] == 0.5 and pa["21_27"] == 0.5


def test_season_totals_split_by_pmf_when_flags_sum_to_many_games() -> None:
    line = {"pts_allow": 322, "pts_allow_7_13": 4, "pts_allow_14_17": 3, "pts_allow_18_21": 2}
    pa = split_pts_allowed(line, DEFAULT_PA_PMF)
    assert sum(pa.values()) == pytest.approx(9)
    assert pa["7_13"] == 4 and pa["14_20"] > 3 and pa["21_27"] > 0


def test_sleeper_vocabulary_passes_through() -> None:
    line = {"pts_allow_14_20": 0.4, "pts_allow_21_27": 0.6}
    assert split_pts_allowed(line, DEFAULT_PA_PMF) == {
        "0": 0, "1_6": 0, "7_13": 0, "14_20": 0.4, "21_27": 0.6, "28_34": 0, "35p": 0
    }  # fmt: skip


def test_no_points_allowed_data_is_left_alone() -> None:
    assert split_pts_allowed({"sack": 3, "yds_allow_0_100": 1}, DEFAULT_PA_PMF) is None
    norm = DefenseNormalizer()({"sack": 3})
    assert not any(k.startswith("pts_allow") for k in norm)


def test_pmf_mass_and_empty_spread() -> None:
    pmf = PointsAllowedPmf({10: 2, 20: 6, 30: 2})
    assert pmf.mass(0, 15) == 2 and pmf.mass(20, None) == 8
    assert pmf.spread(1, [("a", 40, 50), ("b", 60, None)]) == {"a": 0.5, "b": 0.5}


# --- TD roll-ups ------------------------------------------------------------------


def test_sleeper_td_subkeys_roll_up(scorer) -> None:  # noqa: ANN001
    # Sleeper 2026 season shape: no def_td / def_st_td, only the parts
    line = {"int": 15, "sack": 52, "fum_rec": 11, "blk_kick": 1, "def_kr_td": 1, "def_fum_td": 2}
    norm = DefenseNormalizer()(line)
    assert norm["def_td"] == 2 and norm["def_st_td"] == 1
    assert scorer.score(line, "DEF") == pytest.approx(30 + 52 + 22 + 2 + 12 + 6)


def test_pass_int_td_and_pr_td_variants_roll_up() -> None:
    norm = DefenseNormalizer()({"pass_int_td": 1, "pr_td": 1})
    assert norm["def_td"] == 1 and norm["def_st_td"] == 1


def test_recorded_totals_that_cover_parts_are_not_double_counted() -> None:
    # ESPN actual: def_td already = fum + int; Sleeper-style def_st_td already includes returns
    norm = DefenseNormalizer()(
        {
            "def_td": 2,
            "def_fum_td": 1,
            "def_int_td": 1,
            "def_st_td": 2,
            "def_kr_td": 1,
            "def_pr_td": 1,
        }
    )
    assert norm["def_td"] == 2 and norm["def_st_td"] == 2


def test_espn_blocked_kick_td_plus_return_td_are_added() -> None:
    # ESPN def_st_td = blocked-kick TD only; a separate punt-return TD must be added
    norm = DefenseNormalizer()({"def_st_td": 1, "def_pr_td": 2})
    assert norm["def_st_td"] == 3


# --- full lines: exact actual and a projection ------------------------------------


def test_espn_actual_full_line_scores_exactly(scorer) -> None:  # noqa: ANN001
    line = {
        "ff": 5, "int": 2, "sack": 4, "def_td": 2, "fum_rec": 3, "pts_allow": 10, "yds_allow": 171,
        "def_fum_td": 1, "def_int_td": 1, "pts_allow_7_13": 1, "yds_allow_100_199": 1,
    }  # fmt: skip
    # 5 + 4 + 4 + 12 + 6 + 4
    assert scorer.score(line, "DEF") == pytest.approx(35.0)


def test_scorer_only_normalizes_def_rows(rules: ScoringRules) -> None:
    s = default_scorer(rules)
    line = {"def_fum_td": 1, "pts_allow": 0, "pts_allow_0": 1}
    assert s.score(line, "DEF") == pytest.approx(16.0)
    assert s.score(line, "K") == pytest.approx(10.0)  # bracket key scores, sub-key TD does not


# --- streaming rank ----------------------------------------------------------------


class _FakeSession:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, stmt):  # noqa: ANN001, ANN201
        return iter(self._rows)


def test_streaming_ranks_orders_by_mean_points_per_game(scorer) -> None:  # noqa: ANN001
    rows = [
        ("DEN", "DEN", {"sack": 5, "pts_allow": 3, "pts_allow_1_6": 1}),  # 5 + 7 = 12
        ("DEN", "DEN", {"sack": 1, "pts_allow": 30, "pts_allow_28_34": 1}),  # 1 - 1 = 0 → 6.0 avg
        ("SEA", "SEA", {"sack": 3, "pts_allow": 10, "pts_allow_7_13": 1}),  # 3 + 4 = 7 → 7.0 avg
        ("CAR", None, {"sack": 0, "pts_allow": 41, "pts_allow_35p": 1}),  # -4
        (None, None, {"sack": 9}),  # no team → skipped
    ]
    ranks = streaming_ranks(_FakeSession(rows), scorer)
    assert [(r.rank, r.team, r.games) for r in ranks] == [
        (1, "SEA", 1),
        (2, "DEN", 2),
        (3, "CAR", 1),
    ]
    assert [r.ppg for r in ranks] == pytest.approx([7.0, 6.0, -4.0])
    assert ranks[1].sleeper_id == "DEN" and ranks[2].sleeper_id is None


def test_streaming_ranks_tie_break_is_deterministic(scorer) -> None:  # noqa: ANN001
    rows = [("SEA", "SEA", {"sack": 2}), ("DEN", "DEN", {"sack": 2})]
    assert [r.team for r in streaming_ranks(_FakeSession(rows), scorer)] == ["DEN", "SEA"]


def test_default_pmf_is_frozen_from_real_games() -> None:
    assert sum(DEFAULT_PA_PMF.counts.values()) == 1078
    assert DEFAULT_PA_PMF.mass(14, 20) > DEFAULT_PA_PMF.mass(35, None)
