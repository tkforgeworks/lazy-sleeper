"""Snapshot payloads → core.projections / actuals / adp / snap_counts / expected_points.

Pure transforms (`sleeper_stat_rows`, `espn_stat_rows`) are separated from the DB writes so they
can be unit-tested on fixtures without a database.

Rows with no stat-level content (`stats == {}` — unprojected players, did-not-play weeks) are
dropped: they are ~65% of raw provider rows and carry nothing the scoring engine can use. Sleeper
ADP for such players is still captured in core.adp.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from lazy_sleeper.db.models import (
    Actual,
    Adp,
    Crosswalk,
    ExpectedPoints,
    Player,
    Projection,
    SnapCount,
    Snapshot,
)
from lazy_sleeper.ingest.espn_stats import (
    POSITIONS,
    SOURCE_ACTUAL,
    SOURCE_PROJ,
    SPLIT_SEASON,
    TEAMS,
    decode_stats,
)
from lazy_sleeper.ingest.nflverse_loaders import (
    expected_points_rows,
    nflverse_actual_rows,
    snap_count_rows,
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
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})


def normalize_name(name: str | None) -> str:
    """Lowercase alphanumerics, suffix dropped: "Ke'Shawn Williams Jr." → keshawnwilliams."""
    if not name:
        return ""
    parts = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in name.split()]
    parts = [w for w in parts if w and w not in _SUFFIXES]
    return "".join(parts)


@dataclass
class SleeperIdResolver:
    """Foreign ids → sleeper_id.

    Tiers: crosswalk (authoritative) → `core.players` foreign ids → exact normalized
    (name, position, team) match against `core.players`, only when unique. The name tier exists for
    rookies/new signings the crosswalk hasn't caught up with; every use is recorded in
    `resolved_by_name` so it can be audited (`lazy check joins`).
    """

    espn_to_sleeper: dict[str, str] = field(default_factory=dict)
    gsis_to_sleeper: dict[str, str] = field(default_factory=dict)
    pfr_to_sleeper: dict[str, str] = field(default_factory=dict)
    name_to_sleeper: dict[tuple[str, str, str], str] = field(default_factory=dict)
    unresolved: set[str] = field(default_factory=set)
    resolved_by_name: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_session(cls, session: Session) -> SleeperIdResolver:
        espn: dict[str, str] = {}
        gsis: dict[str, str] = {}
        pfr: dict[str, str] = {}
        names: dict[tuple[str, str, str], str | None] = {}
        for e, g, sid, name, pos, team in session.execute(
            select(
                Player.espn_id,
                Player.gsis_id,
                Player.sleeper_id,
                Player.full_name,
                Player.position,
                Player.team,
            )
        ):
            if e:
                espn[str(e)] = sid
            if g:
                gsis[str(g)] = sid
            key = (normalize_name(name), pos or "", team or "")
            if key[0] and pos and team:
                names[key] = None if key in names else sid  # None marks a collision
        for e, g, pf, sid in session.execute(
            select(Crosswalk.espn_id, Crosswalk.gsis_id, Crosswalk.pfr_id, Crosswalk.sleeper_id)
        ):  # crosswalk wins
            if e:
                espn[str(e)] = sid
            if g:
                gsis[str(g)] = sid
            if pf:
                pfr[str(pf)] = sid
        return cls(espn, gsis, pfr, {k: v for k, v in names.items() if v})

    def resolve(
        self,
        espn_id: str,
        position: str | None,
        pro_team_id: int | None,
        full_name: str | None = None,
    ) -> str | None:
        if position == "DEF":
            return TEAMS.get(pro_team_id or -1)
        sid = self.espn_to_sleeper.get(espn_id)
        if sid is None and full_name and position:
            team = TEAMS.get(pro_team_id or -1)
            sid = self.name_to_sleeper.get((normalize_name(full_name), position, team or ""))
            if sid is not None:
                self.resolved_by_name[espn_id] = sid
        if sid is None:
            self.unresolved.add(espn_id)
        return sid


# --- pure transforms ----------------------------------------------------------------------------
def sleeper_stat_rows(
    payload: bytes, snapshot: Snapshot
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Sleeper projections/stats payload → (stat rows, adp rows).

    Stat rows carry a transient "category" (proj|actual) used only to route them to
    core.projections or core.actuals; it is popped before insert.
    """
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
        if not core_stats:
            continue  # no stat-level content (unprojected player) — ADP above is all it carries
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
    return stat_rows, adp_rows


