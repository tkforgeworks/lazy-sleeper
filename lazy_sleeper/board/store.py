"""Persisted draft boards — `derived.boards` / `derived.board_rows` (LS-30).

A board is a dated, immutable run of the pipeline (vorp → tiers → flags) flattened to one row
per player with identity and injury status attached. `/board` serves the latest persisted board
rather than recomputing per request, so what the app shows is stable between regens and a
half-loaded daily pull can't leak into the draft-night view. ``regenerate`` is the single write
path (CLI `lazy board regen`, `POST /board/regen`, and the daily workflow all call it).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from lazy_sleeper.board.baselines import RosterShape, historical_baselines
from lazy_sleeper.board.config import BoardConfigRepository
from lazy_sleeper.board.flags import build_board, latest_adp
from lazy_sleeper.board.tiers import BoardRow, TierConfig
from lazy_sleeper.db.models import Board, BoardEntry, Player
from lazy_sleeper.ingest.byes import bye_of, byes_for
from lazy_sleeper.providers.base import ProjectionProvider
from lazy_sleeper.scoring.engine import Scorer
from lazy_sleeper.scoring.rules import ScoringRules

ROW_FIELDS = (
    "rank",
    "sleeper_id",
    "name",
    "position",
    "team",
    "injury_status",
    "bye",
    "points",
    "baseline",
    "vorp",
    "pos_rank",
    "tier",
    "cliff",
    "gap_to_next",
    "adp",
    "adp_delta",
    "adp_flag",
    "spread",
    "disagree",
    "components",
)


def flatten(
    rows: list[BoardRow],
    players: dict[str, tuple[str | None, str | None]],
    byes: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """BoardRow list (board order) → plain dicts with ``rank``, ``name``, ``injury_status`` and
    ``bye``.

    ``players`` maps sleeper_id → (full_name, injury_status); unknown ids keep the id as name.
    ``byes`` maps team → bye week (LS-57); a player without a team or a schedule gets ``None``.
    """
    out: list[dict[str, Any]] = []
    for rank, r in enumerate(rows, start=1):
        v = r.value
        name, injury = players.get(v.sleeper_id, (None, None))
        out.append(
            {
                "rank": rank,
                "sleeper_id": v.sleeper_id,
                "name": name or v.sleeper_id,
                "position": v.position,
                "team": v.team,
                "injury_status": injury,
                "bye": bye_of(byes, v.team),
                "points": v.points,
                "baseline": v.baseline,
                "vorp": v.vorp,
                "pos_rank": v.pos_rank,
                "tier": r.tier,
                "cliff": r.cliff,
                "gap_to_next": r.gap_to_next,
                "adp": r.adp,
                "adp_delta": r.adp_delta,
                "adp_flag": r.adp_flag,
                "spread": r.spread,
                "disagree": r.disagree,
                "components": dict(v.components),
            }
        )
    return out


def config_dict(config: TierConfig) -> dict[str, Any]:
    d = asdict(config)
    d["depth"] = dict(config.depth)
    return d


class BoardRepository:
    """Read/write persisted boards."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def save(
        self,
        *,
        season: int,
        provider: str,
        baseline: str,
        config: TierConfig,
        rows: list[dict[str, Any]],
        generated_at: datetime | None = None,
    ) -> Board:
        board = Board(
            season=season,
            provider=provider,
            baseline=baseline,
            generated_at=generated_at or datetime.now(UTC),
            config=config_dict(config),
            row_count=len(rows),
        )
        self._s.add(board)
        self._s.flush()
        self._s.add_all(
            BoardEntry(board_id=board.id, **{k: row[k] for k in ROW_FIELDS}) for row in rows
        )
        self._s.flush()
        return board

    def latest(self, season: int, provider: str | None = None) -> Board | None:
        stmt = select(Board).where(Board.season == season)
        if provider:
            stmt = stmt.where(Board.provider == provider)
        return self._s.scalars(stmt.order_by(Board.generated_at.desc(), Board.id.desc())).first()

    def rows(
        self, board_id: int, position: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(BoardEntry).where(BoardEntry.board_id == board_id)
        if position:
            stmt = stmt.where(BoardEntry.position == position.upper())
        stmt = stmt.order_by(BoardEntry.rank)
        if limit:
            stmt = stmt.limit(limit)
        return [{k: getattr(e, k) for k in ROW_FIELDS} for e in self._s.scalars(stmt)]

    def player_info(self, sleeper_ids: set[str]) -> dict[str, tuple[str | None, str | None]]:
        if not sleeper_ids:
            return {}
        rows = self._s.execute(
            select(Player.sleeper_id, Player.full_name, Player.injury_status).where(
                Player.sleeper_id.in_(sleeper_ids)
            )
        )
        return {sid: (name, injury) for sid, name, injury in rows}


def board_meta(board: Board) -> dict[str, Any]:
    return {
        "id": board.id,
        "season": board.season,
        "provider": board.provider,
        "baseline": board.baseline,
        "generated_at": board.generated_at,
        "row_count": board.row_count,
        "config": board.config,
    }


def regenerate(
    session: Session,
    provider: ProjectionProvider,
    rules: ScoringRules,
    scorer: Scorer,
    season: int,
    *,
    baseline: str = "live",
    config: TierConfig | None = None,
) -> tuple[Board, list[dict[str, Any]]]:
    """Build the board from what is in ``core.*`` right now and persist it as a new dated board.

    ``baseline``: ``live`` (default; derived from the same projections) or ``historical``
    (2023–25 actuals average). ``config`` defaults to the stored ``derived.board_config`` row.
    Caller commits.
    """
    if baseline not in ("live", "historical"):
        raise ValueError("baseline must be live | historical")
    repo = BoardRepository(session)
    config = config or BoardConfigRepository(session).get()
    shape = RosterShape.from_rules(rules)
    baselines = None
    if baseline == "historical":
        baselines = historical_baselines(session, scorer, shape).average
    board_rows = build_board(
        provider, shape, season, config, latest_adp(session, season), baselines=baselines
    )
    players = repo.player_info({r.value.sleeper_id for r in board_rows})
    rows = flatten(board_rows, players, byes_for(session, season))
    board = repo.save(
        season=season, provider=provider.name, baseline=baseline, config=config, rows=rows
    )
    return board, rows
