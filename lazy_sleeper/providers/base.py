"""Provider contract: anything that can hand back league-scored projections for a horizon."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

SEASON = "season"
WEEKLY = "weekly"


def horizon_for(week: int | None) -> str:
    return SEASON if week is None else WEEKLY


@dataclass(frozen=True)
class PlayerProjection:
    """One player's projection for a horizon, already scored under the league's rules.

    ``components`` (ensemble only) keeps each member provider's points so downstream code can
    flag disagreement without re-querying; ``provider_points`` is the source's own PPR number
    when it has one (x-check only, never used for ranking).
    """

    sleeper_id: str
    position: str | None
    team: str | None
    points: float
    source: str
    provider_points: float | None = None
    components: dict[str, float] = field(default_factory=dict)


class ProjectionProvider(Protocol):
    """Scored projections for (season, week); ``week`` None = season totals."""

    @property
    def name(self) -> str: ...

    def projections(self, season: int, week: int | None = None) -> list[PlayerProjection]: ...
