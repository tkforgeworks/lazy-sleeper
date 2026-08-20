"""Board persistence shape and CSV/HTML rendering (LS-30) — DB-free."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime

from lazy_sleeper.board import (
    TierConfig,
    assign_tiers,
    flag_adp,
    flag_disagreement,
    flatten,
    to_csv,
    to_html,
    vorp_board,
)
from lazy_sleeper.board.store import ROW_FIELDS, config_dict
from lazy_sleeper.db.models import BoardEntry
from lazy_sleeper.providers.base import PlayerProjection


def _rows():
    projections = [
        PlayerProjection(
            sleeper_id="a",
            position="RB",
            team="DET",
            points=300.0,
            source="t",
            components={"sleeper": 310.0, "espn": 290.0},
        ),
        PlayerProjection(
            sleeper_id="b",
            position="WR",
            team="LAR",
            points=250.0,
            source="t",
            components={"sleeper": 300.0, "espn": 200.0},
        ),
        PlayerProjection(sleeper_id="c", position="RB", team=None, points=100.0, source="t"),
    ]
    board = assign_tiers(vorp_board(projections, {"RB": 100.0, "WR": 100.0}), TierConfig())
    board = flag_adp(board, {"a": 1.0, "b": 40.0}, TierConfig())
    board = flag_disagreement(board, TierConfig(debias_disagreement=False))
    return flatten(board, {"a": ("Jahmyr Gibbs", None), "b": ("Puka Nacua", "Questionable")})


def test_flatten_numbers_rows_and_attaches_identity() -> None:
    rows = _rows()
    assert [r["rank"] for r in rows] == [1, 2, 3]
    assert rows[0]["name"] == "Jahmyr Gibbs" and rows[0]["injury_status"] is None
    assert rows[1]["name"] == "Puka Nacua" and rows[1]["injury_status"] == "Questionable"
    assert rows[2]["name"] == "c"  # unknown player keeps its id
    assert rows[1]["adp_flag"] == "value" and rows[1]["disagree"]
    assert rows[0]["components"] == {"sleeper": 310.0, "espn": 290.0}


def test_flattened_rows_match_the_board_rows_table() -> None:
    # Every persisted column is produced by flatten (and nothing extra) — the save path is **row.
    cols = {c.name for c in BoardEntry.__table__.columns} - {"id", "board_id"}
    assert set(ROW_FIELDS) == cols
    assert set(_rows()[0]) == set(ROW_FIELDS)


def test_config_dict_is_json_ready() -> None:
    d = config_dict(TierConfig(cliff_gap=12.0))
    assert d["cliff_gap"] == 12.0 and d["debias_disagreement"] is True
    assert isinstance(d["depth"], dict) and d["depth"]["RB"] > 0


def test_csv_has_header_and_blank_for_missing() -> None:
    text = to_csv(_rows())
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert len(parsed) == 3
    assert parsed[0]["name"] == "Jahmyr Gibbs" and parsed[0]["adp"] == "1.00"
    assert parsed[2]["adp"] == "" and parsed[2]["spread"] == ""  # no ADP / components
    assert parsed[1]["disagree"] == "1" and parsed[0]["cliff"] in ("0", "1")


def test_html_is_self_contained_and_escapes() -> None:
    rows = _rows()
    rows[0]["name"] = "<script>alert(1)</script>"
    meta = {
        "season": 2026,
        "provider": "ensemble",
        "baseline": "live",
        "generated_at": datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
    }
    html = to_html(meta, rows)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "generated 2026-08-20 10:00 UTC" in html
    assert 'data-pos="RB"' in html and 'data-pos="WR"' in html
    assert "Questionable" in html and ">value<" in html and ">DISAGREE<" in html
    assert "http://" not in html and "https://" not in html  # no external assets
