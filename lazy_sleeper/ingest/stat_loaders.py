"""Snapshot payloads → core.stat_lines / core.adp.

Pure transforms (`sleeper_stat_rows`, `espn_stat_rows`) are separated from the DB writes so they
can be unit-tested on fixtures without a database.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from lazy_sleeper.db.models import Adp, Crosswalk, Player, Snapshot, StatLine
from lazy_sleeper.ingest.espn_stats import (
    POSITIONS,
    SOURCE_ACTUAL,
    SOURCE_PROJ,
    SPLIT_SEASON,
    TEAMS,
    decode_stats,
)
from lazy_sleeper.ingest.validate import parse_json

log = logging.getLogger(__name__)

_ADP_FIELDS = (
    "adp_ppr",
    "adp_half_ppr",
    "adp_std",
    "adp_2qb",
    "adp_dynasty",
    "adp_dynasty_ppr",
    "adp_rookie",
    "adp_idp",
)
_SLEEPER_KIND_CATEGORY = {
    "projections_season": "proj",
    "projections_week": "proj",
    "stats_season": "actual",
    "stats_week": "actual",
}


# --- identity resolution ------------------------------------------------------------------------
@dataclass
class SleeperIdResolver:
    """espn_id → sleeper_id. Crosswalk is authoritative; core.players.espn_id fills gaps."""

    espn_to_sleeper: dict[str, str] = field(default_factory=dict)
    unresolved: set[str] = field(default_factory=set)

    @classmethod
    def from_session(cls, session: Session) -> SleeperIdResolver:
        m: dict[str, str] = {}
        for espn_id, sid in session.execute(
            select(Player.espn_id, Player.sleeper_id).where(Player.espn_id.is_not(None))
        ):
            m[str(espn_id)] = sid
        for espn_id, sid in session.execute(
            select(Crosswalk.espn_id, Crosswalk.sleeper_id).where(Crosswalk.espn_id.is_not(None))
        ):
            m[str(espn_id)] = sid  # crosswalk wins
        return cls(m)

    def resolve(self, espn_id: str, position: str | None, pro_team_id: int | None) -> str | None:
        if position == "DEF":
            return TEAMS.get(pro_team_id or -1)
        sid = self.espn_to_sleeper.get(espn_id)
        if sid is None:
            self.unresolved.add(espn_id)
        return sid


# --- pure transforms ----------------------------------------------------------------------------
def sleeper_stat_rows(
    payload: bytes, snapshot: Snapshot
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Sleeper projections/stats payload → (stat_line rows, adp rows)."""
    category = _SLEEPER_KIND_CATEGORY.get(snapshot.kind)
    if category is None:
        raise ValueError(f"not a Sleeper stat snapshot kind: {snapshot.kind}")
    entries = parse_json(payload)
    stat_rows: list[dict[str, Any]] = []
    adp_rows: list[dict[str, Any]] = []
    for e in entries:
        stats: dict[str, Any] = e.get("stats") or {}
        player = e.get("player") or {}
        pid = str(e["player_id"])
        season = int(e.get("season") or snapshot.season)
        week = e.get("week")
        week = int(week) if week is not None else None
        position = player.get("position") or ("DEF" if pid.isalpha() else None)
        core_stats = {
            k: float(v)
            for k, v in stats.items()
            if not k.startswith("adp_") and not k.startswith("pts_") and k != "gp" and v is not None
        }
        stat_rows.append(
            {
                "snapshot_id": snapshot.id,
                "source": "sleeper",
                "category": category,
                "season": season,
                "week": week,
                "source_player_id": pid,
                "sleeper_id": pid,
                "position": position,
                "team": e.get("team"),
                "gp": _f(stats.get("gp")),
                "provider_points": _f(stats.get("pts_ppr")),
                "stats": core_stats,
            }
        )
        if week is None and any(stats.get(k) is not None for k in _ADP_FIELDS):
            adp_rows.append(
                {
                    "snapshot_id": snapshot.id,
                    "season": season,
                    "sleeper_id": pid,
                    "position": position,
                    **{k: _f(stats.get(k)) for k in _ADP_FIELDS},
                }
            )
    return stat_rows, adp_rows


