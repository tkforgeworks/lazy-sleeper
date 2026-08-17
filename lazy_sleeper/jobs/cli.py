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
lazy load stats                 # not-yet-loaded snapshots → projections/actuals/adp/snaps/xfp
lazy load stats --source sleeper --season 2026   # only matching snapshots
lazy snapshots reindex          # re-register local archive files after a DB reset
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
        p.pull_ff_opportunity(season)
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


# --- snapshots -------------------------------------------------------------
snap_app = typer.Typer(no_args_is_help=True)
app.add_typer(snap_app, name="snapshots", help="Archive maintenance")


@snap_app.command("reindex")
def snapshots_reindex() -> None:
    """Register archive files missing from raw.snapshots (rebuild after a DB reset)."""
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        registered, skipped = ctx.puller(s).reindex()
    typer.echo(f"reindexed {registered} snapshots ({skipped} already registered)")


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
    reload: bool = typer.Option(False, help="Re-load snapshots already loaded"),
) -> None:
    """Load Sleeper projections/stats + ESPN kona snapshots into core.projections/actuals/adp."""
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
        tp = ta = tadp = tsn = tep = done = 0
        for snap in snaps:
            if snap.id in already:
                continue
            r = load_stat_snapshot(s, snap, ctx.store.read(snap.storage_path), resolver)
            done += 1
            tp, ta, tadp = tp + r.projections, ta + r.actuals, tadp + r.adp
            tsn, tep = tsn + r.snap_counts, tep + r.expected_points
            typer.echo(
                f"  {snap.source}/{snap.kind} s={snap.season} w={snap.week} -> "
                f"{r.projections} proj, {r.actuals} actual, {r.adp} adp, "
                f"{r.snap_counts} snaps, {r.expected_points} xfp"
            )
    typer.echo(
        f"loaded {done} snapshots: {tp} projections, {ta} actuals, {tadp} adp, "
        f"{tsn} snap counts, {tep} expected points"
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


# --- score -----------------------------------------------------------------
score_app = typer.Typer(no_args_is_help=True)
app.add_typer(score_app, name="score", help="Apply league scoring to stat lines")


@score_app.command("rules")
def score_rules() -> None:
    """Print the league scoring map from the latest league snapshot."""
    from lazy_sleeper.scoring import load_league_rules

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        rules = load_league_rules(s, ctx.store)
    typer.echo(f"{rules.league_name} ({rules.league_id}) roster={' '.join(rules.roster_positions)}")
    for k, v in sorted(rules.weights.items()):
        typer.echo(f"  {k:<18}{v:>7g}")


@score_app.command("kmix")
def score_kmix(
    seasons: str = typer.Option(
        "2023,2024,2025", help="Comma-separated seasons of nflverse actuals"
    ),
) -> None:
    """FG distance mix derived from core.actuals vs the frozen DEFAULT_MIX used for projections."""
    from lazy_sleeper.scoring import DEFAULT_MIX, distance_mix_from_actuals
    from lazy_sleeper.scoring.kicking import BUCKET_NAMES

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        live = distance_mix_from_actuals(s, tuple(int(x) for x in seasons.split(",")))
    typer.echo(f"{'bucket':<8}{'actuals':>9}{'default':>9}")
    for b in BUCKET_NAMES:
        typer.echo(f"{b:<8}{live.shares[b]:>9.4f}{DEFAULT_MIX.shares[b]:>9.4f}")


@score_app.command("def-rank")
def score_def_rank(
    seasons: str = typer.Option("2024,2025", help="Comma-separated seasons of weekly DEF actuals"),
    source: str = typer.Option("espn"),
) -> None:
    """Season-average DEF streaming rank (league points per game over the given seasons)."""
    from lazy_sleeper.scoring import default_scorer, load_league_rules, streaming_ranks

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        scorer = default_scorer(load_league_rules(s, ctx.store))
        ranks = streaming_ranks(s, scorer, tuple(int(x) for x in seasons.split(",")), source)
    typer.echo(f"{'rank':<5}{'team':<5}{'games':>6}{'ppg':>7}")
    for r in ranks:
        typer.echo(f"{r.rank:<5}{r.team:<5}{r.games:>6}{r.ppg:>7.2f}")


@score_app.command("parity")
def score_parity(
    season: int = typer.Option(2025),
    write_fixture: str | None = typer.Option(None, help="Also write rows as a gzip JSON fixture"),
) -> None:
    """Engine vs nflverse fantasy_points_ppr on weekly offense actuals (known diffs adjusted)."""
    import gzip
    import json

    from lazy_sleeper.scoring import default_scorer, load_league_rules
    from lazy_sleeper.scoring.league import parity_rows
    from lazy_sleeper.scoring.parity import parity

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        rules = load_league_rules(s, ctx.store)
        rows = parity_rows(s, rules, season)
    rep = parity(rows, default_scorer(rules))
    typer.echo(f"{len(rows)} rows, mean |Δ| = {rep.mean_abs_delta:.4f}")
    for pos, m in rep.mean_by_position().items():
        typer.echo(f"  {pos:<3} n={len(rep.by_position[pos]):<5} mean |Δ| = {m:.4f}")
    typer.echo(f"  outliers (>0.05): {len(rep.outliers)}")
    for o in rep.outliers[:10]:
        typer.echo(
            f"    wk{o['week']:<3}{o['position']:<3}{o['source_player_id']:<12}"
            f"league={o['league_points']:.2f} nflverse={o['provider_points']:.2f} "
            f"Δ={o['delta']:+.2f}"
        )
    if write_fixture:
        Path(write_fixture).write_bytes(
            gzip.compress(json.dumps(rows, separators=(",", ":")).encode(), 9)
        )
        typer.echo(f"wrote {len(rows)} rows → {write_fixture}")


@score_app.command("preview")
def score_preview(
    season: int = typer.Option(2026),
    week: int | None = typer.Option(None, help="Omit for season totals"),
    source: str = typer.Option("sleeper", help="sleeper | espn"),
    position: str | None = typer.Option(None, help="QB | RB | WR | TE | K | DEF"),
    top: int = typer.Option(25),
    actuals: bool = typer.Option(False, help="Score core.actuals instead of projections"),
) -> None:
    """Score the latest projection vintage (or actuals) and list the top players."""
    from sqlalchemy import select

    from lazy_sleeper.db.models import Actual, Player, Projection
    from lazy_sleeper.scoring import default_scorer, load_league_rules

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        scorer = default_scorer(load_league_rules(s, ctx.store))
        model = Actual if actuals else Projection
        stmt = select(model).where(
            model.source == source,
            model.season == season,
            model.week.is_(None) if week is None else model.week == week,
        )
        if position:
            stmt = stmt.where(model.position == position)
        rows = list(s.scalars(stmt))
        if not actuals and rows:
            latest = max(r.snapshot_id for r in rows)
            rows = [r for r in rows if r.snapshot_id == latest]
        names = dict(
            s.execute(
                select(Player.sleeper_id, Player.full_name).where(
                    Player.sleeper_id.in_({r.sleeper_id for r in rows if r.sleeper_id})
                )
            ).all()
        )
        scored = sorted(((scorer.score(r.stats, r.position), r) for r in rows), key=lambda t: -t[0])
        typer.echo(f"{'pos':<4}{'team':<5}{'player':<26}{'pts':>8}{'provider':>10}")
        for pts, r in scored[:top]:
            name = names.get(r.sleeper_id or "", r.source_player_id)
            prov = f"{r.provider_points:.1f}" if r.provider_points is not None else "-"
            typer.echo(
                f"{r.position or '':<4}{r.team or '':<5}{name[:25]:<26}{pts:>8.1f}{prov:>10}"
            )
