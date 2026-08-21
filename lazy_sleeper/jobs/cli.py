"""`lazy` command-line entrypoint (installed via pyproject [project.scripts]).

lazy pull daily                 # players + 2026 season projections + ESPN 2026 + crosswalk
lazy pull projections 2025 --week 3
lazy pull espn 2026
lazy pull league --load         # league, users, rosters, draft, draft picks (+ load into core.*)
lazy pull picks --load          # just draft picks, one shot (--draft-id <mock>)
lazy draft poll --advise        # draft-night poller: new picks + my top picks when on the clock
lazy draft advise               # one-shot: who should I take now (survival / run / pick_score)
lazy pull nflverse 2025         # weekly stats + snap counts
lazy backfill data_pulls/ff-projections-2026-08-16 --pulled-at 2026-08-16
lazy load players               # latest valid players snapshot → core.players
lazy load crosswalk
lazy load league                # league-state snapshots → core.drafts/draft_picks/rosters/users
lazy load stats                 # not-yet-loaded snapshots → projections/actuals/adp/snaps/xfp
lazy load stats --source sleeper --season 2026   # only matching snapshots
lazy snapshots reindex          # re-register local archive files after a DB reset
lazy sync push|pull             # mirror local archive ↔ Supabase Storage (fresh machine: pull)
lazy benchmark season|weekly    # Sleeper/ESPN/naive vs 2024–25 actuals → data/benchmarks/*.csv
lazy benchmark fit-weights      # inverse-MAE blend weights → derived.ensemble_weights + JSON
lazy weights show|set|clear|config   # fitted vs manual-override blend weights (the λ switch)
lazy score preview --source ensemble # blended projections with each member's column
lazy board baselines            # replacement-level points per position (historical + live)
lazy board vorp --top 30        # the board: VORP, tiers/cliffs, ADP delta, disagreement flags
lazy board config               # show/update stored tier/cliff/flag thresholds (draft-day dial)
lazy board regen                # persist a dated board (derived.boards) + CSV/HTML in data/boards/
lazy db upgrade                 # alembic upgrade head
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

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
    store_from_settings,
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
draft_app = typer.Typer(no_args_is_help=True)
app.add_typer(draft_app, name="draft", help="Live draft companion (M4)")


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _store(settings: Settings) -> SnapshotStore:
    return store_from_settings(settings)


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

    def scorer(self, session):  # noqa: ANN001, ANN201
        from lazy_sleeper.scoring import default_scorer, load_league_rules

        return default_scorer(load_league_rules(session, self.store))

    def provider(self, session, name: str):  # noqa: ANN001, ANN201
        """`sleeper` | `espn` | `ensemble` — league-scored projections from stored vintages."""
        from lazy_sleeper.providers import make_provider

        try:
            return make_provider(session, self.scorer(session), name)
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e


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
def pull_league(
    draft_id: str | None = typer.Option(None, help="Override the configured draft (e.g. a mock)"),
    load: bool = typer.Option(False, help="Also load the snapshots into core.* (lazy load league)"),
) -> None:
    """Snapshot league, users, rosters, draft and picks."""
    ctx = _Ctx()
    did = draft_id or ctx.settings.sleeper_draft_id
    with session_scope(ctx.sessions) as s:
        ctx.puller(s).pull_league_state(ctx.settings.sleeper_league_id, did)
    if load:
        _load_league(ctx, did)


@pull_app.command("picks")
def pull_picks(
    draft_id: str | None = typer.Option(None, help="Override the configured draft (e.g. a mock)"),
    load: bool = typer.Option(False, help="Also sync core.draft_picks from the snapshot"),
) -> None:
    """Snapshot just the picks — the draft-night poll target. Point --draft-id at a Sleeper mock
    draft you've joined to exercise the loader on real picks before the live draft."""
    from lazy_sleeper.ingest.league_loaders import load_draft_picks

    ctx = _Ctx()
    did = draft_id or ctx.settings.sleeper_draft_id
    with session_scope(ctx.sessions) as s:
        snap = ctx.puller(s).pull_draft_picks(did)
        if load:
            n, gone = load_draft_picks(
                s, ctx.store.read(snap.storage_path), did, snap.id, snap.pulled_at
            )
            typer.echo(f"draft {did}: {n} picks synced, {gone} removed")


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


def _load_league(ctx: _Ctx, draft_id: str) -> None:
    """Latest valid snapshot of each league-state kind → core.drafts/draft_picks/rosters/users."""
    from lazy_sleeper.ingest.league_loaders import (
        load_draft,
        load_draft_picks,
        load_league_users,
        load_rosters,
    )

    with session_scope(ctx.sessions) as s:
        repo = SnapshotRepository(s)

        def latest(kind: str):  # noqa: ANN202
            snap = repo.latest(SnapshotKey("sleeper", kind))
            if snap is None:
                raise typer.BadParameter(
                    f"no valid sleeper/{kind} snapshot; run `lazy pull league`"
                )
            return snap, ctx.store.read(snap.storage_path)

        snap, payload = latest("league_users")
        typer.echo(f"users: {load_league_users(s, payload, snap.id)} (snapshot {snap.id})")
        snap, payload = latest("league_rosters")
        typer.echo(f"rosters: {load_rosters(s, payload, snap.id)} (snapshot {snap.id})")
        snap, payload = latest("draft")
        loaded_id = load_draft(s, payload, snap.id)
        typer.echo(f"draft: {loaded_id} (snapshot {snap.id})")
        if loaded_id != draft_id:
            typer.echo(
                f"  note: latest draft snapshot is {loaded_id}, syncing picks for {draft_id}"
            )
        snap, payload = latest("draft_picks")
        n, gone = load_draft_picks(s, payload, draft_id, snap.id, snap.pulled_at)
        typer.echo(f"picks: {n} synced, {gone} removed (snapshot {snap.id})")


