"""K scoring with distance-mix approximation (LS-19)."""

from __future__ import annotations

import pytest

from lazy_sleeper.scoring import (
    DEFAULT_MIX,
    DistanceMix,
    KickerNormalizer,
    ScoringRules,
    default_scorer,
    score,
)
from lazy_sleeper.scoring.kicking import BUCKET_NAMES, split_buckets

SHORT = ("0_19", "20_29", "30_39")
LONG = ("40_49", "50_59", "60p")


@pytest.fixture
def rules(sleeper_league_payload: dict) -> ScoringRules:
    return ScoringRules.from_league(sleeper_league_payload)


@pytest.fixture
def scorer(rules: ScoringRules):  # noqa: ANN201
    return default_scorer(rules)


# --- the mix -----------------------------------------------------------------


def test_default_mix_sums_to_one_and_is_short_heavy() -> None:
    assert sum(DEFAULT_MIX.shares.values()) == pytest.approx(1.0)
    assert set(DEFAULT_MIX.shares) == set(BUCKET_NAMES)
    assert DEFAULT_MIX.mass(SHORT) > DEFAULT_MIX.mass(LONG)


def test_mix_from_counts_rejects_empty() -> None:
    with pytest.raises(ValueError):
        DistanceMix.from_counts({})


def test_mix_spread_is_proportional_and_conserving() -> None:
    parts = DEFAULT_MIX.spread(10, ["50_59", "60p"])
    assert sum(parts.values()) == pytest.approx(10)
    assert parts["50_59"] / parts["60p"] == pytest.approx(
        DEFAULT_MIX.shares["50_59"] / DEFAULT_MIX.shares["60p"]
    )


# --- league bucket table + XP from scoring_settings ---------------------------


def test_league_k_values(rules: ScoringRules) -> None:
    assert [rules[f"fgm_{b}"] for b in BUCKET_NAMES] == [3, 3, 3, 4, 5, 6]
    assert rules["fgmiss"] == -1 and rules["xpm"] == 1 and rules["xpmiss"] == -1


# --- actuals with real splits score exactly (nflverse shape) ---------------------


def test_actuals_with_full_splits_score_exactly(scorer) -> None:  # noqa: ANN001
    # nflverse-shaped: exact buckets, redundant fgm_50p, totals, explicit misses
    line = {
        "fga": 6,
        "fgm": 5,
        "fgm_20_29": 1,
        "fgm_30_39": 1,
        "fgm_40_49": 1,
        "fgm_50_59": 1,
        "fgm_60p": 1,
        "fgm_50p": 2,
        "fgmiss": 1,
        "fgmiss_40_49": 1,
        "xpa": 3,
        "xpm": 2,
        "xpmiss": 1,
        "fgm_yds": 200,
    }
    # 3 + 3 + 4 + 5 + 6 - 1 + 2 - 1
    assert scorer.score(line, "K") == pytest.approx(21.0)
    norm = KickerNormalizer()(line)
    assert [norm[f"fgm_{b}"] for b in BUCKET_NAMES] == [0, 1, 1, 1, 1, 1]
    assert norm["fgm"] == 5 and norm["fgm_50p"] == 2 and norm["fgmiss"] == 1


def test_split_does_not_double_count_overlapping_keys() -> None:
    made = split_buckets(
        {"fgm": 3, "fgm_50p": 2, "fgm_50_59": 1, "fgm_60p": 1, "fgm_30_39": 1},
        "fgm",
        DEFAULT_MIX,
        impute_unobserved=True,
    )
    assert made == {"0_19": 0, "20_29": 0, "30_39": 1, "40_49": 0, "50_59": 1, "60p": 1}


# --- projections: coarse ranges split by the mix -------------------------------


def test_espn_shape_splits_coarse_ranges(scorer) -> None:  # noqa: ANN001
    line = {"fga": 12, "fgm": 10, "fgm_0_39": 6, "fgm_40_49": 3, "fgm_50p": 1, "xpa": 30, "xpm": 29}
    norm = KickerNormalizer()(line)
    assert sum(norm[f"fgm_{b}"] for b in SHORT) == pytest.approx(6)
    assert norm["fgm_40_49"] == 3
    assert norm["fgm_50_59"] + norm["fgm_60p"] == pytest.approx(1)
    assert norm["fgm"] == pytest.approx(10)
    assert norm["fgmiss"] == 2 and norm["xpmiss"] == 1
    long_pts = 5 * norm["fgm_50_59"] + 6 * norm["fgm_60p"]
    assert scorer.score(line, "K") == pytest.approx(6 * 3 + 3 * 4 + long_pts - 2 + 29 - 1)


