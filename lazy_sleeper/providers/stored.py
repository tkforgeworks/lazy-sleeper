"""Providers backed by the stored projection vintages in ``core.projections``."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from lazy_sleeper.providers.base import PlayerProjection
from lazy_sleeper.scoring import Scorer


class StoredProvider:
    """The stored projections of one external source (``sleeper`` / ``espn``), league-scored.
    Since LS-53 ``core.projections`` holds one current row per (source, player, season, week),
    so "latest vintage" is simply the table.

    One row per resolved ``sleeper_id``; rows without a resolved id are dropped (they can't
    join anything downstream). If two source rows resolve to the same player the higher-scoring
    one wins — rare, and the audit (`lazy check joins`) reports duplicates separately.
    """

    def __init__(self, session: Session, scorer: Scorer, source: str) -> None:
        self._session = session
        self._scorer = scorer
        self._source = source

    @property
    def name(self) -> str:
        return self._source

    def projections(self, season: int, week: int | None = None) -> list[PlayerProjection]:
        from lazy_sleeper.db.models import Projection

        rows = self._session.execute(
            select(
                Projection.sleeper_id,
                Projection.position,
                Projection.team,
                Projection.stats,
                Projection.provider_points,
            ).where(
                Projection.source == self._source,
                Projection.season == season,
                Projection.week.is_(None) if week is None else Projection.week == week,
                Projection.sleeper_id.is_not(None),
            )
        )
        best: dict[str, PlayerProjection] = {}
        for sleeper_id, position, team, stats, provider_points in rows:
            pts = self._scorer.score(stats, position)
            cur = best.get(sleeper_id)
            if cur is None or pts > cur.points:
                best[sleeper_id] = PlayerProjection(
                    sleeper_id, position, team, pts, self._source, provider_points
                )
        return list(best.values())


class SleeperProvider(StoredProvider):
    def __init__(self, session: Session, scorer: Scorer) -> None:
        super().__init__(session, scorer, "sleeper")


class EspnProvider(StoredProvider):
    def __init__(self, session: Session, scorer: Scorer) -> None:
        super().__init__(session, scorer, "espn")