@load_app.command("league")
def load_league_cmd(
    draft_id: str | None = typer.Option(
        None, help="Draft whose picks to sync (default: configured)"
    ),
) -> None:
    """Load the latest league-state snapshots into core.* (upsert; re-runnable any time)."""
    ctx = _Ctx()
    _load_league(ctx, draft_id or ctx.settings.sleeper_draft_id)


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
    source: str = typer.Option("sleeper", help="sleeper | espn | ensemble"),
    position: str | None = typer.Option(None, help="QB | RB | WR | TE | K | DEF"),
    top: int = typer.Option(25),
    actuals: bool = typer.Option(False, help="Score core.actuals instead of projections"),
) -> None:
    """Score the latest projection vintage (via a provider) or actuals and list the top players."""
    from sqlalchemy import select

    from lazy_sleeper.db.models import Actual, Player

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        if actuals:
            scorer = ctx.scorer(s)
            stmt = select(Actual).where(
                Actual.source == source,
                Actual.season == season,
                Actual.week.is_(None) if week is None else Actual.week == week,
            )
            if position:
                stmt = stmt.where(Actual.position == position)
            scored = [
                (
                    scorer.score(r.stats, r.position),
                    r.sleeper_id or r.source_player_id,
                    r.position,
                    r.team,
                    r.provider_points,
                    {},
                )
                for r in s.scalars(stmt)
            ]
        else:
            provider = ctx.provider(s, source)
            scored = [
                (p.points, p.sleeper_id, p.position, p.team, p.provider_points, p.components)
                for p in provider.projections(season, week)
                if position is None or p.position == position
            ]
        scored.sort(key=lambda t: -t[0])
        names = dict(
            s.execute(
                select(Player.sleeper_id, Player.full_name).where(
                    Player.sleeper_id.in_({t[1] for t in scored[:top]})
                )
            ).all()
        )
        members = sorted({k for t in scored[:top] for k in t[5]})
        extra = "".join(f"{m:>9}" for m in members)
        typer.echo(f"{'pos':<4}{'team':<5}{'player':<26}{'pts':>8}{'provider':>10}{extra}")
        for pts, sid, pos, team, prov_pts, comps in scored[:top]:
            name = names.get(sid, sid)
            prov = f"{prov_pts:.1f}" if prov_pts is not None else "-"
            comp = "".join(f"{comps[m]:>9.1f}" if m in comps else f"{'-':>9}" for m in members)
            typer.echo(f"{pos or '':<4}{team or '':<5}{name[:25]:<26}{pts:>8.1f}{prov:>10}{comp}")


# --- check -----------------------------------------------------------------
check_app = typer.Typer(no_args_is_help=True)
app.add_typer(check_app, name="check", help="Data-quality audits over raw/core")


@check_app.command("joins")
def check_joins(
    top_n: int = typer.Option(300, help="Top-N players by Sleeper search_rank to check"),
    min_points: float = typer.Option(20.0, help="Report unresolved rows with ≥ this many pts"),
) -> None:
    """Join coverage: crosswalk, sleeper_id resolution per feed, ESPN DST path, duplicates."""
    from lazy_sleeper.ingest import audit

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        c = audit.counts(s)
        xw = audit.crosswalk_report(s, top_n)
        res = audit.resolve_report(s, min_points)
        d = audit.def_report(s)
        dups = audit.duplicate_report(s)

    typer.echo(f"players {c.players} {c.players_by_position}  crosswalk {c.crosswalk}")
    typer.echo(
        f"projections {c.projections}  actuals {c.actuals}  adp {c.adp} ({c.adp_resolved} resolved)"
    )
    typer.echo(
        f"\ncrosswalk: {xw.rows} rows · sportradar {xw.with_sportradar} · gsis {xw.with_gsis} · "
        f"espn {xw.with_espn} · joined to players {xw.players_joined} · "
        f"sportradar agree {xw.sportradar_agree} / conflicts {len(xw.sportradar_conflicts)}"
    )
    for sid, pn, xn in xw.sportradar_conflicts[:10]:
        typer.echo(f"  conflict {sid}: players={pn!r} crosswalk={xn!r}")
    typer.echo(f"top-{xw.top_n} by search_rank in crosswalk: {xw.top_n_joined}/{xw.top_n}")
    for m in xw.top_n_misses:
        typer.echo(
            f"  miss #{m['search_rank']} {m['name']} {m['position']} {m['team']} "
            f"({m['sleeper_id']})"
        )

    typer.echo("\nsleeper_id resolution:")
    for r in res:
        typer.echo(f"  {r.table:<12}{r.source:<10}{r.resolved:>7}/{r.rows:<7} {r.rate:6.1%}")
        for u in r.unresolved_top:
            typer.echo(
                f"      unresolved {u['source_player_id']:<12}{u['position'] or '':<4}"
                f"{u['team'] or '':<4}s{u['season']} max {u['max_provider_points']:.1f} pts "
                f"({u['rows']} rows)"
            )

    status = "OK" if d.ok else "MISMATCH"
    typer.echo(
        f"\nESPN DST → Sleeper DEF: {status} · players DEF ids {len(d.players_def_ids)} · "
        f"espn DEF ids {len(d.espn_def_ids)} · unresolved rows {d.espn_def_unresolved}"
    )
    for name, got in (("players", d.players_def_ids), ("espn", d.espn_def_ids)):
        if got != audit.NFL_TEAMS:
            typer.echo(
                f"  {name}: missing {sorted(audit.NFL_TEAMS - got)} "
                f"extra {sorted(got - audit.NFL_TEAMS)}"
            )

    for dr in dups:
        typer.echo(f"\nduplicates in {dr.table}: {dr.duplicate_groups}")
        for ex in dr.examples:
            typer.echo(f"  {ex}")


@check_app.command("freshness")
def check_freshness(stale_hours: float = typer.Option(36.0)) -> None:
    """Newest snapshot per feed with its age; flags stale or invalid feeds."""
    from lazy_sleeper.ingest import audit

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        rows = audit.freshness(s)
    typer.echo(
        f"{'source':<10}{'kind':<20}{'season':<8}{'weeks':>6}{'age_h':>7}  {'rows':>7}  flag"
    )
    for f in rows:
        flag = ("STALE " if f.age_hours > stale_hours else "") + ("" if f.valid else "INVALID")
        typer.echo(
            f"{f.source:<10}{f.kind:<20}{str(f.season or '-'):<8}{f.weeks or '-':>6}"
            f"{f.age_hours:>7.1f}  {str(f.record_count or '-'):>7}  {flag}"
        )


