"""Guarded upserts: unchanged rows must not be rewritten (2026-09-03 Supabase IO stall)."""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from lazy_sleeper.db.models import Player
from lazy_sleeper.ingest.loaders import (
    _PLAYER_FIELDS,
    _upsert_stmt,
    load_crosswalk,
    load_players,
)


def _compile(stmt) -> str:  # noqa: ANN001
    return str(stmt.compile(dialect=postgresql.dialect()))


def _where(sql: str) -> str:
    assert " WHERE " in sql, f"no WHERE guard in: {sql[-200:]}"
    return sql.split(" WHERE ", 1)[1]


class _Recorder:
    """Stands in for a Session; captures the statements a loader would execute."""

    def __init__(self) -> None:
        self.stmts: list = []

    def execute(self, stmt) -> None:  # noqa: ANN001
        self.stmts.append(stmt)


def _player_chunk() -> list[dict]:
    return [
        {"sleeper_id": "1", **dict.fromkeys(_PLAYER_FIELDS), "snapshot_id": 1, "updated_at": None}
    ]


def test_guarded_upsert_compares_data_columns_not_bookkeeping() -> None:
    stmt = _upsert_stmt(
        Player.__table__,
        _player_chunk(),
        "sleeper_id",
        (*_PLAYER_FIELDS, "snapshot_id", "updated_at"),
        _PLAYER_FIELDS,
    )
    where = _where(_compile(stmt))
    assert "IS DISTINCT FROM" in where
    assert "injury_status" in where and "search_rank" in where
    # A no-change day must rewrite nothing: bookkeeping columns can't trip the guard.
    assert "updated_at" not in where
    assert "snapshot_id" not in where


def test_unguarded_upsert_keeps_old_behavior() -> None:
    stmt = _upsert_stmt(
        Player.__table__,
        _player_chunk(),
        "sleeper_id",
        (*_PLAYER_FIELDS, "snapshot_id", "updated_at"),
        None,
    )
    assert " WHERE " not in _compile(stmt)


def test_load_players_emits_guarded_upsert(sleeper_players_payload: bytes) -> None:
    rec = _Recorder()
    n = load_players(rec, sleeper_players_payload, snapshot_id=7)
    assert n > 0 and rec.stmts
    where = _where(_compile(rec.stmts[0]))
    assert "IS DISTINCT FROM" in where and "updated_at" not in where


def test_load_crosswalk_emits_guarded_upsert() -> None:
    rec = _Recorder()
    payload = b"sleeper_id,name,position\n1,Foo Bar,QB\n"
    n = load_crosswalk(rec, payload, snapshot_id=7)
    assert n == 1 and rec.stmts
    where = _where(_compile(rec.stmts[0]))
    assert "IS DISTINCT FROM" in where
    assert "loaded_at" not in where and "snapshot_id" not in where
