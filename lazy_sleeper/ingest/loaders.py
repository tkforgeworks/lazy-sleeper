"""Snapshot payload → core.* tables. Idempotent upserts keyed on sleeper_id."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from lazy_sleeper.db.models import Crosswalk, Player
from lazy_sleeper.ingest.validate import parse_json

_PLAYER_FIELDS = (
    "full_name",
    "position",
    "team",
    "status",
    "injury_status",
    "depth_chart_order",
    "search_rank",
    "years_exp",
    "age",
    "team_changed_at",
    "sportradar_id",
    "espn_id",
    "gsis_id",
    "yahoo_id",
    "active",
)


_NULL_TOKENS = frozenset({"", "NA", "N/A", "null", "None"})


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return None if s in _NULL_TOKENS else s


def _int_or_none(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def load_players(
    session: Session, payload: bytes, snapshot_id: int | None, *, batch: int = 1000
) -> int:
    """Upsert every player in a Sleeper players payload. Returns rows written."""
    data: dict[str, dict[str, Any]] = parse_json(payload)
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for pid, p in data.items():
        if not isinstance(p, dict):
            continue
        full_name = p.get("full_name") or " ".join(
            x for x in (p.get("first_name"), p.get("last_name")) if x
        )
        rows.append(
            {
                "sleeper_id": str(pid),
                "full_name": _str_or_none(full_name),
                "position": _str_or_none(p.get("position")),
                "team": _str_or_none(p.get("team")),
                "status": _str_or_none(p.get("status")),
                "injury_status": _str_or_none(p.get("injury_status")),
                "depth_chart_order": _int_or_none(p.get("depth_chart_order")),
                "search_rank": _int_or_none(p.get("search_rank")),
                "years_exp": _int_or_none(p.get("years_exp")),
                "age": _int_or_none(p.get("age")),
                "team_changed_at": _int_or_none(p.get("team_changed_at")),
                "sportradar_id": _str_or_none(p.get("sportradar_id")),
                "espn_id": _str_or_none(p.get("espn_id")),
                "gsis_id": _str_or_none(p.get("gsis_id")),
                "yahoo_id": _str_or_none(p.get("yahoo_id")),
                "active": p.get("active") if isinstance(p.get("active"), bool) else None,
                "snapshot_id": snapshot_id,
                "updated_at": now,
            }
        )
    _upsert(
        session,
        Player.__table__,
        rows,
        "sleeper_id",
        (*_PLAYER_FIELDS, "snapshot_id", "updated_at"),
        batch,
    )
    return len(rows)


def load_crosswalk(
    session: Session, payload: bytes, snapshot_id: int | None, *, batch: int = 1000
) -> int:
    """Upsert dynastyprocess db_playerids.csv rows that carry a sleeper_id."""
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8", errors="replace")))
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for r in reader:
        sid = _str_or_none(r.get("sleeper_id"))
        if not sid:
            continue
        rows.append(
            {
                "sleeper_id": sid,
                "sportradar_id": _str_or_none(r.get("sportradar_id")),
                "gsis_id": _str_or_none(r.get("gsis_id")),
                "espn_id": _str_or_none(r.get("espn_id")),
                "pfr_id": _str_or_none(r.get("pfr_id")),
                "mfl_id": _str_or_none(r.get("mfl_id")),
                "name": _str_or_none(r.get("name")),
                "merge_name": _str_or_none(r.get("merge_name")),
                "position": _str_or_none(r.get("position")),
                "snapshot_id": snapshot_id,
                "loaded_at": now,
            }
        )
    rows = _dedupe_by_pk(rows, "sleeper_id")
    cols = tuple(k for k in rows[0] if k != "sleeper_id") if rows else ()
    _upsert(session, Crosswalk.__table__, rows, "sleeper_id", cols, batch)
    return len(rows)


def _dedupe_by_pk(rows: list[dict[str, Any]], pk: str) -> list[dict[str, Any]]:
    """The crosswalk CSV occasionally repeats a sleeper_id; keep the row with most ids filled."""
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = r[pk]
        cur = best.get(k)
        if cur is None or _filled(r) > _filled(cur):
            best[k] = r
    return list(best.values())


def _filled(r: dict[str, Any]) -> int:
    return sum(1 for v in r.values() if v is not None)


def _upsert(
    session: Session,
    table,
    rows: list[dict[str, Any]],
    pk: str,
    update_cols: tuple[str, ...],
    batch: int,
) -> None:  # noqa: ANN001
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        stmt = insert(table).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[pk],
            set_={c: getattr(stmt.excluded, c) for c in update_cols},
        )
        session.execute(stmt)