def test_total_residual_spread_over_unspecified_buckets() -> None:
    made = split_buckets({"fgm": 5, "fgm_40_49": 2}, "fgm", DEFAULT_MIX, impute_unobserved=True)
    assert made["40_49"] == 2
    others = [b for b in BUCKET_NAMES if b != "40_49"]
    assert sum(made[b] for b in others) == pytest.approx(3)
    assert made["30_39"] > made["60p"]  # proportional to the mix


def test_finer_keys_win_over_total_when_they_disagree() -> None:
    # total says 4 but buckets already account for 6 → nothing negative, buckets kept
    made = split_buckets(
        {"fgm": 4, "fgm_0_39": 3, "fgm_40_49": 2, "fgm_50p": 1},
        "fgm",
        DEFAULT_MIX,
        impute_unobserved=True,
    )
    assert sum(made.values()) == pytest.approx(6)
    assert min(made.values()) >= 0


# --- Sleeper season shape: short range unobserved → imputed from the long range ----


def test_sleeper_season_shape_imputes_short_fgs(scorer) -> None:  # noqa: ANN001
    line = {
        "fgm_40_49": 9,
        "fgm_50p": 8,
        "fgmiss_40_49": 1,
        "fgmiss_50p": 3,
        "xpm": 42,
        "xpmiss": 2,
    }
    norm = KickerNormalizer()(line)
    short = sum(norm[f"fgm_{b}"] for b in SHORT)
    assert short == pytest.approx(17 * DEFAULT_MIX.mass(SHORT) / DEFAULT_MIX.mass(LONG))
    assert norm["fgm_40_49"] == 9 and norm["fgm_50_59"] + norm["fgm_60p"] == pytest.approx(8)
    assert norm["fgmiss"] == pytest.approx(4)  # misses are never imputed
    assert scorer.score(line, "K") > 150  # in line with ESPN's projection for the same kicker


def test_imputation_can_be_disabled() -> None:
    line = {"fgm_40_49": 9, "fgm_50p": 8}
    made = split_buckets(line, "fgm", DEFAULT_MIX, impute_unobserved=False)
    assert sum(made[b] for b in SHORT) == 0
    assert KickerNormalizer(impute_unobserved=False)(line)["fgm"] == pytest.approx(17)


def test_no_imputation_when_nothing_observed_or_total_present() -> None:
    assert sum(split_buckets({}, "fgm", DEFAULT_MIX, impute_unobserved=True).values()) == 0
    # a total covers everything → short buckets are observed-zero, not unobserved
    made = split_buckets({"fgm": 2, "fgm_40_49": 2}, "fgm", DEFAULT_MIX, impute_unobserved=True)
    assert sum(made[b] for b in SHORT) == 0


# --- misses / XPs ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "fgmiss", "xpmiss"),
    [
        ({"fgmiss": 2, "fga": 9, "fgm": 5, "xpmiss": 1, "xpa": 5, "xpm": 5}, 2, 1),  # explicit wins
        ({"fga": 5, "fgm": 3, "xpa": 4, "xpm": 3}, 2, 1),  # attempts − makes
        (
            {"fgmiss_40_49": 1, "fgmiss_50p": 1, "fgmiss_50_59": 1},
            2,
            None,
        ),  # bucketed, no dbl count
        ({"fgm": 3}, None, None),  # nothing to infer from
    ],
)
def test_miss_inference(line: dict, fgmiss: float | None, xpmiss: float | None) -> None:
    norm = KickerNormalizer()(line)
    assert norm.get("fgmiss") == (fgmiss if fgmiss is None else pytest.approx(fgmiss))
    assert norm.get("xpmiss") == (xpmiss if xpmiss is None else pytest.approx(xpmiss))


# --- wiring -----------------------------------------------------------------


def test_default_scorer_only_normalizes_kickers(rules: ScoringRules) -> None:
    s = default_scorer(rules)
    line = {"fgm_40_49": 9, "fgm_50p": 8}
    assert s.score(line, "K") > s.score(line, "QB") == score(line, rules) == pytest.approx(36.0)


def test_normalizer_preserves_unrelated_keys() -> None:
    norm = KickerNormalizer()({"fgm": 1, "fgm_yds": 45, "gp": 1, "pts_ppr": 4})
    assert norm["fgm_yds"] == 45 and norm["gp"] == 1 and norm["pts_ppr"] == 4
