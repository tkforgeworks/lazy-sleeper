"""`lazy` command-line entrypoint (installed via pyproject [project.scripts]).

lazy pull daily                 # players + 2026 season projections + ESPN 2026 + crosswalk
lazy pull projections 2025 --week 3
lazy pull espn 2026
lazy pull league                # league, users, rosters, draft, draft picks
lazy pull picks                 # just draft picks (poll target for draft night)
lazy pull nflverse 2025         # weekly stats + snap counts
lazy backfill data_pulls/ff-projections-2026-08-16 --pulled-at 2026-08-16
lazy load players               # latest valid players snapshot → core.players
lazy load crosswalk
lazy load stats                 # valid, not-yet-loaded proj/actual snapshots → core.stat_lines/adp
lazy load stats --source sleeper --season 2026   # only matching snapshots
lazy db upgrade                 # alembic upgrade head
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import typer

from lazy_sleeper.config import Settings, get_settings
from lazy_sleeper.db.session import make_engine, make_session_factory, session_scope
from lazy_sleeper.ingest.espn import EspnClient
from lazy_sleeper.ingest.http import HttpClient
from lazy_sleeper.ingest.loaders import load_crosswalk, load_players
from lazy_sleeper.ingest.nflverse import NflverseClient
from lazy_sleeper.ingest.pipeline import Puller
from lazy_sleeper.ingest.sleeper import SleeperClient
from lazy_sleeper.ingest.snapshots import (
    SnapshotKey,
    SnapshotRepository,
    SnapshotStore,
    SupabaseStorage,
)
from lazy_sleeper.ingest.stat_loaders import (
    STAT_KINDS,
    SleeperIdResolver,
    load_stat_snapshot,
    loaded_snapshot_ids,
)

app = typer.Typer(no_args_is_help=True, add_completion=False)
pull_app = typer.Typer(no_args_is_help=True)
load_app = typer.Typer(no_args_is_help=True)
db_app = typer.Typer(no_args_is_help=True)
app.add_typer(pull_app, name="pull", help="Fetch external data into dated snapshots")
app.add_typer(load_app, name="load", help="Load latest snapshots into core tables")
app.add_typer(db_app, name="db", help="Database migrations")


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _store(settings: Settings) -> SnapshotStore:
    remote = None
    if settings.supabase_enabled:
        remote = SupabaseStorage(
            settings.supabase_url or "",
            settings.supabase_secret_key or "",
            settings.supabase_bucket,
        )
    return SnapshotStore(settings.snapshot_dir, remote)


class _Ctx:
    """Wires collaborators for one CLI invocation."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.http = HttpClient(
            timeout_s=self.settings.http_timeout_s,
            retries=self.settings.http_retries,
            delay_ms=self.settings.http_delay_ms,
        )
        self.engine = make_engine(self.settings)
        self.sessions = make_session_factory(self.engine)
        self.store = _store(self.settings)

    def puller(self, session) -> Puller:  # noqa: ANN001
        return Puller(
            session=session,
            store=self.store,
            sleeper=SleeperClient(self.http),
            espn=EspnClient(self.http),
            nflverse=NflverseClient(self.http),
        )


# --- pull ------------------------------------------------------------------
@pull_app.command("projections")
def pull_projections(season: int, week: int | None = typer.Option(None)) -> None:
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        ctx.puller(s).pull_sleeper_projections(season, week)


@pull_app.command("stats")
def pull_stats(season: int, week: int | None = typer.Option(None)) -> None:
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        ctx.puller(s).pull_sleeper_stats(season, week)


@pull_app.command("players")
def pull_players() -> None:
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        ctx.puller(s).pull_sleeper_players()


@pull_app.command("espn")
def pull_espn(season: int) -> None:
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        ctx.puller(s).pull_espn_kona(season)


@pull_app.command("league")
def pull_league() -> None:
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        ctx.puller(s).pull_league_state(
            ctx.settings.sleeper_league_id, ctx.settings.sleeper_draft_id
        )


@pull_app.command("picks")
def pull_picks() -> None:
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        ctx.puller(s).pull_draft_picks(ctx.settings.sleeper_draft_id)


@pull_app.command("nflverse")
def pull_nflverse(
    season: int, crosswalk: bool = typer.Option(False, help="Also refresh the crosswalk")
) -> None:
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        p = ctx.puller(s)
        p.pull_nflverse_stats(season)
        p.pull_nflverse_snaps(season)
        if crosswalk:
            p.pull_crosswalk()