@check_app.command("player")
def check_player(
    name: str = typer.Argument(..., help="Player name (any punctuation/case) or a sleeper_id"),
    team: str | None = typer.Option(None, "--team", "-t", help="Team abbr to disambiguate"),
    weeks: bool = typer.Option(False, help="Also list 2026 weekly projections"),
) -> None:
    """One-player dossier: identity across sources, projections vs our score, actuals, ADP."""
    from lazy_sleeper.ingest import audit
    from lazy_sleeper.scoring import default_scorer, load_league_rules

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        hits = audit.find_players(s, name, team)
        if not hits:
            typer.echo(f"no player matches {name!r}" + (f" on {team}" if team else ""))
            raise typer.Exit(1)
        if len(hits) > 1:
            typer.echo(f"{len(hits)} matches — pick one with --team or use the sleeper_id:")
            for p in hits[:15]:
                typer.echo(
                    f"  {p.sleeper_id:<8}{p.full_name:<26}{p.position or '':<4}{p.team or '-':<5}"
                    f"rank {p.search_rank}"
                )
            raise typer.Exit(1)
        scorer = default_scorer(load_league_rules(s, ctx.store))
        d = audit.player_dossier(s, hits[0], scorer)
        p, x = d.player, d.crosswalk
        typer.echo(
            f"{p.full_name}  {p.position} {p.team or '-'}  sleeper_id={p.sleeper_id}  "
            f"status={p.status} inj={p.injury_status or '-'} rank={p.search_rank} "
            f"exp={p.years_exp} depth={p.depth_chart_order}"
        )
        typer.echo(
            f"  players ids : espn={p.espn_id or '-'} gsis={p.gsis_id or '-'} "
            f"sportradar={p.sportradar_id or '-'}"
        )
        if x is None:
            typer.echo(
                "  crosswalk   : ABSENT (rookie/new signing? ids come from players/name tier)"
            )
        else:
            flag = ""
            if p.sportradar_id and x.sportradar_id and p.sportradar_id != x.sportradar_id:
                flag = "  <-- SPORTRADAR MISMATCH"
            typer.echo(
                f"  crosswalk   : {x.name} {x.position} espn={x.espn_id or '-'} "
                f"gsis={x.gsis_id or '-'} pfr={x.pfr_id or '-'} sportradar={x.sportradar_id or '-'}"
                f"{flag}"
            )
        if d.adp:
            typer.echo(
                f"  adp {d.adp['season']}: ppr={d.adp['adp_ppr']} half={d.adp['adp_half_ppr']} "
                f"std={d.adp['adp_std']}"
            )
        extra = "; +2026 weeks" if weeks else ""
        typer.echo(f"  projections (latest vintage per source/season{extra}):")
        typer.echo(
            f"    {'source':<9}{'season':<8}{'wk':<5}{'src id':<12}{'ours':>8}{'provider':>10}"
        )
        for r in d.projections:
            if r["week"] is not None and not weeks:
                continue
            prov = f"{r['provider_points']:.1f}" if r["provider_points"] is not None else "-"
            typer.echo(
                f"    {r['source']:<9}{r['season']:<8}{str(r['week'] or '-'):<5}"
                f"{r['source_player_id']:<12}{r['our_points']:>8.1f}{prov:>10}"
            )
        if d.actuals:
            typer.echo("  actuals (weekly rows summed):")
            hdr = f"    {'source':<9}{'season':<8}{'games':<7}{'src id':<12}"
            typer.echo(hdr + f"{'ours':>8}{'provider':>10}")
            for a in d.actuals:
                line = f"    {a['source']:<9}{a['season']:<8}{a['games']:<7}"
                line += f"{a['source_player_id']:<12}"
                typer.echo(line + f"{a['our_points']:>8.1f}{a['provider_points']:>10.1f}")


# --- benchmark -------------------------------------------------------------
SCOREBOARD_CSV = Path("data/benchmarks/season_scoreboard.csv")
WEEKLY_CSV = Path("data/benchmarks/weekly_scoreboard.csv")

bench_app = typer.Typer(no_args_is_help=True)
app.add_typer(bench_app, name="benchmark", help="Score providers against actuals")


def _pool_sizes(pool: list[str] | None) -> dict[str, int]:
    from lazy_sleeper.benchmark import season as bench

    sizes = dict(bench.DEFAULT_POOL_SIZES)
    for spec in pool or ():
        pos, _, n = spec.partition("=")
        sizes[pos.upper()] = int(n)
    return sizes


@bench_app.command("season")
def benchmark_season(
    seasons: Annotated[
        list[int] | None, typer.Option("--season", "-s", help="Repeatable; default 2024 2025")
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Scoreboard CSV path; --no-csv to skip")
    ] = SCOREBOARD_CSV,
    csv_out: Annotated[bool, typer.Option("--csv/--no-csv")] = True,
    players_out: Annotated[Path | None, typer.Option(help="Per-player detail CSV")] = None,
    pool: Annotated[
        list[str] | None,
        typer.Option("--pool", help="Override a pool size, e.g. --pool RB=48 (repeatable)"),
    ] = None,
    max_adp: Annotated[float, typer.Option(help="Ignore ADP beyond this pick")] = 300.0,
) -> None:
    """Season scoreboard: Sleeper / ESPN / naive vs scored actuals, top-N by ADP per position.

    Default pool: QB 24, RB 60, WR 72, TE 24, K 24, DEF 24 (top-N by preseason Sleeper ADP).
    """
    from lazy_sleeper.benchmark import report
    from lazy_sleeper.benchmark import season as bench
    from lazy_sleeper.scoring import default_scorer, load_league_rules

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        scorer = default_scorer(load_league_rules(s, ctx.store))
        rows, detail = bench.run(
            s, scorer, seasons or [2024, 2025], sizes=_pool_sizes(pool), max_adp=max_adp
        )

    fmt = report.fmt
    typer.echo(
        f"{'season':<7}{'pos':<5}{'provider':<9}{'pool':>5}{'n':>4}"
        f"{'mae':>8}{'bias':>8}{'rmse':>8}{'spearman':>9}{'mean_act':>10}"
    )
    for r in rows:
        typer.echo(
            f"{r.season:<7}{r.position:<5}{r.provider:<9}{r.n_pool:>5}{r.n:>4}"
            f"{fmt(r.mae, 8)}{fmt(r.bias, 8)}{fmt(r.rmse, 8)}{fmt(r.spearman, 9, 3)}"
            f"{fmt(r.mean_actual, 10, 1)}"
        )
    if csv_out and out:
        report.write_rows(out, rows)
        typer.echo(f"\nwrote {out}")
    if players_out:
        report.write_players(players_out, detail)
        typer.echo(f"wrote {players_out}")


