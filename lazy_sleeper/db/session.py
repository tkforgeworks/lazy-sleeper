"""Engine/session factory. Constructor-injected everywhere else; no module-level engine.

LS-69: the engine is built with a pool recycle, TCP keepalives and connect/statement timeouts
(all on ``Settings``). Without them a pooled connection whose far end (Supavisor) has silently
gone away hangs ``pool_pre_ping`` until the OS gives up on retransmits — ~15 minutes on Windows —
and every DB-touching request queues behind it. Only the Postgres dialect gets the ``connect_args``
(they are libpq parameters); anything else (SQLite in tests) gets the pool settings only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from lazy_sleeper.config import Settings


def is_postgres(settings: Settings) -> bool:
    return settings.database_url.startswith("postgresql")


def engine_kwargs(settings: Settings) -> dict[str, Any]:
    """``create_engine`` keyword arguments for ``settings.database_url``."""
    kw: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_recycle": settings.db_pool_recycle_s,
        "future": True,
    }
    if is_postgres(settings):
        kw["connect_args"] = {
            "connect_timeout": settings.db_connect_timeout_s,
            "keepalives": 1,
            "keepalives_idle": settings.db_keepalives_idle_s,
            "keepalives_interval": settings.db_keepalives_interval_s,
            "keepalives_count": settings.db_keepalives_count,
        }
    return kw


def statement_timeout_sql(settings: Settings) -> str | None:
    """Run on every new pooled connection. A ``SET`` rather than the ``options`` startup
    parameter: Supavisor (Supabase's pooler) silently drops the latter — verified 2026-08-28,
    the session kept the project default of 2 min — but honours a ``SET`` for the session."""
    if not is_postgres(settings) or settings.db_statement_timeout_ms <= 0:
        return None
    return f"SET statement_timeout = {int(settings.db_statement_timeout_ms)}"


def make_engine(settings: Settings) -> Engine:
    engine = create_engine(settings.database_url, **engine_kwargs(settings))
    sql = statement_timeout_sql(settings)
    if sql is not None:

        @event.listens_for(engine, "connect")
        def _set_statement_timeout(dbapi_conn, _record) -> None:  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute(sql)
            cur.close()
            dbapi_conn.commit()  # SET is transactional: the pool's rollback-on-return would undo it

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
