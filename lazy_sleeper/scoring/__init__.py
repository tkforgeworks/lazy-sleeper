"""Scoring engine — league `scoring_settings` applied to stat lines (LS-18)."""

from lazy_sleeper.scoring.engine import Scored, Scorer, breakdown, score
from lazy_sleeper.scoring.league import load_league_rules
from lazy_sleeper.scoring.rules import OFFENSE_POSITIONS, PRESCORED_KEYS, ScoringRules

__all__ = [
    "OFFENSE_POSITIONS",
    "PRESCORED_KEYS",
    "Scored",
    "Scorer",
    "ScoringRules",
    "breakdown",
    "load_league_rules",
    "score",
]
