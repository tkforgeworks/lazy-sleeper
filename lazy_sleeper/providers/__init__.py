"""Projection providers (E4/LS-25): stored Sleeper/ESPN vintages and their weighted ensemble."""

from lazy_sleeper.providers.base import (
    SEASON,
    WEEKLY,
    PlayerProjection,
    ProjectionProvider,
    horizon_for,
)
from lazy_sleeper.providers.ensemble import EnsembleProvider, StaticWeights, WeightSource
from lazy_sleeper.providers.stored import EspnProvider, SleeperProvider, StoredProvider
from lazy_sleeper.providers.weights import (
    FittedWeight,
    ResolvedWeights,
    WeightRepository,
    fit_from_csvs,
    fit_weights,
    normalize,
    to_json,
)


def make_provider(session, scorer, name: str) -> ProjectionProvider:  # noqa: ANN001
    """`sleeper` | `espn` | `ensemble` — league-scored projections from stored vintages.

    The one place provider names are resolved; CLI and API both wire through here.
    """
    if name == "sleeper":
        return SleeperProvider(session, scorer)
    if name == "espn":
        return EspnProvider(session, scorer)
    if name == "ensemble":
        return EnsembleProvider(
            [SleeperProvider(session, scorer), EspnProvider(session, scorer)],
            WeightRepository(session),
        )
    raise ValueError(f"unknown provider {name!r} (sleeper | espn | ensemble)")


__all__ = [
    "SEASON",
    "WEEKLY",
    "EnsembleProvider",
    "EspnProvider",
    "FittedWeight",
    "PlayerProjection",
    "ProjectionProvider",
    "ResolvedWeights",
    "SleeperProvider",
    "StaticWeights",
    "StoredProvider",
    "WeightRepository",
    "WeightSource",
    "fit_from_csvs",
    "fit_weights",
    "horizon_for",
    "make_provider",
    "normalize",
    "to_json",
]
