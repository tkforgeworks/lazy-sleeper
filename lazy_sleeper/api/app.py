"""FastAPI application. M0: health + snapshot inventory; LS-25: ensemble weights switchboard;
LS-28/29 board config; LS-30 `/board` (latest persisted board), `/board.html`, `POST /board/regen`;
LS-35 `/draft/{id}/state` served from an in-process `DraftHost` (`POST /draft/{id}/start|stop`);
LS-37 `/draft/{id}/state.html` + `/draft.html` — the draft-night fallback page — and
`/board/config.html`, the tuning page (`PUT /board/config` + `POST /draft/{id}/config`)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lazy_sleeper.config import Settings, get_settings
from lazy_sleeper.db.models import Snapshot
from lazy_sleeper.db.session import make_engine, make_session_factory
from lazy_sleeper.ingest.http import HttpClient
from lazy_sleeper.ingest.sleeper import SleeperClient
from lazy_sleeper.ingest.snapshots import store_from_settings
from lazy_sleeper.providers import SEASON, WEEKLY, WeightRepository, make_provider

DEFAULT_SEASON = 2026


class RegenBody(BaseModel):
    season: int = DEFAULT_SEASON
    provider: str = Field("ensemble", pattern="^(sleeper|espn|ensemble)$")
    baseline: str = Field("live", pattern="^(live|historical)$")


class OverrideBody(BaseModel):
    horizon: str = Field(pattern=f"^({SEASON}|{WEEKLY})$")
    position: str = Field(min_length=1, max_length=8)
    weights: dict[str, float]  # provider → weight (normalized on read)
    note: str | None = None


class ConfigBody(BaseModel):
    use_overrides: bool | None = None
    weights_version: int | None = None  # pin a fitted version
    latest: bool = False  # True → clear the pin (use latest fitted)


class BoardConfigBody(BaseModel):
    cliff_gap: float | None = Field(None, gt=0)
    gap_multiplier: float | None = Field(None, gt=0)
    min_gap: float | None = Field(None, gt=0)
    adp_min_delta: float | None = Field(None, gt=0)
    adp_pct: float | None = Field(None, gt=0)
    disagree_min_pts: float | None = Field(None, gt=0)
    disagree_pct: float | None = Field(None, gt=0)
    debias_disagreement: bool | None = None
    # LS-33 draft-time signal dials
    survival_sigma_min: float | None = Field(None, gt=0)
    survival_sigma_pct: float | None = Field(None, gt=0)
    demand_shift: float | None = Field(None, gt=0)
    need_bonus: float | None = Field(None, gt=0)
    run_window: int | None = Field(None, ge=1)
    run_threshold: int | None = Field(None, ge=1)
    run_streak: int | None = Field(None, ge=1)
    stream_depth: int | None = Field(None, ge=0)
    late_rounds: int | None = Field(None, ge=0)


class DraftStartBody(BaseModel):
    season: int = DEFAULT_SEASON
    forever: bool = False  # keep polling after the draft reports complete
    interval_s: float | None = Field(None, ge=0.5, le=60)  # poll cadence; default 2 s


class DraftSpecOut(BaseModel):
    teams: int
    rounds: int
    type: str
    total_picks: int


class DraftClockOut(BaseModel):
    current_pick: int
    round: int | None
    on_the_clock: int | None
    my_slot: int | None
    my_turn: bool
    my_next_pick: int | None
    picks_until_my_turn: int | None
    picks_made: int
    complete: bool


class SeatedOut(BaseModel):
    pick_no: int
    sleeper_id: str | None
    name: str | None
    position: str | None
    seat: str


class RosterOut(BaseModel):
    slot: int
    picks: list[SeatedOut]
    counts: dict[str, int]
    open_starters: dict[str, int]
    open_flex: int
    open_bench: int
    needs: dict[str, float]


class RecomputeOut(BaseModel):
    seq: int
    pick_no: int
    computed_at: datetime
    elapsed_ms: float
    stale: bool
    error: str | None
    count: int
    avg_ms: float
    max_ms: float
    failures: int


class BoardMetaOut(BaseModel):
    built_at: datetime
    season: int | None
    rows: int
    available: int


class PersistOut(BaseModel):
    """The off-thread writer (snapshots + `core.draft_picks`): `pending` > 0 with
    `failures_in_a_row` > 0 means the DB is behind Sleeper — advice is unaffected (LS-62)."""

    pending: int
    applied: int
    failures: int
    failures_in_a_row: int
    dropped: int
    last_error: str | None


class PollerOut(BaseModel):
    interval_s: float
    status: str | None
    expected_picks: int | None
    started_at: datetime | None
    last_poll_at: datetime | None  # when the last poll *started* (success or not)
    last_ok_at: datetime | None  # when the last poll succeeded
    failures_in_a_row: int  # > 0 = the Sleeper fetch is failing and the poller is backing off
    last_error: str | None
    degraded: bool  # the start-of-run DB read failed; picks were re-emitted from the payload
    runner_error: str | None  # the runner thread died with this; `running` is then False
    rebuild_pending: bool  # a state rebuild raised and will be retried on the next changed poll
    persist: PersistOut
    summary: dict[str, Any] | None


class DraftRowOut(BaseModel):
    """One available player, best pick first (``rank`` = overall pick_score order)."""

    rank: int
    sleeper_id: str
    name: str
    position: str
    team: str | None
    injury_status: str | None
    points: float
    vorp: float
    pos_rank: int
    tier: int | None
    cliff: bool
    gap_to_next: float | None
    adp: float | None
    adp_delta: float | None
    adp_flag: str | None
    disagree: bool
    survival: float | None
    run: bool
    run_count: int
    pick_score: float | None


class DraftStateOut(BaseModel):
    """The decision surface: who's on the clock, my roster and needs, and the available board
    ordered by pick_score with tier / cliff / run / survival — from the latest recompute."""

    draft_id: str
    spec: DraftSpecOut
    clock: DraftClockOut
    my_roster: RosterOut | None
    recompute: RecomputeOut
    board: BoardMetaOut
    poller: PollerOut
    running: bool | None
    rows: list[DraftRowOut]


class DraftStartOut(BaseModel):
    draft_id: str
    season: int
    running: bool
    started_at: datetime | None
    already_running: bool
    my_slot: int | None
    picks_made: int
    board_rows: int


def _weights_payload(repo: WeightRepository, horizon: str) -> dict[str, Any]:
    cfg = repo.config()
    latest = repo.latest_version()
    fitted = repo.fitted(cfg.weights_version)
    overrides = repo.overrides()
    return {
        "horizon": horizon,
        "config": {
            "use_overrides": cfg.use_overrides,
            "weights_version": cfg.weights_version,
            "latest_version": latest,
            "updated_at": cfg.updated_at,
        },
        "in_force": {
            pos: {"weights": r.weights, "source": r.source, "version": r.version}
            for pos, r in repo.resolve_all(horizon).items()
        },
        "fitted": {pos: w for (h, pos), w in fitted.items() if h == horizon},
        "overrides": {pos: w for (h, pos), w in overrides.items() if h == horizon},
    }


def create_app(settings: Settings | None = None, *, draft_host=None) -> FastAPI:  # noqa: ANN001
    """``draft_host`` (a ``DraftHost``) is injectable so tests can serve the replay fixture."""
    settings = settings or get_settings()
    engine = make_engine(settings)
    sessions = make_session_factory(engine)
    store = store_from_settings(settings)

    def get_session() -> Iterator[Session]:
        s = sessions()
        try:
            yield s
        finally:
            s.close()

    http = HttpClient(
        timeout_s=settings.http_timeout_s,
        retries=settings.http_retries,
        delay_ms=settings.http_delay_ms,
    )
    # the draft poll's client (LS-65): fail fast, no courtesy pause, the poller retries
    draft_http = HttpClient(timeout_s=settings.draft_http_timeout_s, retries=0, delay_ms=0)

    def provider(session: Session, name: str):  # noqa: ANN202
        from lazy_sleeper.scoring import default_scorer, load_league_rules

        return make_provider(session, default_scorer(load_league_rules(session, store)), name)

    def puller(session: Session):  # noqa: ANN202
        from lazy_sleeper.ingest.espn import EspnClient
        from lazy_sleeper.ingest.nflverse import NflverseClient
        from lazy_sleeper.ingest.pipeline import Puller

        return Puller(
            session=session,
            store=store,
            sleeper=SleeperClient(http),
            espn=EspnClient(http),
            nflverse=NflverseClient(http),
        )

    host = draft_host
    if host is None:
        from lazy_sleeper.draft.host import DbDraftFactory

        host = DbDraftFactory(
            sessions, store, SleeperClient(draft_http), provider, puller, settings,
            max_backoff_s=settings.draft_max_backoff_s,
        ).host()  # fmt: skip

    app = FastAPI(title="Lazy Sleeper API", version="0.1.1")
    app.state.draft_host = host

    @app.exception_handler(OperationalError)
    def _db_unavailable(_req: Request, exc: OperationalError) -> JSONResponse:
        """A connect/statement timeout or a refused connection (LS-69): say so in seconds with a
        503 instead of letting the request hang. The engine's timeouts bound how long this takes
        (`DB_CONNECT_TIMEOUT_S`, `DB_STATEMENT_TIMEOUT_MS`)."""
        reason = " ".join(str(exc.orig or exc).split())
        return JSONResponse({"detail": f"database unavailable: {reason}"}, status_code=503)

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness: `{"status": "ok"}`."""
        return {"status": "ok"}

    @app.get("/snapshots")
    def snapshots(
        limit: int = 50,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> list[dict[str, Any]]:
        """Most recent raw snapshots (id, source, kind, season, week, pulled_at, record_count,
        valid, byte_size) — the data-freshness view."""
        rows = session.scalars(
            select(Snapshot).order_by(Snapshot.pulled_at.desc()).limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "source": r.source,
                "kind": r.kind,
                "season": r.season,
                "week": r.week,
                "pulled_at": r.pulled_at,
                "record_count": r.record_count,
                "valid": r.valid,
                "byte_size": r.byte_size,
            }
            for r in rows
        ]

    @app.get("/ensemble/weights")
    def ensemble_weights(
        horizon: str = SEASON,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> dict[str, Any]:
        """Weights in force per position, plus the fitted and override rows and the config flags."""
        if horizon not in (SEASON, WEEKLY):
            raise HTTPException(422, f"horizon must be {SEASON!r} or {WEEKLY!r}")
        return _weights_payload(WeightRepository(session), horizon)

    @app.put("/ensemble/overrides")
    def put_override(
        body: OverrideBody,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> dict[str, Any]:
        """Set the manual (λ) override for one position. Does not flip use_overrides by itself."""
        repo = WeightRepository(session)
        try:
            repo.set_override(body.horizon, body.position.upper(), body.weights, body.note)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        session.commit()
        return _weights_payload(repo, body.horizon)

    @app.delete("/ensemble/overrides")
    def delete_override(
        horizon: str = SEASON,
        position: str | None = None,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> dict[str, Any]:
        """Remove the manual override for one position, or all positions when omitted."""
        repo = WeightRepository(session)
        removed = repo.clear_override(horizon, position.upper() if position else None)
        session.commit()
        return {"removed": removed, **_weights_payload(repo, horizon)}

    @app.put("/ensemble/config")
    def put_config(
        body: ConfigBody,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> dict[str, Any]:
        """Flip use_overrides and/or pin a fitted version (`latest: true` clears the pin)."""
        repo = WeightRepository(session)
        pin: int | None | str = "keep"
        if body.latest:
            pin = None
        elif body.weights_version is not None:
            if body.weights_version > (repo.latest_version() or 0):
                raise HTTPException(422, f"no fitted version {body.weights_version}")
            pin = body.weights_version
        repo.set_config(use_overrides=body.use_overrides, weights_version=pin)
        session.commit()
        return _weights_payload(repo, SEASON)

    @app.get("/board/config")
    def board_config(
        session: Session = Depends(get_session),  # noqa: B008
    ) -> dict[str, Any]:
        """Tier/cliff/flag thresholds in force (defaults seeded by migrations 0006/0007)."""
        from lazy_sleeper.board import BoardConfigRepository

        return BoardConfigRepository(session).as_dict()

    @app.put("/board/config")
    def put_board_config(
        body: BoardConfigBody,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> dict[str, Any]:
        """Adjust any subset of the tier/cliff/flag thresholds (draft-day dial)."""
        from lazy_sleeper.board import BoardConfigRepository

        repo = BoardConfigRepository(session)
        repo.set(**body.model_dump())
        session.commit()
        return repo.as_dict()

    def _latest_board(session: Session, season: int, provider: str):  # noqa: ANN202
        from lazy_sleeper.board import BoardRepository

        repo = BoardRepository(session)
        board = repo.latest(season, provider)
        if board is None:
            raise HTTPException(404, f"no board for {season} yet — run `lazy board regen`")
        return repo, board

    @app.get("/board")
    def board(
        season: int = DEFAULT_SEASON,
        provider: str = Query("ensemble", pattern="^(sleeper|espn|ensemble)$"),
        position: str | None = Query(None, min_length=1, max_length=8),
        limit: int | None = Query(None, ge=1, le=1000),
        session: Session = Depends(get_session),  # noqa: B008
    ) -> dict[str, Any]:
        """The latest persisted board: ranked rows with points, VORP, tier/cliff, ADP delta,
        disagreement spread/flag and injury status. `position` filters; `rank` stays overall."""
        from lazy_sleeper.board import board_meta

        repo, b = _latest_board(session, season, provider)
        return {"board": board_meta(b), "rows": repo.rows(b.id, position, limit)}

    @app.get("/board.html", response_class=HTMLResponse)
    def board_html(
        season: int = DEFAULT_SEASON,
        provider: str = Query("ensemble", pattern="^(sleeper|espn|ensemble)$"),
        session: Session = Depends(get_session),  # noqa: B008
    ) -> str:
        """Self-contained HTML view of the latest board — the draft-night fallback."""
        from lazy_sleeper.board import board_meta, to_html

        repo, b = _latest_board(session, season, provider)
        return to_html(board_meta(b), repo.rows(b.id))

    @app.post("/board/regen")
    def board_regen(
        body: RegenBody,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> dict[str, Any]:
        """Rebuild the board from core.* under the current config and persist it — on-demand
        regen, so a `PUT /board/config` change shows up without waiting for the daily job."""
        from lazy_sleeper.board import board_meta, regenerate
        from lazy_sleeper.scoring import default_scorer, load_league_rules

        rules = load_league_rules(session, store)
        scorer = default_scorer(rules)
        b, rows = regenerate(
            session,
            make_provider(session, scorer, body.provider),
            rules,
            scorer,
            body.season,
            baseline=body.baseline,
        )
        session.commit()
        return {"board": board_meta(b), "rows": len(rows)}

    # --- LS-35: live draft decision surface -------------------------------------------------

    def _not_running(draft_id: str) -> HTTPException:
        return HTTPException(404, f"draft {draft_id} is not running; POST /draft/{draft_id}/start")

    @app.post("/draft/{draft_id}/start", response_model=DraftStartOut)
    def draft_start(draft_id: str, body: DraftStartBody | None = None) -> dict[str, Any]:
        """Pre-draft load (board + ADP + player lookup, once) and start polling Sleeper for this
        draft on a background thread. Idempotent while the runner is alive. Do this *before* the
        draft room opens — the board build takes seconds, the recompute per pick ~50 ms."""
        body = body or DraftStartBody()
        before = host.get(draft_id)
        already = before is not None and before.runner.running
        run = host.start(
            draft_id, body.season, until_complete=not body.forever, interval_s=body.interval_s
        )
        return {
            "draft_id": draft_id,
            "season": run.season,
            "running": run.runner.running,
            "started_at": run.started_at,
            "already_running": already,
            "my_slot": run.engine.state.my_slot,
            "picks_made": run.engine.state.picks_made,
            "board_rows": len(run.engine.board.rows),
        }

    @app.post("/draft/{draft_id}/stop")
    def draft_stop(draft_id: str) -> dict[str, Any]:
        """Stop polling this draft (`{draft_id, running}`); 404 if it isn't running."""
        run = host.stop(draft_id)
        if run is None:
            raise _not_running(draft_id)
        return {"draft_id": draft_id, "running": run.runner.running}

    @app.get("/draft/{draft_id}/state", response_model=DraftStateOut)
    def draft_state(
        draft_id: str,
        position: str | None = Query(None, min_length=1, max_length=8),
        limit: int | None = Query(None, ge=1, le=1000),
    ) -> dict[str, Any]:
        """The decision surface from the latest recompute: clock, my roster + needs, available
        players by pick_score with tier/cliff/run/survival. `recompute.stale`/`error` flag a failed
        recompute (rows are then the previous good ones). 404 until `POST /draft/{id}/start`."""
        payload = host.state(draft_id, position=position, limit=limit)
        if payload is None:
            raise _not_running(draft_id)
        return payload

    @app.get("/draft/{draft_id}/state.html", response_class=HTMLResponse)
    def draft_state_html(
        draft_id: str,
        season: int = DEFAULT_SEASON,
        limit: int = Query(40, ge=1, le=1000),
        refresh: float = Query(2.0, ge=0.5, le=60.0),
        interval: float = Query(2.0, ge=0.5, le=60.0),
    ) -> str:
        """The draft-night fallback (LS-37): a self-contained page that polls
        `/draft/{id}/state` every `refresh` seconds and offers the start button when the runner
        isn't up. Phone- and second-monitor-readable; no build step."""
        from lazy_sleeper.draft.render import draft_page

        return draft_page(
            draft_id, season=season, limit=limit, refresh_s=refresh, interval_s=interval
        )

    @app.get("/board/config.html", response_class=HTMLResponse)
    def board_config_html(draft_id: str | None = None) -> str:
        """Tuning page: every `board_config` dial as a form → `PUT /board/config`, then apply to
        the running draft (`POST /draft/{id}/config`) — signal dials instantly, board-time dials
        via a restart. Linked from the draft page so no CLI is needed on draft night."""
        from lazy_sleeper.draft.render import config_page

        return config_page(draft_id or settings.sleeper_draft_id)

    @app.post("/draft/{draft_id}/config")
    def draft_apply_config(
        draft_id: str,
        restart: bool = Query(
            False, description="Rebuild the board (tiers/cliffs/flags/stream_depth)"
        ),
        session: Session = Depends(get_session),  # noqa: B008
    ) -> dict[str, Any]:
        """Push the stored `board_config` into the running draft. Without `restart` only the
        draft-time dials (survival, runs, need bonus, late_rounds) change — instant recompute.
        With it the runner is stopped and started again (board rebuild, a few seconds)."""
        from lazy_sleeper.board import BoardConfigRepository

        run = host.get(draft_id)
        if run is None:
            raise _not_running(draft_id)
        if restart:
            run = host.restart(draft_id)
            assert run is not None  # noqa: S101 — get() just returned it
            return {"draft_id": draft_id, "restarted": True, "running": run.runner.running,
                    "recompute_seq": run.engine.latest.seq}  # fmt: skip
        cfg = BoardConfigRepository(session).get()
        adv = run.engine.set_config(cfg)
        return {"draft_id": draft_id, "restarted": False, "running": run.runner.running,
                "recompute_seq": adv.seq, "error": adv.error}  # fmt: skip

    @app.get("/draft.html", response_class=HTMLResponse)
    def draft_html_default(
        season: int = DEFAULT_SEASON,
        limit: int = Query(40, ge=1, le=1000),
        refresh: float = Query(2.0, ge=0.5, le=60.0),
        interval: float = Query(2.0, ge=0.5, le=60.0),
    ) -> str:
        """`/draft/{id}/state.html` for the configured `sleeper_draft_id` — the bookmark."""
        from lazy_sleeper.draft.render import draft_page

        return draft_page(
            settings.sleeper_draft_id, season=season, limit=limit, refresh_s=refresh,
            interval_s=interval,
        )  # fmt: skip

    @app.get("/draft")
    def drafts() -> list[dict[str, Any]]:
        """Drafts known to this API process: `[{draft_id, running, season}]`."""
        return [
            {"draft_id": did, "running": r.runner.running, "season": r.season}
            for did in host.ids()
            if (r := host.get(did)) is not None
        ]

    return app


app = create_app()
