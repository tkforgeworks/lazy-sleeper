"""Schema validation for undocumented feeds.

Deliberately shallow: confirm the payload has the shape we parse downstream and a sane
record count. A failed validation still gets snapshotted (valid=False) so we keep the evidence,
but loaders skip it and fall back to the last valid snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import orjson


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    record_count: int | None
    notes: str | None = None


def _fail(msg: str, count: int | None = None) -> ValidationResult:
    return ValidationResult(False, count, msg)


def parse_json(payload: bytes) -> Any:
    return orjson.loads(payload)


def validate_sleeper_projections(payload: bytes, *, min_records: int = 500) -> ValidationResult:
    """List of {player_id, season, week, stats{...}, player{...}}; season file has week=None."""
    try:
        data = parse_json(payload)
    except orjson.JSONDecodeError as exc:
        return _fail(f"invalid json: {exc}")
    if not isinstance(data, list):
        return _fail("expected list")
    n = len(data)
    if n < min_records:
        return _fail(f"too few records: {n} < {min_records}", n)
    sample = data[0]
    for k in ("player_id", "season", "stats"):
        if k not in sample:
            return _fail(f"missing key '{k}' in first record", n)
    if not isinstance(sample["stats"], dict):
        return _fail("stats is not a dict", n)
    return ValidationResult(True, n)


def validate_sleeper_players(payload: bytes, *, min_records: int = 3000) -> ValidationResult:
    """Dict keyed by player_id → {position, team, full_name, search_rank, ...}."""
    try:
        data = parse_json(payload)
    except orjson.JSONDecodeError as exc:
        return _fail(f"invalid json: {exc}")
    if not isinstance(data, dict):
        return _fail("expected dict keyed by player_id")
    n = len(data)
    if n < min_records:
        return _fail(f"too few players: {n} < {min_records}", n)
    first = next(iter(data.values()))
    if not isinstance(first, dict) or "position" not in first:
        return _fail("player record missing 'position'", n)
    return ValidationResult(True, n)


def validate_espn_kona(payload: bytes, *, min_records: int = 500) -> ValidationResult:
    """Shape: {players: [{id, player{id, fullName, defaultPositionId,
    stats[{statSourceId, statSplitTypeId, stats{}}]}}]}"""
    try:
        data = parse_json(payload)
    except orjson.JSONDecodeError as exc:
        return _fail(f"invalid json: {exc}")
    if not isinstance(data, dict) or "players" not in data:
        return _fail("missing 'players'")
    players = data["players"]
    n = len(players)
    if n < min_records:
        return _fail(f"too few players: {n} < {min_records}", n)
    p = players[0].get("player", {})
    for k in ("id", "fullName", "defaultPositionId", "stats"):
        if k not in p:
            return _fail(f"missing player.{k}", n)
    if p["stats"] and not {"statSourceId", "statSplitTypeId", "stats"} <= set(p["stats"][0]):
        return _fail("stats entry missing statSourceId/statSplitTypeId/stats", n)
    return ValidationResult(True, n)


def validate_espn_pro_teams(payload: bytes, *, min_teams: int = 32) -> ValidationResult:
    """ESPN season doc with ``settings.proTeams[]`` (the bye-week source, LS-57)."""
    try:
        data = parse_json(payload)
    except ValueError as e:
        return _fail(f"invalid JSON: {e}")
    teams = data.get("settings", {}).get("proTeams") if isinstance(data, dict) else None
    if not isinstance(teams, list):
        return _fail("missing settings.proTeams")
    with_bye = [t for t in teams if isinstance(t, dict) and t.get("byeWeek")]
    if len(with_bye) < min_teams:
        return _fail(f"only {len(with_bye)} teams carry a byeWeek (< {min_teams})", len(teams))
    return ValidationResult(True, len(teams), "ok")


def validate_json_any(payload: bytes) -> ValidationResult:
    """For documented Sleeper endpoints (league, rosters, draft picks): just parseable JSON."""
    try:
        data = parse_json(payload)
    except orjson.JSONDecodeError as exc:
        return _fail(f"invalid json: {exc}")
    n = len(data) if isinstance(data, list | dict) else None
    return ValidationResult(True, n)


def validate_csv(
    payload: bytes, *, required_columns: tuple[str, ...] = (), min_rows: int = 1
) -> ValidationResult:
    text = payload[:65536].decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return _fail("empty csv")
    header = [h.strip().strip('"') for h in lines[0].split(",")]
    missing = [c for c in required_columns if c not in header]
    if missing:
        return _fail(f"missing columns: {missing}")
    rows = payload.count(b"\n")
    if rows < min_rows:
        return _fail(f"too few rows: {rows}", rows)
    return ValidationResult(True, rows)