@pull_app.command("crosswalk")
def pull_crosswalk() -> None:
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        ctx.puller(s).pull_crosswalk()


@pull_app.command("daily")
def pull_daily(season: int = 2026) -> None:
    """The daily pre-draft job: players, season projections/ADP, ESPN, crosswalk."""
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        p = ctx.puller(s)
        p.pull_sleeper_players()
        p.pull_sleeper_projections(season)
        p.pull_espn_kona(season)
        p.pull_crosswalk()


# --- backfill --------------------------------------------------------------
@app.command("backfill")
def backfill(
    directory: Path,
    pulled_at: str = typer.Option(
        ..., help="ISO date/datetime the files were originally fetched, e.g. 2026-08-16"
    ),
) -> None:
    """Import a data_pull_script.ps1 output directory as snapshots."""
    ts = datetime.fromisoformat(pulled_at)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        rows = ctx.puller(s).backfill_dir(directory, ts)
    typer.echo(f"backfilled {len(rows)} snapshots from {directory}")


# --- load ------------------------------------------------------------------
@load_app.command("players")
def load_players_cmd() -> None:
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        snap = SnapshotRepository(s).latest(SnapshotKey("sleeper", "players"))
        if snap is None:
            raise typer.BadParameter("no valid players snapshot; run `lazy pull players` first")
        n = load_players(s, ctx.store.read(snap.storage_path), snap.id)
    typer.echo(f"loaded {n} players from snapshot {snap.id}")


@load_app.command("crosswalk")
def load_crosswalk_cmd() -> None:
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        snap = SnapshotRepository(s).latest(SnapshotKey("nflverse", "crosswalk"))
        if snap is None:
            raise typer.BadParameter("no valid crosswalk snapshot; run `lazy pull crosswalk` first")
        n = load_crosswalk(s, ctx.store.read(snap.storage_path), snap.id)
    typer.echo(f"loaded {n} crosswalk rows from snapshot {snap.id}")


@load_app.command("stats")
def load_stats_cmd(
    source: str | None = typer.Option(None, help="sleeper | espn"),
    season: int | None = typer.Option(None),
    week: int | None = typer.Option(None),
    latest_only: bool = typer.Option(
        False, help="Only the latest snapshot per (source, kind, season, week)"
    ),
    reload: bool = typer.Option(False, help="Re-load snapshots already present in core.stat_lines"),
) -> None:
    """Load Sleeper projections/stats and ESPN kona snapshots into core.stat_lines + core.adp."""
    from sqlalchemy import select

    from lazy_sleeper.db.models import Snapshot

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        stmt = select(Snapshot).where(Snapshot.valid.is_(True), Snapshot.kind.in_(STAT_KINDS))
        if source:
            stmt = stmt.where(Snapshot.source == source)
        if season is not None:
            stmt = stmt.where(Snapshot.season == season)
        if week is not None:
            stmt = stmt.where(Snapshot.week == week)
        snaps = list(s.scalars(stmt.order_by(Snapshot.pulled_at)))
        if latest_only:
            latest: dict[tuple, Snapshot] = {}
            for snap in snaps:
                latest[(snap.source, snap.kind, snap.season, snap.week)] = snap
            snaps = list(latest.values())
        already = set() if reload else loaded_snapshot_ids(s)
        resolver = SleeperIdResolver.from_session(s)
        total_stats = total_adp = done = 0
        for snap in snaps:
            if snap.id in already:
                continue
            n_stats, n_adp = load_stat_snapshot(
                s, snap, ctx.store.read(snap.storage_path), resolver
            )
            done += 1
            total_stats += n_stats
            total_adp += n_adp
            typer.echo(
                f"  {snap.source}/{snap.kind} s={snap.season} w={snap.week} -> "
                f"{n_stats} stat lines, {n_adp} adp"
            )
    typer.echo(
        f"loaded {done} snapshots: {total_stats} stat lines, {total_adp} adp rows"
        + (f"; {len(resolver.unresolved)} espn ids unresolved" if resolver.unresolved else "")
    )


# --- db --------------------------------------------------------------------
@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), revision)


@db_app.command("downgrade")
def db_downgrade(revision: str = "-1") -> None:
    from alembic import command
    from alembic.config import Config

    command.downgrade(Config("alembic.ini"), revision)


if __name__ == "__main__":
    app()
