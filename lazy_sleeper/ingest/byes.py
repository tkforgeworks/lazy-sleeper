"""Bye weeks (LS-57): ESPN's pro-team doc → ``core.team_byes`` → a ``bye`` on every board row.

Source: ``GET /apis/v3/games/ffl/seasons/{season}?view=proTeamSchedules_wl`` — one small
document whose ``settings.proTeams[]`` carries ``byeWeek`` per team (verified 2026-08-28 for
2026: 32 teams + the free-agent pseudo-team ``id 0`` with ``byeWeek 0``). ESPN's ``proTeamId``
→ Sleeper team abbreviation is the same map the DEF loader uses (``espn_stats.TEAMS``, 32/32
verified in LS-22), so no new crosswalk. Players resolve to a bye through ``core.players.team``;
free agents / unknown teams get ``None``, never an error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from lazy_sleeper.db.models import TeamBye
from lazy_sleeper.ingest.espn_stats import TEAMS
from lazy_sleeper.ingest.validate import parse_json


def parse_pro_teams(payload: bytes) -> list[dict[str, Any]]:
    """``settings.proTeams[]`` → one row per real team: ``team`` (Sleeper abbreviation),
    ``espn_id``, ``espn_abbrev``, ``bye_week``. Teams without a bye (the FA pseudo-team) and
    ids outside the 32-team map are dropped."""
    d = parse_json(payload)
    teams = d.get("settings", {}).get("proTeams") if isinstance(d, dict) else None
    if not isinstance(teams, list):
        raise ValueError("pro-team payload has no settings.proTeams list")
    out: list[dict[str, Any]] = []
    for t in teams:
        if not isinstance(t, dict) or t.get("id") is None:
            continue
        team = TEAMS.get(int(t["id"]))
        bye = t.get("byeWeek")
        if team is None or not bye:
            continue
        out.append(
            {
                "team": team,
                "espn_id": int(t["id"]),
                "espn_abbrev": t.get("abbrev"),
                "bye_week": int(bye),
            }
        )
    return out


def load_byes(session: Session, payload: bytes, season: int, snapshot_id: int | None) -> int:
    """Upsert the season's byes by (season, team). Returns the row count."""
    rows = parse_pro_teams(payload)
    if not rows:
        return 0
    now = datetime.now(UTC)
    for r in rows:
        r.update(season=season, snapshot_id=snapshot_id, updated_at=now)
    stmt = insert(TeamBye).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["season", "team"],
        set_={c: getattr(stmt.excluded, c) for c in rows[0] if c not in ("season", "team")},
    )
    session.execute(stmt)
    return len(rows)


def byes_for(session: Session, season: int) -> dict[str, int]:
    """team abbreviation → bye week for ``season`` (empty until ``lazy pull byes --load``)."""
    return {
        team: int(week)
        for team, week in session.execute(
            select(TeamBye.team, TeamBye.bye_week).where(TeamBye.season == season)
        )
    }


def bye_of(byes: dict[str, int] | None, team: str | None) -> int | None:
    """The row-level lookup: None for free agents / unknown teams / no byes loaded."""
    if not byes or not team:
        return None
    return byes.get(team)


__all__ = ["bye_of", "byes_for", "load_byes", "parse_pro_teams"]