def espn_stat_rows(
    payload: bytes, snapshot: Snapshot, resolver: SleeperIdResolver
) -> list[dict[str, Any]]:
    """ESPN kona payload → stat rows (season + weekly, proj + actual, all seasons present).

    Each row carries a transient "category" (proj|actual) for routing; popped before insert.
    """
    data = parse_json(payload)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int, int | None]] = set()
    for entry in data.get("players", []):
        p = entry.get("player") or {}
        espn_id = str(p.get("id"))
        position = POSITIONS.get(p.get("defaultPositionId"))
        pro_team_id = p.get("proTeamId")
        team = TEAMS.get(pro_team_id) if pro_team_id else None
        sleeper_id = resolver.resolve(espn_id, position, pro_team_id, p.get("fullName"))
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
            if not stats:
                continue  # unprojected / did-not-play: nothing to score
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
_STAT_UPDATE = ("snapshot_id", "sleeper_id", "position", "team", "gp", "provider_points", "stats")
_ADP_UPDATE = ("position", *_ADP_FIELDS)
_SNAP_UPDATE = (
    "snapshot_id", "sleeper_id", "player", "position", "team", "opponent",
    "offense_snaps", "offense_pct", "defense_snaps", "defense_pct", "st_snaps", "st_pct",
)  # fmt: skip
_EP_UPDATE = (
    "snapshot_id", "sleeper_id", "full_name", "position", "team",
    "total_fantasy_points", "total_fantasy_points_exp", "ep",
)  # fmt: skip


# --- pre-game freeze (LS-53) -------------------------------------------------------------------
# NFL week 1 starts the Thursday after Labor Day (first Monday of September). We freeze a week's
# projections at 00:00 UTC that Thursday (Wed evening ET) — comfortably before kickoff — because
# ESPN's kona feed mutates past weeks after games are played, and the weekly benchmark depends on
# the stored value being the *pre-game* one. Season totals (week NULL) freeze at week-1 kickoff.


def week_kickoff(season: int, week: int) -> datetime:
    """00:00 UTC on the Thursday of NFL week ``week`` (week 1 = Thursday after Labor Day)."""
    sept1 = date(season, 9, 1)
    labor_day = sept1 + timedelta(days=(7 - sept1.weekday()) % 7)  # first Monday
    thursday = labor_day + timedelta(days=3)
    d = thursday + timedelta(weeks=week - 1)
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def frozen(season: int, week: int | None, at: datetime | None) -> bool:
    """Is the (season, week) scope frozen for a payload pulled at ``at``? The *pull* time is what
    matters — a pre-kickoff snapshot loaded later still carries pre-game values."""
    if at is None:
        return False
    return at >= week_kickoff(season, week if week is not None else 1)


