"""League scoring rules — the literal Sleeper `scoring_settings` map, nothing more.

Sleeper scores a stat line as `sum(scoring_settings[k] * stats[k])` over the keys present in both.
We do exactly that, driven entirely by the league payload, so the engine works for any Sleeper
league. Provider-computed points (`pts_ppr`, `pts_std`, ...) are never scoring keys and are
therefore never counted; `ScoringRules` also refuses them explicitly as a guardrail.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Provider-computed fantasy points that must never be treated as a scoring weight or a stat.
PRESCORED_KEYS: frozenset[str] = frozenset({"pts_ppr", "pts_half_ppr", "pts_std"})

# Positions covered by the generic offense path (LS-18). K and DEF get pre-scoring
# normalizers in LS-19 / LS-20 (distance mix, points-allowed buckets).
OFFENSE_POSITIONS: frozenset[str] = frozenset({"QB", "RB", "WR", "TE"})


@dataclass(frozen=True)
class ScoringRules:
    """Immutable stat-key → points-per-unit map, plus roster shape from the same league payload."""

    weights: Mapping[str, float]
    roster_positions: tuple[str, ...] = ()
    total_rosters: int | None = None
    league_id: str | None = None
    league_name: str | None = None

    def __post_init__(self) -> None:
        clean: dict[str, float] = {}
        for k, v in self.weights.items():
            if k in PRESCORED_KEYS:
                raise ValueError(f"{k!r} is a pre-scored total, not a scoring rule")
            if v is None:
                continue
            clean[str(k)] = float(v)
        object.__setattr__(self, "weights", clean)

    @classmethod
    def from_league(cls, payload: Mapping[str, Any]) -> ScoringRules:
        """Build from a Sleeper `/league/{id}` payload (or a snapshot of one)."""
        settings = payload.get("scoring_settings")
        if not isinstance(settings, Mapping) or not settings:
            raise ValueError("league payload has no scoring_settings")
        total = payload.get("total_rosters")
        return cls(
            weights=settings,
            roster_positions=tuple(payload.get("roster_positions") or ()),
            total_rosters=int(total) if total else None,
            league_id=payload.get("league_id"),
            league_name=payload.get("name"),
        )

    def weight(self, key: str) -> float:
        """Points per unit of `key`; 0.0 when the league doesn't score it."""
        return self.weights.get(key, 0.0)

    def __getitem__(self, key: str) -> float:
        return self.weights[key]

    def __contains__(self, key: object) -> bool:
        return key in self.weights
