from __future__ import annotations

from lazy_sleeper.ingest.validate import (
    validate_csv,
    validate_espn_kona,
    validate_sleeper_players,
    validate_sleeper_projections,
)


def test_sleeper_projections_valid(sleeper_proj_payload: bytes) -> None:
    r = validate_sleeper_projections(sleeper_proj_payload, min_records=1)
    assert r.valid, r.notes
    assert r.record_count and r.record_count >= 1


def test_sleeper_projections_rejects_wrong_shape() -> None:
    assert not validate_sleeper_projections(b'{"players": []}', min_records=1).valid
    assert not validate_sleeper_projections(b"[]", min_records=1).valid
    assert not validate_sleeper_projections(b"not json", min_records=1).valid
    assert not validate_sleeper_projections(b'[{"player_id": "1"}]', min_records=1).valid


def test_sleeper_projections_min_records(sleeper_proj_payload: bytes) -> None:
    r = validate_sleeper_projections(sleeper_proj_payload, min_records=10_000)
    assert not r.valid
    assert "too few" in (r.notes or "")


def test_espn_kona_valid(espn_kona_payload: bytes) -> None:
    r = validate_espn_kona(espn_kona_payload, min_records=1)
    assert r.valid, r.notes


def test_espn_kona_rejects_missing_players() -> None:
    assert not validate_espn_kona(b"[]", min_records=1).valid
    assert not validate_espn_kona(b'{"foo": 1}', min_records=1).valid


def test_sleeper_players_valid(sleeper_players_payload: bytes) -> None:
    r = validate_sleeper_players(sleeper_players_payload, min_records=1)
    assert r.valid, r.notes


def test_csv_validation() -> None:
    good = b"sleeper_id,sportradar_id,gsis_id\n1,a,b\n2,c,d\n"
    assert validate_csv(good, required_columns=("sleeper_id", "gsis_id"), min_rows=2).valid
    assert not validate_csv(good, required_columns=("nope",)).valid
    assert not validate_csv(b"", required_columns=()).valid