@bench_app.command("weekly")
def benchmark_weekly(
    seasons: Annotated[
        list[int] | None, typer.Option("--season", "-s", help="Repeatable; default 2024 2025")
    ] = None,
    out: Annotated[
        Path | None, typer.Option(help="Roll-up CSV path; --no-csv to skip")
    ] = WEEKLY_CSV,
    csv_out: Annotated[bool, typer.Option("--csv/--no-csv")] = True,
    weeks_out: Annotated[Path | None, typer.Option(help="Per-week rows CSV")] = None,
    players_out: Annotated[Path | None, typer.Option(help="Per-player-week detail CSV")] = None,
    pool: Annotated[
        list[str] | None,
        typer.Option("--pool", help="Override a pool size, e.g. --pool RB=48 (repeatable)"),
    ] = None,
    max_adp: Annotated[float, typer.Option(help="Ignore ADP beyond this pick")] = 300.0,
) -> None:
    """Weekly scoreboard: pre-game Sleeper / ESPN / naive vs each week's scored actuals.

    Pool per week = the season ADP pool ∩ players some provider expected to play. MAE/bias/RMSE
    are pooled over player-weeks; spearman is the mean (and min) of per-week rank correlations.
    """
    from lazy_sleeper.benchmark import report
    from lazy_sleeper.benchmark import weekly as bench
    from lazy_sleeper.scoring import default_scorer, load_league_rules

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        scorer = default_scorer(load_league_rules(s, ctx.store))
        rows, week_rows, detail = bench.run(
            s, scorer, seasons or [2024, 2025], sizes=_pool_sizes(pool), max_adp=max_adp
        )

    fmt = report.fmt
    typer.echo(
        f"{'season':<7}{'pos':<5}{'provider':<9}{'wks':>4}{'pool':>6}{'n':>6}"
        f"{'mae':>7}{'bias':>7}{'rmse':>7}{'spearman':>9}{'min':>7}{'mean_act':>10}"
    )
    for r in rows:
        typer.echo(
            f"{r.season:<7}{r.position:<5}{r.provider:<9}{r.weeks:>4}{r.n_pool:>6}{r.n:>6}"
            f"{fmt(r.mae, 7)}{fmt(r.bias, 7)}{fmt(r.rmse, 7)}{fmt(r.spearman, 9, 3)}"
            f"{fmt(r.spearman_min, 7, 3)}{fmt(r.mean_actual, 10, 1)}"
        )
    if csv_out and out:
        report.write_rows(out, rows)
        typer.echo(f"\nwrote {out}")
    if weeks_out:
        report.write_rows(weeks_out, week_rows)
        typer.echo(f"wrote {weeks_out}")
    if players_out:
        report.write_players(players_out, detail)
        typer.echo(f"wrote {players_out}")


WEIGHTS_JSON = Path("data/benchmarks/ensemble_weights.json")


@bench_app.command("fit-weights")
def benchmark_fit_weights(
    season_csv: Annotated[Path, typer.Option(help="Season scoreboard CSV")] = SCOREBOARD_CSV,
    weekly_csv: Annotated[Path, typer.Option(help="Weekly scoreboard CSV")] = WEEKLY_CSV,
    out: Annotated[Path, typer.Option(help="Committed JSON artefact")] = WEIGHTS_JSON,
    note: Annotated[str | None, typer.Option(help="Stored with the fitted version")] = None,
) -> None:
    """Fit inverse-MAE blend weights from the scoreboards → new fitted version + JSON artefact."""
    import json
    from datetime import UTC, datetime

    from lazy_sleeper.providers import WeightRepository, fit_from_csvs, to_json

    fitted = fit_from_csvs(season_csv, weekly_csv)
    if not fitted:
        raise typer.BadParameter(f"nothing to fit from {season_csv} / {weekly_csv}")
    fitted_at = datetime.now(UTC)
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        version = WeightRepository(s).store_fitted(fitted, fitted_at=fitted_at, note=note)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(to_json(fitted, version, fitted_at), indent=2) + "\n")
    typer.echo(f"{'horizon':<8}{'pos':<5}{'provider':<9}{'weight':>8}{'mae':>9}{'n':>7}")
    for f in fitted:
        typer.echo(
            f"{f.horizon:<8}{f.position:<5}{f.provider:<9}{f.weight:>8.3f}{f.mae:>9.2f}{f.n:>7}"
        )
    typer.echo(f"\nstored derived.ensemble_weights version {version}; wrote {out}")


# --- weights ---------------------------------------------------------------
weights_app = typer.Typer(no_args_is_help=True)
app.add_typer(
    weights_app, name="weights", help="Ensemble blend weights: fitted vs manual overrides"
)


@weights_app.command("show")
def weights_show(horizon: str = typer.Option("season", help="season | weekly")) -> None:
    """Weights in force per position, plus the fitted/override rows behind them."""
    from lazy_sleeper.providers import WeightRepository

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        repo = WeightRepository(s)
        cfg = repo.config()
        latest = repo.latest_version()
        typer.echo(
            f"use_overrides={cfg.use_overrides}  weights_version="
            f"{cfg.weights_version if cfg.weights_version is not None else f'latest ({latest})'}"
        )
        fitted = repo.fitted(cfg.weights_version)
        overrides = repo.overrides()
        resolved = repo.resolve_all(horizon)
        typer.echo(f"\n{horizon}: {'pos':<5}{'in force':<40}{'source':<10}{'fitted':<28}override")
        for pos in sorted(
            set(resolved)
            | {p for h, p in fitted if h == horizon}
            | {p for h, p in overrides if h == horizon}
        ):
            r = resolved.get(pos)
            fmt = lambda d: " ".join(f"{k}={v:.3f}" for k, v in sorted(d.items())) or "-"  # noqa: E731
            typer.echo(
                f"{'':<{len(horizon) + 2}}{pos:<5}{fmt(r.weights) if r else '-':<40}"
                f"{r.source if r else '-':<10}{fmt(fitted.get((horizon, pos), {})):<28}"
                f"{fmt(overrides.get((horizon, pos), {}))}"
            )