def espn_stat_rows(
    payload: bytes, snapshot: Snapshot, resolver: SleeperIdResolver
) -> list[dict[str, Any]]:
    """ESPN kona payload → stat_line rows (season + weekly, proj + actual, all seasons present)."""
    data = parse_json(payload)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, int | None]] = set()
    for entry in data.get("players", []):
        p = entry.get("player") or {}
        espn_id = str(p.get("id"))
        position = POSITIONS.get(p.get("defaultPositionId"))
        pro_team_id = p.get("proTeamId")
        team = TEAMS.get(pro_team_id) if pro_team_id else None
        sleeper_id = resolver.resolve(espn_id, position, pro_team_id)
        for s in p.get("stats") or []:
            src, split = s.get("statSourceId"), s.get("statSplitTypeId")
            if src not in (SOURCE_ACTUAL, SOURCE_PROJ):
                continue
            category = "actual" if src == SOURCE_ACTUAL else "proj"
            season = int(s.get("seasonId"))
            week = None if split == SPLIT_SEASON else int(s.get("scoringPeriodId"))
            key = (espn_id, category, season, week)
            if key in seen:
                continue
            seen.add(key)
            stats = decode_stats(s.get("stats") or {})
            gp = stats.pop("gp", None)
            rows.append(
                {
                    "snapshot_id": snapshot.id,
                    "source": "espn",
                    "category": category,
                    "season": season,
                    "week": week,
                    "source_player_id": espn_id,
                    "sleeper_id": sleeper_id,
                    "position": position,
                    "team": team,
                    "gp": gp,
                    "provider_points": _f(s.get("appliedTotal")),
                    "stats": stats,
                }
            )
    return rows


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --- DB writes ----------------------------------------------------------------------------------
_STAT_UPDATE = ("sleeper_id", "position", "team", "gp", "provider_points", "stats")
_ADP_UPDATE = ("position", *_ADP_FIELDS)


def write_stat_lines(session: Session, rows: Iterable[dict[str, Any]], *, batch: int = 2000) -> int:
    rows = list(rows)
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        stmt = insert(StatLine.__table__).values(chunk)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_stat_line_identity",
            set_={c: getattr(stmt.excluded, c) for c in _STAT_UPDATE},
        )
        session.execute(stmt)
    return len(rows)


def write_adp(session: Session, rows: Iterable[dict[str, Any]], *, batch: int = 2000) -> int:
    rows = list(rows)
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        stmt = insert(Adp.__table__).values(chunk)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_adp_identity",
            set_={c: getattr(stmt.excluded, c) for c in _ADP_UPDATE},
        )
        session.execute(stmt)
    return len(rows)


def load_stat_snapshot(
    session: Session, snapshot: Snapshot, payload: bytes, resolver: SleeperIdResolver | None = None
) -> tuple[int, int]:
    """Dispatch one snapshot to the right transform and write. Returns (stat rows, adp rows)."""
    if snapshot.source == "sleeper" and snapshot.kind in _SLEEPER_KIND_CATEGORY:
        stat_rows, adp_rows = sleeper_stat_rows(payload, snapshot)
        return write_stat_lines(session, stat_rows), write_adp(session, adp_rows)
    if snapshot.source == "espn" and snapshot.kind == "kona":
        resolver = resolver or SleeperIdResolver.from_session(session)
        rows = espn_stat_rows(payload, snapshot, resolver)
        n = write_stat_lines(session, rows)
        if resolver.unresolved:
            log.warning(
                "espn snapshot %s: %d espn ids unresolved to sleeper_id (rows kept, NULL id)",
                snapshot.id,
                len(resolver.unresolved),
            )
        return n, 0
    raise ValueError(
        f"snapshot {snapshot.id} ({snapshot.source}/{snapshot.kind}) is not a stat feed"
    )


def loaded_snapshot_ids(session: Session) -> set[int]:
    return set(session.scalars(select(StatLine.snapshot_id).distinct()))


STAT_KINDS = ("projections_season", "projections_week", "stats_season", "stats_week", "kona")
