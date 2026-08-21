"""FastAPI application. M0: health + snapshot inventory; LS-25: ensemble weights switchboard;
LS-28/29 board config; LS-30 `/board` (latest persisted board), `/board.html`, `POST /board/regen`.
Draft endpoints arrive in M4."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from lazy_sleeper.config import Settings, get_settings
from lazy_sleeper.db.models import Snapshot
from lazy_sleeper.db.session import make_engine, make_session_factory
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


def create_app(settings: Settings | None = None) -> FastAPI:
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

    app = FastAPI(title="Lazy Sleeper API", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/snapshots")
    def snapshots(
        limit: int = 50,
        session: Session = Depends(get_session),  # noqa: B008
    ) -> list[dict[str, Any]]:
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

    return app


app = create_app()