@weights_app.command("set")
def weights_set(
    position: Annotated[str, typer.Argument(help="QB | RB | WR | TE | K | DEF")],
    weights: Annotated[
        list[str], typer.Argument(help="provider=weight …, e.g. sleeper=0.7 espn=0.3")
    ],
    horizon: str = typer.Option("season", help="season | weekly"),
    note: str | None = typer.Option(None),
    enable: bool = typer.Option(False, help="Also switch use_overrides on"),
) -> None:
    """Set the manual (λ) override for one position; normalized on read."""
    from lazy_sleeper.providers import WeightRepository

    parsed = {}
    for spec in weights:
        name, _, val = spec.partition("=")
        parsed[name] = float(val)
    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        repo = WeightRepository(s)
        repo.set_override(horizon, position.upper(), parsed, note)
        if enable:
            repo.set_config(use_overrides=True)
        cfg = repo.config()
    typer.echo(
        f"override {horizon}/{position.upper()} = {parsed} (use_overrides={cfg.use_overrides})"
    )


@weights_app.command("clear")
def weights_clear(
    position: str | None = typer.Argument(None, help="Omit to clear every position"),
    horizon: str = typer.Option("season", help="season | weekly"),
) -> None:
    """Remove manual overrides (does not touch fitted weights or the use_overrides flag)."""
    from lazy_sleeper.providers import WeightRepository

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        n = WeightRepository(s).clear_override(horizon, position.upper() if position else None)
    typer.echo(f"cleared {n} override rows")


@weights_app.command("config")
def weights_config(
    use_overrides: bool | None = typer.Option(None, "--use-overrides/--use-fitted"),
    version: str | None = typer.Option(None, help="Pin a fitted version, or 'latest'"),
) -> None:
    """Flip between manual overrides and fitted weights; optionally pin a fitted version."""
    from lazy_sleeper.providers import WeightRepository

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        repo = WeightRepository(s)
        pin: int | None | str = "keep"
        if version is not None:
            pin = None if version == "latest" else int(version)
        cfg = repo.set_config(use_overrides=use_overrides, weights_version=pin)
        latest = repo.latest_version()
        typer.echo(
            f"use_overrides={cfg.use_overrides}  weights_version="
            f"{cfg.weights_version if cfg.weights_version is not None else f'latest ({latest})'}"
        )


# --- board -----------------------------------------------------------------
board_app = typer.Typer(no_args_is_help=True)
app.add_typer(board_app, name="board", help="Draft board: baselines, VORP, tiers")


@board_app.command("baselines")
def board_baselines(
    season: int = typer.Option(2026, help="Season for the live projection baseline"),
    provider: str = typer.Option("ensemble", help="sleeper | espn | ensemble"),
) -> None:
    """Replacement-level points per position: 2023–25 actuals average vs live projections.

    Cutoffs come from the league roster (teams × dedicated slots + flex filled greedily by
    value), so they move with the points table instead of being hardcoded.
    """
    from lazy_sleeper.board import RosterShape, historical_baselines, live_baselines
    from lazy_sleeper.scoring import default_scorer, load_league_rules

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        rules = load_league_rules(s, ctx.store)
        shape = RosterShape.from_rules(rules)
        hist = historical_baselines(s, default_scorer(rules), shape)
        live = live_baselines(ctx.provider(s, provider), shape, season)

    slots = " ".join(f"{p}{n}" for p, n in shape.dedicated.items())
    flex = ", ".join("/".join(e) for e in shape.flex) or "none"
    typer.echo(f"{shape.teams} teams  {slots}  flex: {flex}")

    typer.echo("\nhistorical actuals (baseline pts @ cutoff rank):")
    header = f"{'pos':<5}" + "".join(f"{yr:>14}" for yr in hist.seasons) + f"{'avg':>9}"
    typer.echo(header)
    for pos in [p for p in ("QB", "RB", "WR", "TE", "K", "DEF") if p in hist.average]:
        cells = ""
        for yr in hist.seasons:
            b = hist.per_season[yr].get(pos)
            cells += f"{f'{b.points:.1f} @{b.cutoff_rank}':>14}" if b else f"{'-':>14}"
        typer.echo(f"{pos:<5}{cells}{hist.average[pos]:>9.1f}")

    typer.echo(f"\nlive {provider} {season} projections:")
    typer.echo(f"{'pos':<5}{'cutoff':>7}{'baseline':>10}{'flex_fills':>12}")
    for pos in [p for p in ("QB", "RB", "WR", "TE", "K", "DEF") if p in live]:
        b = live[pos]
        typer.echo(f"{pos:<5}{b.cutoff_rank:>7}{b.points:>10.1f}{b.flex_fills:>12}")


