"""Sleeper league-state snapshots → core.drafts / draft_picks / rosters / league_users (LS-16).

These are *current-state* tables, not vintages: every load upserts, so they can be re-run against
a live, changing draft as often as the poller likes. ``parse_*`` turn a payload into row dicts
(pure, unit-tested); ``load_*`` write them.

Picks use **sync** semantics: rows present in the payload are upserted by ``(draft_id, pick_no)``
and rows for that draft that are *absent* are deleted, so a commissioner "undo pick" converges
instead of leaving a ghost row. Sleeper's pick payload has no timestamp, so ``first_seen_at`` is
the ``pulled_at`` of the snapshot that first showed the pick (kept on conflict) — accurate to the
poll interval.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from lazy_sleeper.db.models import Draft, DraftPick, LeagueUser, Roster
from lazy_sleeper.ingest.loaders import _int_or_none, _str_or_none
from lazy_sleeper.ingest.validate import parse_json


def _bool_or_none(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


# --- parse -----------------------------------------------------------------


def parse_draft(payload: bytes) -> dict[str, Any]:
    d = parse_json(payload)
    if not isinstance(d, dict) or not d.get("draft_id"):
        raise ValueError("draft payload is not a draft object")
    settings = d.get("settings") if isinstance(d.get("settings"), dict) else {}
    return {
        "draft_id": str(d["draft_id"]),
        "league_id": _str_or_none(d.get("league_id")),
        "season": _int_or_none(d.get("season")),
        "type": _str_or_none(d.get("type")),
        "status": _str_or_none(d.get("status")),
        "start_time": _int_or_none(d.get("start_time")),
        "last_picked": _int_or_none(d.get("last_picked")),
        "rounds": _int_or_none(settings.get("rounds")),
        "teams": _int_or_none(settings.get("teams")),
        "pick_timer": _int_or_none(settings.get("pick_timer")),
        "settings": settings or None,
        "metadata_": d.get("metadata") if isinstance(d.get("metadata"), dict) else None,
        "slot_to_roster_id": d.get("slot_to_roster_id")
        if isinstance(d.get("slot_to_roster_id"), dict)
        else None,
        "draft_order": d.get("draft_order") if isinstance(d.get("draft_order"), dict) else None,
    }


def parse_picks(payload: bytes, draft_id: str) -> list[dict[str, Any]]:
    """Picks for one draft; rows belonging to another draft (shouldn't happen) are dropped."""
    data = parse_json(payload)
    if not isinstance(data, list):
        raise ValueError("picks payload is not a list")
    out: list[dict[str, Any]] = []
    for p in data:
        if not isinstance(p, dict):
            continue
        pick_no = _int_or_none(p.get("pick_no"))
        if pick_no is None or (p.get("draft_id") and str(p["draft_id"]) != draft_id):
            continue
        out.append(
            {
                "draft_id": draft_id,
                "pick_no": pick_no,
                "round": _int_or_none(p.get("round")),
                "draft_slot": _int_or_none(p.get("draft_slot")),
                "roster_id": _int_or_none(p.get("roster_id")),
                "picked_by": _str_or_none(p.get("picked_by")),  # "" on autopick → None
                "sleeper_id": _str_or_none(p.get("player_id")),
                "is_keeper": _bool_or_none(p.get("is_keeper")),
                "metadata_": p.get("metadata") if isinstance(p.get("metadata"), dict) else None,
            }
        )
    out.sort(key=lambda r: r["pick_no"])
    return out


def parse_rosters(payload: bytes) -> list[dict[str, Any]]:
    data = parse_json(payload)
    if not isinstance(data, list):
        raise ValueError("rosters payload is not a list")
    out: list[dict[str, Any]] = []
    for r in data:
        if not isinstance(r, dict) or _int_or_none(r.get("roster_id")) is None:
            continue
        out.append(
            {
                "league_id": str(r.get("league_id") or ""),
                "roster_id": int(r["roster_id"]),
                "owner_id": _str_or_none(r.get("owner_id")),
                "co_owners": r.get("co_owners"),
                "players": r.get("players"),
                "starters": r.get("starters"),
                "reserve": r.get("reserve"),
                "taxi": r.get("taxi"),
                "keepers": r.get("keepers"),
                "settings": r.get("settings") if isinstance(r.get("settings"), dict) else None,
            }
        )
    return out


def parse_users(payload: bytes) -> list[dict[str, Any]]:
    data = parse_json(payload)
    if not isinstance(data, list):
        raise ValueError("users payload is not a list")
    out: list[dict[str, Any]] = []
    for u in data:
        if not isinstance(u, dict) or not u.get("user_id"):
            continue
        meta = u.get("metadata") if isinstance(u.get("metadata"), dict) else {}
        out.append(
            {
                "league_id": str(u.get("league_id") or ""),
                "user_id": str(u["user_id"]),
                "display_name": _str_or_none(u.get("display_name")),
                "team_name": _str_or_none(meta.get("team_name")),
                "avatar": _str_or_none(u.get("avatar")),
                "is_owner": _bool_or_none(u.get("is_owner")),
            }
        )
    return out


# --- load ------------------------------------------------------------------


def _upsert(session: Session, model, rows: list[dict[str, Any]], pk: tuple[str, ...]) -> None:  # noqa: ANN001
    if not rows:
        return
    table = model.__table__
    # Dataclass-style keys use attribute names; `metadata_` maps to the `metadata` column.
    cols = {attr.key: attr.columns[0].name for attr in model.__mapper__.column_attrs}
    values = [{cols.get(k, k): v for k, v in row.items()} for row in rows]
    stmt = insert(table).values(values)
    pk_cols = [cols.get(k, k) for k in pk]
    keep = {*pk_cols, "first_seen_at"}
    stmt = stmt.on_conflict_do_update(
        index_elements=pk_cols,
        set_={c: getattr(stmt.excluded, c) for c in values[0] if c not in keep},
    )
    session.execute(stmt)


def _stamp(rows: list[dict[str, Any]], snapshot_id: int | None, now: datetime) -> None:
    for r in rows:
        r["snapshot_id"] = snapshot_id
        r["updated_at"] = now


def load_draft(session: Session, payload: bytes, snapshot_id: int | None) -> str:
    """Upsert the draft doc; returns its draft_id."""
    row = parse_draft(payload)
    _stamp([row], snapshot_id, datetime.now(UTC))
    _upsert(session, Draft, [row], ("draft_id",))
    return row["draft_id"]


def load_draft_picks(
    session: Session,
    payload: bytes,
    draft_id: str,
    snapshot_id: int | None,
    pulled_at: datetime | None = None,
) -> tuple[int, int]:
    """Sync one draft's picks with the payload. Returns (picks in payload, picks deleted)."""
    rows = parse_picks(payload, draft_id)
    now = datetime.now(UTC)
    _stamp(rows, snapshot_id, now)
    for r in rows:
        r["first_seen_at"] = pulled_at or now
    _upsert(session, DraftPick, rows, ("draft_id", "pick_no"))
    stmt = delete(DraftPick).where(DraftPick.draft_id == draft_id)
    if rows:
        stmt = stmt.where(DraftPick.pick_no.not_in([r["pick_no"] for r in rows]))
    deleted = session.execute(stmt).rowcount or 0
    return len(rows), deleted


def load_rosters(session: Session, payload: bytes, snapshot_id: int | None) -> int:
    rows = parse_rosters(payload)
    _stamp(rows, snapshot_id, datetime.now(UTC))
    _upsert(session, Roster, rows, ("league_id", "roster_id"))
    return len(rows)


def load_league_users(session: Session, payload: bytes, snapshot_id: int | None) -> int:
    rows = parse_users(payload)
    _stamp(rows, snapshot_id, datetime.now(UTC))
    _upsert(session, LeagueUser, rows, ("league_id", "user_id"))
    return len(rows)