def partition_frozen(
    rows: list[dict[str, Any]], pulled_at: datetime | None, *, thaw: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(live, frozen) — frozen rows may still *insert* a missing player but never overwrite.
    ``thaw`` bypasses the freeze (archival rebuilds: load snapshots in pulled_at order)."""
    if thaw or pulled_at is None:
        return rows, []
    live: list[dict[str, Any]] = []
    ice: list[dict[str, Any]] = []
    for r in rows:
        (ice if frozen(r["season"], r.get("week"), pulled_at) else live).append(r)
    return live, ice


def _split(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    proj: list[dict[str, Any]] = []
    actual: list[dict[str, Any]] = []
    for r in rows:
        r = dict(r)
        cat = r.pop("category")
        (proj if cat == "proj" else actual).append(r)
    return proj, actual


def _upsert(
    session: Session, table, rows: list[dict[str, Any]], constraint: str, cols, batch: int
) -> None:  # noqa: ANN001
    """Upsert on ``constraint``; ``cols=None`` → insert-if-missing only (frozen scopes)."""
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        stmt = insert(table).values(chunk)
        if cols is None:
            stmt = stmt.on_conflict_do_nothing(constraint=constraint)
        else:
            stmt = stmt.on_conflict_do_update(
                constraint=constraint, set_={c: getattr(stmt.excluded, c) for c in cols}
            )
        session.execute(stmt)


def write_stat_rows(
    session: Session,
    rows: Iterable[dict[str, Any]],
    *,
    pulled_at: datetime | None = None,
    thaw: bool = False,
    batch: int = 2000,
) -> tuple[int, int]:
    """Route rows to core.projections (latest wins per player until the scope's kickoff — LS-53)
    / core.actuals (latest wins). Frozen projection scopes insert missing players only."""
    proj, actual = _split(rows)
    live, ice = partition_frozen(proj, pulled_at, thaw=thaw)
    _upsert(session, Projection.__table__, live, "uq_projection_identity", _STAT_UPDATE, batch)
    _upsert(session, Projection.__table__, ice, "uq_projection_identity", None, batch)
    if ice:
        log.info(
            "projections: %d rows in frozen (post-kickoff) scopes — insert-if-missing only",
            len(ice),
        )
    _upsert(session, Actual.__table__, actual, "uq_actual_identity", _STAT_UPDATE, batch)
    return len(proj), len(actual)


def write_adp(session: Session, rows: Iterable[dict[str, Any]], *, batch: int = 2000) -> int:
    rows = list(rows)
    _upsert(session, Adp.__table__, rows, "uq_adp_identity", _ADP_UPDATE, batch)
    return len(rows)


@dataclass
class LoadResult:
    projections: int = 0
    actuals: int = 0
    adp: int = 0
    snap_counts: int = 0
    expected_points: int = 0


def load_stat_snapshot(
    session: Session,
    snapshot: Snapshot,
    payload: bytes,
    resolver: SleeperIdResolver | None = None,
    *,
    thaw: bool = False,
) -> LoadResult:
    """Dispatch one snapshot to the right transform and write, then stamp ``loaded_at``.
    ``thaw`` bypasses the projection freeze — archival rebuilds only, loading snapshots in
    pulled_at order."""
    result = _load_stat_snapshot(session, snapshot, payload, resolver, thaw=thaw)
    snapshot.loaded_at = datetime.now(UTC)
    return result


def _load_stat_snapshot(
    session: Session,
    snapshot: Snapshot,
    payload: bytes,
    resolver: SleeperIdResolver | None,
    *,
    thaw: bool,
) -> LoadResult:
    if snapshot.source == "sleeper" and snapshot.kind in _SLEEPER_KIND_CATEGORY:
        stat_rows, adp_rows = sleeper_stat_rows(payload, snapshot)
        p, a = write_stat_rows(session, stat_rows, pulled_at=snapshot.pulled_at, thaw=thaw)
        return LoadResult(p, a, write_adp(session, adp_rows))
    resolver = resolver or SleeperIdResolver.from_session(session)
    if snapshot.source == "espn" and snapshot.kind == "kona":
        p, a = write_stat_rows(
            session, espn_stat_rows(payload, snapshot, resolver),
            pulled_at=snapshot.pulled_at, thaw=thaw,
        )  # fmt: skip
        _warn_unresolved("espn", snapshot, resolver)
        return LoadResult(p, a, 0)
    if snapshot.source == "nflverse" and snapshot.kind == "stats_player_week":
        rows = nflverse_actual_rows(
            payload, snapshot, resolver.gsis_to_sleeper, resolver.unresolved
        )
        _, a = write_stat_rows(session, rows)
        _warn_unresolved("nflverse", snapshot, resolver)
        return LoadResult(0, a, 0)
    if snapshot.source == "nflverse" and snapshot.kind == "snap_counts":
        rows = snap_count_rows(payload, snapshot, resolver.pfr_to_sleeper)
        _upsert(session, SnapCount.__table__, rows, "uq_snap_count_identity", _SNAP_UPDATE, 2000)
        return LoadResult(snap_counts=len(rows))
    if snapshot.source == "nflverse" and snapshot.kind == "ff_opportunity":
        rows = expected_points_rows(payload, snapshot, resolver.gsis_to_sleeper)
        _upsert(
            session, ExpectedPoints.__table__, rows, "uq_expected_points_identity", _EP_UPDATE, 2000
        )
        return LoadResult(expected_points=len(rows))
    raise ValueError(f"snapshot {snapshot.id} ({snapshot.source}/{snapshot.kind}): not a stat feed")


def _warn_unresolved(source: str, snapshot: Snapshot, resolver: SleeperIdResolver) -> None:
    if resolver.unresolved:
        log.warning(
            "%s snapshot %s: %d ids unresolved to sleeper_id so far (rows kept, NULL id)",
            source,
            snapshot.id,
            len(resolver.unresolved),
        )


def duplicate_scope_ids(snaps: Sequence[Any], loaded_ids: set[int]) -> set[int]:
    """LS-52 defense in depth for pre-existing duplicates in the archive: among ``snaps``
    (Snapshot rows), ids whose (source, kind, season, week, sha256) matches an already-loaded
    snapshot — or an earlier snapshot in this same batch — and can be skipped by ``load stats``."""
    seen: set[tuple[str, str, int | None, int | None, str]] = {
        (s.source, s.kind, s.season, s.week, s.sha256) for s in snaps if s.id in loaded_ids
    }
    dupes: set[int] = set()
    for s in sorted(snaps, key=lambda s: (s.pulled_at, s.id)):
        if s.id in loaded_ids:
            continue
        scope = (s.source, s.kind, s.season, s.week, s.sha256)
        if scope in seen:
            dupes.add(s.id)
        else:
            seen.add(scope)
    return dupes


def loaded_snapshot_ids(session: Session) -> set[int]:
    """Snapshots `load_stat_snapshot` has already processed (``raw.snapshots.loaded_at``).

    Row references in core.* are not the test: latest-wins projections and the freeze leave a
    snapshot that changed nothing with no row pointing at it, and it would be re-loaded forever."""
    return set(session.scalars(select(Snapshot.id).where(Snapshot.loaded_at.is_not(None))))


STAT_KINDS = (
    "projections_season",
    "projections_week",
    "stats_season",
    "stats_week",
    "kona",
    "stats_player_week",
    "snap_counts",
    "ff_opportunity",
)