@board_app.command("vorp")
def board_vorp(
    season: int = typer.Option(2026, help="Season to rank"),
    provider: str = typer.Option("ensemble", help="sleeper | espn | ensemble"),
    baseline: str = typer.Option("live", help="live | historical"),
    position: str | None = typer.Option(None, "--position", "-p", help="Filter one position"),
    top: int = typer.Option(50, help="Rows to print"),
    cliff_gap: float | None = typer.Option(None, help="Override the stored cliff threshold"),
    flags_only: bool = typer.Option(False, help="Only rows with a value/reach or DISAGREE flag"),
) -> None:
    """The draft board: VORP with tier/cliff, ADP-delta and provider-disagreement columns.

    `live` measures against a baseline derived from the same projections (provider bias
    cancels; the last starter per position sits at exactly 0). `historical` measures against
    the 2023–25 actuals average instead. `Δadp` = Sleeper ADP − board rank (+ = value, − =
    reach); `spread` = Sleeper-vs-ESPN league-scored gap, position-debiased by default. All
    thresholds come from `derived.board_config` (`lazy board config` / `PUT /board/config`).
    """
    from dataclasses import replace

    from sqlalchemy import select

    from lazy_sleeper.board import (
        BoardConfigRepository,
        RosterShape,
        build_board,
        historical_baselines,
        latest_adp,
    )
    from lazy_sleeper.db.models import Player
    from lazy_sleeper.scoring import default_scorer, load_league_rules

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        rules = load_league_rules(s, ctx.store)
        shape = RosterShape.from_rules(rules)
        prov = ctx.provider(s, provider)
        if baseline == "live":
            baselines = None
        elif baseline == "historical":
            baselines = historical_baselines(s, default_scorer(rules), shape).average
        else:
            raise typer.BadParameter("baseline must be live | historical")
        config = BoardConfigRepository(s).get()
        if cliff_gap is not None:
            config = replace(config, cliff_gap=cliff_gap)
        board = build_board(prov, shape, season, config, latest_adp(s, season), baselines=baselines)
        if position:
            board = [r for r in board if r.value.position == position.upper()]
        if flags_only:
            board = [r for r in board if r.adp_flag or r.disagree]
        rows = board[:top]
        names = dict(
            s.execute(
                select(Player.sleeper_id, Player.full_name).where(
                    Player.sleeper_id.in_({r.value.sleeper_id for r in rows})
                )
            ).all()
        )
    typer.echo(
        f"{'rk':<4}{'pos':<5}{'team':<5}{'player':<26}{'pts':>8}{'base':>8}{'vorp':>8}"
        f"{'pos_rk':>7}{'tier':>6}{'gap':>7}{'adp':>7}{'dadp':>7}{'spread':>8}  flags"
    )
    for i, r in enumerate(rows, start=1):
        v = r.value
        name = names.get(v.sleeper_id, v.sleeper_id)
        tier = str(r.tier) if r.tier is not None else "-"
        gap = f"{r.gap_to_next:.1f}" if r.gap_to_next is not None else "-"
        adp = f"{r.adp:.1f}" if r.adp is not None else "-"
        delta = f"{r.adp_delta:+.0f}" if r.adp_delta is not None else "-"
        spread = f"{r.spread:.1f}" if r.spread is not None else "-"
        flags = " ".join(
            f
            for f in (
                "CLIFF" if r.cliff else "",
                r.adp_flag or "",
                "DISAGREE" if r.disagree else "",
            )
            if f
        )
        typer.echo(
            f"{i:<4}{v.position:<5}{v.team or '':<5}{name[:25]:<26}"
            f"{v.points:>8.1f}{v.baseline:>8.1f}{v.vorp:>8.1f}{v.pos_rank:>7}"
            f"{tier:>6}{gap:>7}{adp:>7}{delta:>7}{spread:>8}  {flags}"
        )


@board_app.command("config")
def board_config_cmd(
    cliff_gap: float | None = typer.Option(None, help="Season-points drop → cliff flag"),
    gap_multiplier: float | None = typer.Option(None, help="Tier break at × median gap"),
    min_gap: float | None = typer.Option(None, help="Tier-break floor in season points"),
    adp_min_delta: float | None = typer.Option(None, help="ADP-delta flag floor in picks"),
    adp_pct: float | None = typer.Option(None, help="ADP-delta flag as a fraction of ADP"),
    disagree_min_pts: float | None = typer.Option(None, help="Disagreement floor, season pts"),
    disagree_pct: float | None = typer.Option(None, help="Disagreement as a fraction of points"),
    debias: bool | None = typer.Option(
        None, "--debias/--no-debias", help="Remove position-level provider bias before comparing"
    ),
    survival_sigma_min: float | None = typer.Option(None, help="ADP scatter floor, picks (LS-33)"),
    survival_sigma_pct: float | None = typer.Option(None, help="ADP scatter as a fraction of ADP"),
    demand_shift: float | None = typer.Option(None, help="Window stretch per unit relative demand"),
    need_bonus: float | None = typer.Option(None, help="pick_score points per unit of my need"),
    run_window: int | None = typer.Option(None, help="Picks looked back for a run"),
    run_threshold: int | None = typer.Option(None, help="Run when this many in the window"),
    run_streak: int | None = typer.Option(None, help="...or this many consecutive"),
) -> None:
    """Show (no options) or update the stored tier/cliff/flag/draft-signal thresholds."""
    from lazy_sleeper.board import BoardConfigRepository

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        repo = BoardConfigRepository(s)
        try:
            cfg = repo.set(
                cliff_gap=cliff_gap,
                gap_multiplier=gap_multiplier,
                min_gap=min_gap,
                adp_min_delta=adp_min_delta,
                adp_pct=adp_pct,
                disagree_min_pts=disagree_min_pts,
                disagree_pct=disagree_pct,
                debias_disagreement=debias,
                survival_sigma_min=survival_sigma_min,
                survival_sigma_pct=survival_sigma_pct,
                demand_shift=demand_shift,
                need_bonus=need_bonus,
                run_window=run_window,
                run_threshold=run_threshold,
                run_streak=run_streak,
            )
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e
    typer.echo(
        f"cliff_gap={cfg.cliff_gap:g}  gap_multiplier={cfg.gap_multiplier:g}  "
        f"min_gap={cfg.min_gap:g}\n"
        f"adp_min_delta={cfg.adp_min_delta:g}  adp_pct={cfg.adp_pct:g}\n"
        f"disagree_min_pts={cfg.disagree_min_pts:g}  disagree_pct={cfg.disagree_pct:g}  "
        f"debias_disagreement={cfg.debias_disagreement}\n"
        f"survival_sigma_min={cfg.survival_sigma_min:g}  "
        f"survival_sigma_pct={cfg.survival_sigma_pct:g}  "
        f"demand_shift={cfg.demand_shift:g}  need_bonus={cfg.need_bonus:g}\n"
        f"run_window={cfg.run_window}  run_threshold={cfg.run_threshold}  "
        f"run_streak={cfg.run_streak}"
    )


BOARDS_DIR = Path("data/boards")


