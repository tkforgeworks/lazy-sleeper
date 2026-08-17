"""Scoring engine — league `scoring_settings` applied to stat lines (LS-18, LS-19)."""

from lazy_sleeper.scoring.engine import Scored, Scorer, breakdown, score
from lazy_sleeper.scoring.kicking import DEFAULT_MIX, DistanceMix, KickerNormalizer
from lazy_sleeper.scoring.league import distance_mix_from_actuals, load_league_rules
from lazy_sleeper.scoring.rules import OFFENSE_POSITIONS, PRESCORED_KEYS, ScoringRules


def default_scorer(rules: ScoringRules, *, k_mix: DistanceMix = DEFAULT_MIX) -> Scorer:
    """Scorer with the standard per-position normalizers wired (K distance mix; DEF in LS-20)."""
    return Scorer(rules, normalizers={"K": KickerNormalizer(k_mix)})


__all__ = [
    "DEFAULT_MIX",
    "OFFENSE_POSITIONS",
    "PRESCORED_KEYS",
    "DistanceMix",
    "KickerNormalizer",
    "Scored",
    "Scorer",
    "ScoringRules",
    "breakdown",
    "default_scorer",
    "distance_mix_from_actuals",
    "load_league_rules",
    "score",
]
