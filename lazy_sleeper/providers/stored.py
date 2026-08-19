"""Providers backed by the stored projection vintages in ``core.projections``."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from lazy_sleeper.providers.base import PlayerProjection
from lazy_sleeper.scoring import Scorer


class StoredProvider:
    """Latest stored vintage of one external source (``sleeper`` / ``espn``), league-scored.

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

    def latest_snapshot_id(self, season: int, week: int | None = None) -> int | None:
        from lazy_sleeper.db.models import Projection

        return self._session.scalar(
            select(func.max(Projection.snapshot_id)).where(
                Projection.source == self._source,
                Projection.season == season,
                Projection.week.is_(None) if week is None else Projection.week == week,
            )
        )

    def projections(self, season: int, week: int | None = None) -> list[PlayerProjection]:
        from lazy_sleeper.db.models import Projection

        snap_id = self.latest_snapshot_id(season, week)
        if snap_id is None:
            return []
        rows = self._session.execute(
            select(
                Projection.sleeper_id,
                Projection.position,
                Projection.team,
                Projection.stats,
                Projection.provider_points,
            ).where(
                Projection.snapshot_id == snap_id,
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