@board_app.command("regen")
def board_regen(
    season: int = typer.Option(2026, help="Season to rank"),
    provider: str = typer.Option("ensemble", help="sleeper | espn | ensemble"),
    baseline: str = typer.Option("live", help="live | historical"),
    out: Annotated[Path, typer.Option(help="Directory for the CSV + HTML exports")] = BOARDS_DIR,
) -> None:
    """Regenerate the board from what's in core.* and persist it as a new dated board.

    Writes `derived.boards` + `derived.board_rows` (what `GET /board` serves) and
    `<out>/board_<season>_<provider>_<UTC stamp>.{csv,html}` plus `board_latest.{csv,html}`.
    Runs after the daily pull in CI; safe to re-run any time (every run is a new board).
    """
    from lazy_sleeper.board import board_meta, regenerate, to_csv, to_html
    from lazy_sleeper.scoring import default_scorer, load_league_rules

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        rules = load_league_rules(s, ctx.store)
        try:
            board, rows = regenerate(
                s,
                ctx.provider(s, provider),
                rules,
                default_scorer(rules),
                season,
                baseline=baseline,
            )
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e
        meta = board_meta(board)
    out.mkdir(parents=True, exist_ok=True)
    stamp = meta["generated_at"].strftime("%Y%m%dT%H%M%SZ")
    csv_text, html_text = to_csv(rows), to_html(meta, rows)
    for stem in (f"board_{season}_{provider}_{stamp}", "board_latest"):
        (out / f"{stem}.csv").write_text(csv_text, encoding="utf-8")
        (out / f"{stem}.html").write_text(html_text, encoding="utf-8")
    flagged = sum(1 for r in rows if r["adp_flag"] or r["disagree"])
    typer.echo(
        f"board #{meta['id']} {season} {provider}/{baseline}: {len(rows)} rows, "
        f"{flagged} flagged, {sum(1 for r in rows if r['cliff'])} cliffs -> {out}/board_latest.*"
    )


# --- sync ------------------------------------------------------------------
sync_app = typer.Typer(no_args_is_help=True)
app.add_typer(sync_app, name="sync", help="Reconcile the local archive with Supabase Storage")


def _sync_report(action: str, rep) -> None:  # noqa: ANN001
    typer.echo(
        f"{action}: uploaded={rep.uploaded} already_remote={rep.already_remote} "
        f"downloaded={rep.downloaded} skipped={rep.skipped} "
        f"missing_local={len(rep.missing_local)} failed={len(rep.failed)}"
    )
    for p in rep.missing_local[:20]:
        typer.echo(f"  missing locally: {p}")
    for p, err in rep.failed[:20]:
        typer.echo(f"  FAILED {p}: {err}")
    if rep.failed:
        raise typer.Exit(code=1)


@sync_app.command("push")
def sync_push(
    verify: bool = typer.Option(False, help="Re-check rows that already claim a remote_path"),
    dry_run: bool = typer.Option(False, help="Report only"),
) -> None:
    """Upload every registered snapshot the mirror lacks; sets raw.snapshots.remote_path."""
    from lazy_sleeper.ingest.sync import Syncer

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        _sync_report("push", Syncer(s, ctx.store).push(verify=verify, dry_run=dry_run))


@sync_app.command("pull")
def sync_pull(dry_run: bool = typer.Option(False, help="Report only")) -> None:
    """Download every registered snapshot missing from the local archive (fresh machine / CI)."""
    from lazy_sleeper.ingest.sync import Syncer

    ctx = _Ctx()
    with session_scope(ctx.sessions) as s:
        _sync_report("pull", Syncer(s, ctx.store).pull(dry_run=dry_run))


# --- draft (M4) ------------------------------------------------------------


class _Adviser:
    """Pre-draft board built once; draft state rebuilt from core.draft_picks on each refresh."""

    def __init__(self, ctx: _Ctx, draft_id: str, season: int, top: int) -> None:
        from sqlalchemy import select

        from lazy_sleeper.board import BoardConfigRepository, RosterShape, build_board, latest_adp
        from lazy_sleeper.db.models import Player
        from lazy_sleeper.draft.signals import SearchRankAdp
        from lazy_sleeper.draft.state import DraftSpec
        from lazy_sleeper.scoring import load_league_rules

        self._ctx, self._draft_id, self._top = ctx, draft_id, top
        with session_scope(ctx.sessions) as s:
            rules = load_league_rules(s, ctx.store)
            self._cfg = BoardConfigRepository(s).get()
            self._rows = build_board(
                ctx.provider(s, "ensemble"), RosterShape.from_rules(rules), season, self._cfg,
                latest_adp(s, season),
            )  # fmt: skip
            self._adp = latest_adp(s, season)
            players = s.execute(
                select(Player.sleeper_id, Player.full_name, Player.position, Player.team,
                       Player.search_rank)
            ).all()  # fmt: skip
        self._names = {p[0]: f"{p[1]} {p[2]}/{p[3]}" for p in players}
        self._positions = {p[0]: p[2] for p in players}
        self._search_rank = {p[0]: p[4] for p in players if p[4] is not None}
        self._rank_map = SearchRankAdp(
            (self._search_rank[sid], adp)
            for sid, adp in self._adp.items()
            if sid in self._search_rank
        )
        self._spec_rules = rules
        self._spec = DraftSpec.build(rules)
        self.state = None
        self.refresh(None)

    def refresh(self, draft_doc: dict | None) -> None:
        from sqlalchemy import select

        from lazy_sleeper.db.models import Draft, DraftPick
        from lazy_sleeper.draft.state import DraftSpec, DraftState, resolve_my_slot

        with session_scope(self._ctx.sessions) as s:
            row = s.get(Draft, self._draft_id)
            doc = draft_doc or (
                {"teams": row.teams, "rounds": row.rounds, "type": row.type,
                 "draft_order": row.draft_order} if row else {}
            )  # fmt: skip
            picks = s.execute(
                select(DraftPick.pick_no, DraftPick.draft_slot, DraftPick.sleeper_id,
                       DraftPick.metadata_).where(DraftPick.draft_id == self._draft_id)
            ).all()  # fmt: skip
        self._spec = DraftSpec.build(self._spec_rules, doc)
        my_slot = resolve_my_slot(
            self._ctx.settings.my_draft_slot,
            doc.get("draft_order"),
            self._ctx.settings.sleeper_user_id,
        )
        st = DraftState(self._spec, my_slot=my_slot, position_of=self._positions.get)
        st.rebuild(
            {"pick_no": p[0], "draft_slot": p[1], "sleeper_id": p[2], "metadata_": p[3]}
            for p in picks
        )
        self.state = st

    def on_the_clock(self) -> bool:
        st = self.state
        return st.my_slot is not None and st.on_the_clock == st.my_slot

    def advice(self) -> list:  # noqa: ANN201 — list[BoardRow]
        from lazy_sleeper.draft.signals import advise

        return advise(
            self._rows, self.state, self._adp, self._cfg,
            search_rank_by_id=self._search_rank, rank_map=self._rank_map,
        )  # fmt: skip

    def render(self, position: str | None = None) -> str:
        st = self.state
        rows = self.advice()
        if position:
            rows = [r for r in rows if r.value.position == position.upper()]
        until = st.picks_until_my_turn()
        mine = st.my_roster()
        head = (
            f"pick {st.current_pick}/{st.spec.total_picks}  on the clock: slot {st.on_the_clock}  "
            f"my slot: {st.my_slot or '?'}  until my turn: {'?' if until is None else until}"
        )
        needs = (
            f"my open starters: {mine.open_starters or 'none'}  bench open: {mine.open_bench}"
            if mine
            else "my roster: unknown slot"
        )
        lines = [
            head,
            needs,
            f"{'score':>6} {'vorp':>6} {'surv':>5} {'adp':>6} {'tier':>4} {'run':>3}  player",
        ]
        for r in rows[: self._top]:
            v = r.value
            surv = "  n/a" if r.survival is None else f"{r.survival:5.2f}"
            adp = "     -" if r.adp is None else f"{r.adp:6.1f}"
            tier = "   -" if r.tier is None else f"{r.tier:4d}"
            run = f"{'RUN' if r.run else '':>3}"
            cliff = " CLIFF" if r.cliff else ""
            lines.append(
                f"{r.pick_score:6.1f} {v.vorp:6.1f} {surv} {adp} {tier} {run}  "
                f"{self._names.get(v.sleeper_id, v.sleeper_id)}{cliff}"
            )
        return "\n".join(lines)


