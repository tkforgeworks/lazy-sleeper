"""EnsembleProvider — weighted blend of member providers, per position, per horizon."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from lazy_sleeper.providers.base import PlayerProjection, ProjectionProvider, horizon_for
from lazy_sleeper.providers.weights import ResolvedWeights, normalize


class WeightSource(Protocol):
    def resolve_all(self, horizon: str) -> Mapping[str, ResolvedWeights]: ...


class StaticWeights:
    """In-memory WeightSource for tests / one-offs: {horizon: {position: {provider: w}}}."""

    def __init__(self, table: Mapping[str, Mapping[str, Mapping[str, float]]]) -> None:
        self._table = table

    def resolve_all(self, horizon: str) -> dict[str, ResolvedWeights]:
        return {
            pos: ResolvedWeights(horizon, pos, normalize(w), "static", None)
            for pos, w in self._table.get(horizon, {}).items()
        }


class EnsembleProvider:
    """Blend of member providers.

    For each player (union across members) the position's weights are renormalized over the
    members that actually projected them — a rookie only one feed carries gets that feed at
    100 % (the spec's consensus fallback). Positions with no stored weights split equally.
    ``components`` on each result keeps the member points for disagreement flags.
    """

    def __init__(self, members: Sequence[ProjectionProvider], weights: WeightSource) -> None:
        if not members:
            raise ValueError("EnsembleProvider needs at least one member provider")
        self._members = list(members)
        self._weights = weights

    @property
    def name(self) -> str:
        return "ensemble"

    @property
    def members(self) -> list[ProjectionProvider]:
        return list(self._members)

    def projections(self, season: int, week: int | None = None) -> list[PlayerProjection]:
        weights = self._weights.resolve_all(horizon_for(week))
        by_player: dict[str, dict[str, PlayerProjection]] = {}
        for m in self._members:
            for p in m.projections(season, week):
                by_player.setdefault(p.sleeper_id, {})[m.name] = p
        return [self._blend(sid, parts, weights) for sid, parts in by_player.items()]

    def _blend(
        self,
        sleeper_id: str,
        parts: Mapping[str, PlayerProjection],
        weights: Mapping[str, ResolvedWeights],
    ) -> PlayerProjection:
        # position/team: majority-free — take the first member's (they agree in practice; the
        # crosswalk audit catches the exceptions)
        first = next(iter(parts.values()))
        position = first.position
        resolved = weights.get(position or "")
        raw = {name: (resolved.weights.get(name, 0.0) if resolved else 1.0) for name in parts}
        w = normalize(raw) or normalize(dict.fromkeys(parts, 1.0))
        points = sum(w[name] * parts[name].points for name in w)
        return PlayerProjection(
            sleeper_id=sleeper_id,
            position=position,
            team=first.team,
            points=points,
            source=self.name,
            provider_points=None,
            components={name: p.points for name, p in parts.items()},
        )
