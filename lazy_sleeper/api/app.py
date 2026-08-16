"""FastAPI application. M0: health + snapshot inventory. Board/draft endpoints arrive in M3/M4."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session

from lazy_sleeper.config import Settings, get_settings
from lazy_sleeper.db.models import Snapshot
from lazy_sleeper.db.session import make_engine, make_session_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings)
    sessions = make_session_factory(engine)

    def get_session() -> Iterator[Session]:
        s = sessions()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI(title="Lazy Sleeper API", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/snapshots")
    def snapshots(
        limit: int = 50,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> list[dict[str, Any]]:
        rows = session.scalars(
            select(Snapshot).order_by(Snapshot.pulled_at.desc()).limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "source": r.source,
                "kind": r.kind,
                "season": r.season,
                "week": r.week,
                "pulled_at": r.pulled_at,
                "record_count": r.record_count,
                "valid": r.valid,
                "byte_size": r.byte_size,
            }
            for r in rows
        ]

    return app


app = create_app()