@draft_app.command("advise")
def draft_advise(
    draft_id: str | None = typer.Option(None, help="Override the configured draft (e.g. a mock)"),
    top: int = typer.Option(12, help="Rows to show"),
    position: str | None = typer.Option(None, help="Only this position"),
    season: int = typer.Option(2026, help="Board season (projections/ADP)"),
) -> None:
    """Who should I take now? Available board by pick_score = VORP − expected best alternative
    at my next pick + need bonus; with survival, run and cliff flags (LS-33). Reads the draft
    state from core.draft_picks — run `lazy draft poll` (or `lazy pull picks --load`) first."""
    ctx = _Ctx()
    did = draft_id or ctx.settings.sleeper_draft_id
    typer.echo(_Adviser(ctx, did, season, top).render(position))


def _draft_log_file(draft_id: str) -> Path:
    """Route INFO+ logging to data/logs/draft_poll_<id>_<stamp>.log; console keeps WARNING+.
    The poller logs one `poll …` and one `pick …` key=value line per event, so the file is the
    parseable record of the night (picks, timings, backoff) while the terminal shows only picks."""
    logs = Path("data/logs")
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"draft_poll_{draft_id}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.log"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in root.handlers:  # the basicConfig stream handler from _root()
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(logging.WARNING)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(fh)
    return path


@draft_app.command("poll")
def draft_poll(
    draft_id: str | None = typer.Option(None, help="Override the configured draft (e.g. a mock)"),
    interval: float = typer.Option(5.0, help="Seconds between polls"),
    max_backoff: float = typer.Option(60.0, help="Cap on the error backoff, seconds"),
    once: bool = typer.Option(False, help="Poll a single time and exit"),
    forever: bool = typer.Option(False, help="Keep polling after the draft reports complete"),
    advise: bool = typer.Option(False, help="Print my top picks whenever I'm on the clock (LS-33)"),
    top: int = typer.Option(12, help="Rows in the advice table"),
    season: int = typer.Option(2026, help="Board season (projections/ADP)"),
) -> None:
    """Poll /draft/{id}/picks until the draft completes (LS-31). Every poll is snapshotted;
    core.draft_picks is synced; each new pick is printed as it lands. Ctrl-C stops cleanly."""
    import signal
    import threading

    from sqlalchemy import select

    from lazy_sleeper.db.models import Player
    from lazy_sleeper.draft.poller import DbPickSink, DraftPoller, PickEvent, SleeperPickSource

    ctx = _Ctx()
    did = draft_id or ctx.settings.sleeper_draft_id
    me = ctx.settings.sleeper_user_id
    log_path = _draft_log_file(did)
    source = SleeperPickSource(ctx.sessions, ctx.puller, SleeperClient(ctx.http), did)
    poller = DraftPoller(
        source, DbPickSink(ctx.sessions, did), did, interval_s=interval, max_backoff_s=max_backoff
    )
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    names: dict[str, str] = {}

    def name_of(sleeper_id: str | None, meta: dict | None) -> str:
        if sleeper_id and sleeper_id not in names:
            with session_scope(ctx.sessions) as s:
                row = s.execute(
                    select(Player.full_name, Player.position, Player.team).where(
                        Player.sleeper_id == sleeper_id
                    )
                ).first()
            if row:
                names[sleeper_id] = f"{row[0]} {row[1]}/{row[2]}"
        if sleeper_id in names:
            return names[sleeper_id]
        m = meta or {}
        return f"{m.get('first_name', '?')} {m.get('last_name', '')} {m.get('position', '?')}"

    def on_pick(ev: PickEvent) -> None:
        mine = ev.picked_by == me or (ev.picked_by is None and poller.my_slot(me) == ev.draft_slot)
        tag = " <-- you" if mine else ""
        auto = " (auto)" if ev.picked_by is None else ""
        who = name_of(ev.sleeper_id, ev.metadata)
        typer.echo(f"#{ev.pick_no:3d} R{ev.round}.{ev.draft_slot:<2d} {who}{auto}{tag}")

    adviser = _Adviser(ctx, did, season, top) if advise else None

    def on_poll(r) -> None:  # noqa: ANN001
        if adviser and (r.new or r.poll_seq == 1 or r.removed):
            adviser.refresh(poller.draft)
            if adviser.on_the_clock():
                typer.echo(adviser.render())
        if r.unchanged or r.new:
            return
        typer.echo(f"poll {r.poll_seq}: {r.picks} picks, {r.removed} removed, status {r.status}")

    typer.echo(f"polling draft {did} every {interval:g}s, log: {log_path}")
    summary = poller.run(
        on_pick,
        on_poll=on_poll,
        stop=stop,
        until_complete=not forever,
        max_polls=1 if once else None,
    )
    exp = poller.expected_picks
    slot = poller.my_slot(me)
    typer.echo(
        f"done: my slot {slot or '?'}, {summary.polls} polls, {summary.events} new picks, "
        f"{summary.failures} failures, status {poller.status}"
        + (f" ({exp} expected)" if exp else "")
        + (" [stopped]" if summary.stopped else "")
    )
