"""Data-quality audit over `raw` / `core`: join coverage, freshness, duplicates, DEF path (LS-22).

Pure query functions returning plain data; `lazy check` prints them. Run after every load and before
the board is trusted. Every "miss" is reported by id/position/team/points — nothing is dropped
silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from lazy_sleeper.db.models import Actual, Adp, Crosswalk, Player, Projection, Snapshot

OFFENSE_K = ("QB", "RB", "WR", "TE", "K")
NFL_TEAMS = frozenset(
    [
        "ARI",
        "ATL",
        "BAL",
        "BUF",
        "CAR",
        "CHI",
        "CIN",
        "CLE",
        "DAL",
        "DEN",
        "DET",
        "GB",
        "HOU",
        "IND",
        "JAX",
        "KC",
        "LAC",
        "LAR",
        "LV",
        "MIA",
        "MIN",
        "NE",
        "NO",
        "NYG",
        "NYJ",
        "PHI",
        "PIT",
        "SEA",
        "SF",
        "TB",
        "TEN",
        "WAS",
    ]
)


@dataclass
class Freshness:
    source: str
    kind: str
    season: int | None
    latest: datetime
    age_hours: float
    valid: bool
    record_count: int | None
    weeks: int  # distinct weeks present for this (source, kind, season); 0 for season-level kinds


def freshness(session: Session, now: datetime | None = None) -> list[Freshness]:
    """Newest snapshot per (source, kind, season) with its age — stale feeds jump out."""
    now = now or datetime.now(UTC)
    latest: dict[tuple[str, str, int | None], Snapshot] = {}
    weeks: dict[tuple[str, str, int | None], set[int]] = {}
    for snap in session.scalars(select(Snapshot).order_by(Snapshot.pulled_at.desc())):
        key = (snap.source, snap.kind, snap.season)
        latest.setdefault(key, snap)
        if snap.week is not None:
            weeks.setdefault(key, set()).add(snap.week)
    return [
        Freshness(
            s.source,
            s.kind,
            s.season,
            s.pulled_at,
            (now - s.pulled_at).total_seconds() / 3600,
            s.valid,
            s.record_count,
            len(weeks.get(key, ())),
        )
        for key, s in sorted(latest.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or 0))
    ]


@dataclass
class CrosswalkReport:
    rows: int
    with_sportradar: int
    with_gsis: int
    with_espn: int
    players_joined: int
    sportradar_agree: int
    sportradar_conflicts: list[tuple[str, str | None, str | None]]  # sleeper_id, player, xwalk name
    top_n: int
    top_n_joined: int
    top_n_misses: list[dict[str, Any]]


def crosswalk_report(session: Session, top_n: int = 300) -> CrosswalkReport:
    """Crosswalk completeness, sportradar agreement with core.players, top-N coverage."""
    rows, sr, gs, es = session.execute(
        select(
            func.count(),
            func.count(Crosswalk.sportradar_id),
            func.count(Crosswalk.gsis_id),
            func.count(Crosswalk.espn_id),
        )
    ).one()
    j = select(Player, Crosswalk).join(Crosswalk, Crosswalk.sleeper_id == Player.sleeper_id)
    joined = agree = 0
    conflicts: list[tuple[str, str | None, str | None]] = []
    for p, x in session.execute(j):
        joined += 1
        if p.sportradar_id and x.sportradar_id:
            if p.sportradar_id == x.sportradar_id:
                agree += 1
            else:
                conflicts.append((p.sleeper_id, p.full_name, x.name))
    top = list(
        session.scalars(
            select(Player)
            .where(Player.position.in_(OFFENSE_K), Player.search_rank.is_not(None))
            .order_by(Player.search_rank)
            .limit(top_n)
        )
    )
    xw = set(session.scalars(select(Crosswalk.sleeper_id)))
    misses = [
        {
            "sleeper_id": p.sleeper_id,
            "name": p.full_name,
            "position": p.position,
            "team": p.team,
            "search_rank": p.search_rank,
            "years_exp": p.years_exp,
        }
        for p in top
        if p.sleeper_id not in xw
    ]
    return CrosswalkReport(
        rows, sr, gs, es, joined, agree, conflicts, len(top), len(top) - len(misses), misses
    )


@dataclass
class ResolveReport:
    table: str
    source: str
    rows: int
    resolved: int
    unresolved_top: list[dict[str, Any]] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.resolved / self.rows if self.rows else 1.0


def resolve_report(
    session: Session, min_points: float = 20.0, limit: int = 15
) -> list[ResolveReport]:
    """sleeper_id resolution per (table, source) + the unresolved rows that carry real points."""
    out: list[ResolveReport] = []
    for model, name in ((Projection, "projections"), (Actual, "actuals")):
        for src, n, res in session.execute(
            select(model.source, func.count(), func.count(model.sleeper_id)).group_by(model.source)
        ):
            top = session.execute(
                select(
                    model.source_player_id,
                    model.position,
                    model.team,
                    func.max(model.season),
                    func.max(cast(model.provider_points, Float)),
                    func.count(),
                )
                .where(model.source == src, model.sleeper_id.is_(None))
                .group_by(model.source_player_id, model.position, model.team)
                .having(func.max(model.provider_points) >= min_points)
                .order_by(func.max(model.provider_points).desc())
                .limit(limit)
            ).all()
            out.append(
                ResolveReport(
                    name,
                    src,
                    n,
                    res,
                    [
                        {
                            "source_player_id": pid,
                            "position": pos,
                            "team": team,
                            "season": season,
                            "max_provider_points": pts,
                            "rows": cnt,
                        }
                        for pid, pos, team, season, pts, cnt in top
                    ],
                )
            )
    return out


@dataclass
class DefReport:
    players_def_ids: set[str]
    espn_def_ids: set[str]
    espn_def_rows: int
    espn_def_unresolved: int

    @property
    def ok(self) -> bool:
        return (
            self.players_def_ids == NFL_TEAMS
            and self.espn_def_ids == NFL_TEAMS
            and self.espn_def_unresolved == 0
        )


def def_report(session: Session) -> DefReport:
    """ESPN DST → Sleeper DEF path: proTeamId → team abbr == Sleeper DEF player_id, all 32 teams."""
    players = set(session.scalars(select(Player.sleeper_id).where(Player.position == "DEF")))
    rows, unresolved = session.execute(
        select(func.count(), func.count() - func.count(Projection.sleeper_id)).where(
            Projection.source == "espn", Projection.position == "DEF"
        )
    ).one()
    espn_ids = set(
        session.scalars(
            select(Projection.sleeper_id)
            .where(Projection.source == "espn", Projection.position == "DEF")
            .distinct()
        )
    ) - {None}
    return DefReport(players, espn_ids, rows, unresolved)


@dataclass
class DupReport:
    table: str
    duplicate_groups: int
    examples: list[tuple]


def duplicate_report(session: Session) -> list[DupReport]:
    """A resolved sleeper_id must appear once per (source, snapshot/season/week).

    Otherwise two source rows were mapped onto the same player and the board would double count.
    """
    out: list[DupReport] = []
    keys = (
        Projection.source,
        Projection.snapshot_id,
        Projection.season,
        Projection.week,
        Projection.sleeper_id,
    )
    stmt = (
        select(*keys, func.count())
        .where(Projection.sleeper_id.is_not(None))
        .group_by(*keys)
        .having(func.count() > 1)
    )
    dups = session.execute(stmt).all()
    out.append(DupReport("projections", len(dups), [tuple(d) for d in dups[:10]]))
    akeys = (Actual.source, Actual.season, Actual.week, Actual.sleeper_id)
    stmt = (
        select(*akeys, func.count())
        .where(Actual.sleeper_id.is_not(None))
        .group_by(*akeys)
        .having(func.count() > 1)
    )
    dups = session.execute(stmt).all()
    out.append(DupReport("actuals", len(dups), [tuple(d) for d in dups[:10]]))
    return out


@dataclass
class Counts:
    players: int
    players_by_position: dict[str, int]
    crosswalk: int
    projections: int
    actuals: int
    adp: int
    adp_resolved: int


def counts(session: Session) -> Counts:
    by_pos = dict(
        session.execute(
            select(Player.position, func.count())
            .where(Player.position.in_(OFFENSE_K + ("DEF",)))
            .group_by(Player.position)
        ).all()
    )
    return Counts(
        players=session.scalar(select(func.count()).select_from(Player)) or 0,
        players_by_position=by_pos,
        crosswalk=session.scalar(select(func.count()).select_from(Crosswalk)) or 0,
        projections=session.scalar(select(func.count()).select_from(Projection)) or 0,
        actuals=session.scalar(select(func.count()).select_from(Actual)) or 0,
        adp=session.scalar(select(func.count()).select_from(Adp)) or 0,
        adp_resolved=session.scalar(select(func.count(Adp.sleeper_id))) or 0,
    )


# --- single-player dossier ---------------------------------------------------------


@dataclass
class PlayerDossier:
    player: Player
    crosswalk: Crosswalk | None
    projections: list[dict[str, Any]]  # latest vintage per (source, season, week) + our score
    actuals: list[dict[str, Any]]  # per source: seasons, games, provider pts, our pts
    adp: dict[str, Any] | None


def find_players(session: Session, name: str, team: str | None = None) -> list[Player]:
    """Players whose normalized name matches `name` (exact) or contains it; team narrows."""
    from lazy_sleeper.ingest.stat_loaders import normalize_name

    if name.isdigit():
        p = session.get(Player, name)
        return [p] if p else []
    want = normalize_name(name)
    stmt = select(Player).where(Player.full_name.is_not(None))
    if team:
        stmt = stmt.where(Player.team == team.upper())
    hits = [p for p in session.scalars(stmt) if want and want in normalize_name(p.full_name)]
    exact = [p for p in hits if normalize_name(p.full_name) == want]
    hits = exact or hits
    return sorted(hits, key=lambda p: (p.search_rank or 10**9, p.sleeper_id))


def player_dossier(session: Session, player: Player, scorer: Any) -> PlayerDossier:
    xw = session.get(Crosswalk, player.sleeper_id)
    latest: dict[tuple[str, int, int | None], Projection] = {}
    for pr in session.scalars(
        select(Projection)
        .where(Projection.sleeper_id == player.sleeper_id)
        .order_by(Projection.snapshot_id.desc())
    ):
        latest.setdefault((pr.source, pr.season, pr.week), pr)
    projections = [
        {
            "source": pr.source,
            "season": pr.season,
            "week": pr.week,
            "source_player_id": pr.source_player_id,
            "team": pr.team,
            "provider_points": pr.provider_points,
            "our_points": scorer.score(pr.stats, pr.position or player.position),
            "snapshot_id": pr.snapshot_id,
        }
        for (_, _, _), pr in sorted(
            latest.items(), key=lambda kv: (kv[0][0], -kv[0][1], kv[0][2] or 0)
        )
        if pr.week is None or pr.season >= 2026
    ]
    actuals: dict[tuple[str, int], dict[str, Any]] = {}
    for a in session.scalars(
        select(Actual).where(Actual.sleeper_id == player.sleeper_id, Actual.week.is_not(None))
    ):
        d = actuals.setdefault(
            (a.source, a.season),
            {
                "source": a.source,
                "season": a.season,
                "source_player_id": a.source_player_id,
                "games": 0,
                "provider_points": 0.0,
                "our_points": 0.0,
            },
        )
        d["games"] += 1
        d["provider_points"] += a.provider_points or 0.0
        d["our_points"] += scorer.score(a.stats, a.position or player.position)
    adp_row = session.scalars(
        select(Adp).where(Adp.sleeper_id == player.sleeper_id).order_by(Adp.season.desc()).limit(1)
    ).first()
    adp = None
    if adp_row is not None:
        adp = {c.name: getattr(adp_row, c.name) for c in Adp.__table__.columns}
    return PlayerDossier(
        player,
        xw,
        projections,
        [actuals[k] for k in sorted(actuals, key=lambda k: (k[0], -k[1]))],
        adp,
    )
