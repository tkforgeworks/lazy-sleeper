"""Parity vs nflverse `fantasy_points_ppr` on every 2025 weekly offense actual (LS-21).

Fixture: `nflverse_actuals_2025_weekly.json.gz` — all 5,712 QB/RB/WR/TE weekly rows from
`core.actuals` (nflverse, 2025), stats trimmed to scored keys. Regenerate with
`lazy score parity --write-fixture tests/fixtures/nflverse_actuals_2025_weekly.json.gz`.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from lazy_sleeper.scoring import ScoringRules, default_scorer
from lazy_sleeper.scoring.league import NFLVERSE_PPR_DIFFS
from lazy_sleeper.scoring.parity import nflverse_adjustment, parity

FIXTURE = Path(__file__).parent / "fixtures" / "nflverse_actuals_2025_weekly.json.gz"


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return json.loads(gzip.decompress(FIXTURE.read_bytes()))


@pytest.fixture
def rules(sleeper_league_payload: dict) -> ScoringRules:
    return ScoringRules.from_league(sleeper_league_payload)


def test_fixture_covers_the_full_2025_offense_season(rows: list[dict]) -> None:
    assert len(rows) == 5712
    assert {r["position"] for r in rows} == {"QB", "RB", "WR", "TE"}
    assert {r["week"] for r in rows} == set(range(1, 19))
    assert all(r["provider_points"] is not None for r in rows)


def test_adjustment_only_touches_known_differences(rules: ScoringRules) -> None:
    assert set(NFLVERSE_PPR_DIFFS) == {"pass_int", "fum_rec_td", "fum_lost"}
    # INT: league −1, nflverse −2 → subtract +1 per INT to land on nflverse
    assert nflverse_adjustment({"pass_int": 2}, rules) == pytest.approx(2.0)
    # fumble-recovery TD: league +6, nflverse 0
    assert nflverse_adjustment({"fum_rec_td": 1}, rules) == pytest.approx(6.0)
    assert nflverse_adjustment({"rec": 9, "rec_yd": 120, "rec_td": 2}, rules) == 0.0


def test_engine_matches_nflverse_ppr_within_tolerance(
    rows: list[dict], rules: ScoringRules
) -> None:
    rep = parity(rows, default_scorer(rules))
    assert rep.n == len(rows)
    assert rep.mean_abs_delta <= 0.05, rep.mean_by_position()
    # and, position by position, nothing is quietly off
    for pos, mean in rep.mean_by_position().items():
        assert mean <= 0.05, (pos, mean)


def test_every_residual_is_a_return_fumble(rows: list[dict], rules: ScoringRules) -> None:
    """The only rows off by more than rounding are fumbles lost on kick/punt returns.

    nflverse's formula charges only sack/rush/receiving fumbles; the league's `fum_lost` charges all
    of them. Each such row is off by exactly one −2 and had a fumble lost — nothing else leaks.
    """
    rep = parity(rows, default_scorer(rules))
    assert rep.outliers, "expected the known return-fumble residuals to be present"
    for o in rep.outliers:
        assert o["delta"] == pytest.approx(-2.0), o
        assert o["stats"].get("fum_lost", 0) >= 1, o
    assert len(rep.outliers) < 0.01 * rep.n  # well under 1% of rows


def test_league_and_nflverse_agree_exactly_on_clean_lines(
    rows: list[dict], rules: ScoringRules
) -> None:
    """Rows without INTs / fumbles / FR-TDs need no adjustment at all and match to the cent."""
    scorer = default_scorer(rules)
    clean = [
        r
        for r in rows
        if not any(r["stats"].get(k) for k in ("pass_int", "fum_lost", "fum_rec_td", "fum"))
    ]
    assert len(clean) > 4000
    worst = max(abs(scorer.score(r["stats"], r["position"]) - r["provider_points"]) for r in clean)
    assert worst < 0.011  # nflverse rounds to 2 dp
