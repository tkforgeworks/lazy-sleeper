"""Engine configuration (LS-69): recycle, keepalives and timeouts so a silently dropped pooled
connection fails fast instead of hanging every DB-touching request for ~15 minutes."""

from __future__ import annotations

from lazy_sleeper.config import Settings
from lazy_sleeper.db.session import engine_kwargs, make_engine, statement_timeout_sql

PG = "postgresql+psycopg://u:p@db.example:5432/x"


def test_postgres_engine_gets_recycle_keepalives_and_timeouts() -> None:
    s = Settings(database_url=PG)
    kw = engine_kwargs(s)
    assert kw["pool_pre_ping"] is True and kw["pool_recycle"] == 300
    ca = kw["connect_args"]
    assert ca["connect_timeout"] == 10
    assert ca["keepalives"] == 1 and ca["keepalives_idle"] == 30
    assert ca["keepalives_interval"] == 10 and ca["keepalives_count"] == 3
    # the statement timeout is a per-connection SET (Supavisor drops the `options` startup param)
    assert "options" not in ca
    assert statement_timeout_sql(s) == "SET statement_timeout = 30000"


def test_engine_settings_are_tunable_and_statement_timeout_can_be_disabled() -> None:
    s = Settings(
        database_url=PG,
        db_pool_recycle_s=60,
        db_connect_timeout_s=3,
        db_keepalives_idle_s=5,
        db_keepalives_interval_s=2,
        db_keepalives_count=9,
        db_statement_timeout_ms=0,
    )
    kw = engine_kwargs(s)
    assert kw["pool_recycle"] == 60
    ca = kw["connect_args"]
    assert (ca["connect_timeout"], ca["keepalives_idle"]) == (3, 5)
    assert (ca["keepalives_interval"], ca["keepalives_count"]) == (2, 9)
    assert statement_timeout_sql(s) is None


def test_non_postgres_urls_get_pool_settings_only() -> None:
    s = Settings(database_url="sqlite://")
    kw = engine_kwargs(s)
    assert "connect_args" not in kw and kw["pool_recycle"] == 300
    assert statement_timeout_sql(s) is None
    eng = make_engine(s)  # libpq-only connect args / SET would make SQLite refuse to connect
    with eng.connect() as c:
        assert c.exec_driver_sql("select 1").scalar() == 1


def test_make_engine_applies_the_postgres_settings_to_the_pool() -> None:
    eng = make_engine(Settings(database_url=PG))
    assert eng.pool._recycle == 300  # noqa: SLF001 — the only place SQLAlchemy exposes it
    assert eng.pool._pre_ping is True  # noqa: SLF001
