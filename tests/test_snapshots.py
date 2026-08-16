from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lazy_sleeper.ingest.snapshots import SnapshotKey, SnapshotStore


class FakeRemote:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, int]] = []

    def upload(self, path: str, data: bytes, content_type: str) -> str:
        self.uploads.append((path, len(data)))
        return f"bucket/{path}"


class ExplodingRemote:
    def upload(self, path: str, data: bytes, content_type: str) -> str:
        raise RuntimeError("supabase down")


def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    key = SnapshotKey("sleeper", "projections_week", 2025, 3)
    ts = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    rec = store.write(key, b'[{"a":1}]', pulled_at=ts, record_count=1)

    assert rec.storage_path == "sleeper/projections_week/2025/03/20260816T120000Z.json.gz"
    assert (tmp_path / rec.storage_path).exists()
    assert gzip.decompress((tmp_path / rec.storage_path).read_bytes()) == b'[{"a":1}]'
    assert store.read(rec.storage_path) == b'[{"a":1}]'
    assert rec.byte_size == 9
    assert len(rec.sha256) == 64
    assert rec.remote_path is None


def test_never_overwrites(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    key = SnapshotKey("sleeper", "players")
    ts = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    store.write(key, b"{}", pulled_at=ts)
    with pytest.raises(FileExistsError):
        store.write(key, b"{}", pulled_at=ts)


def test_remote_mirror(tmp_path: Path) -> None:
    remote = FakeRemote()
    store = SnapshotStore(tmp_path, remote)
    rec = store.write(SnapshotKey("espn", "kona", 2026), b'{"players":[]}')
    assert rec.remote_path == f"bucket/{rec.storage_path}"
    assert remote.uploads and remote.uploads[0][0] == rec.storage_path


def test_remote_failure_keeps_local(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path, ExplodingRemote())
    rec = store.write(SnapshotKey("espn", "kona", 2026), b'{"players":[]}')
    assert rec.remote_path is None
    assert (tmp_path / rec.storage_path).exists()


def test_key_path_without_season_week() -> None:
    key = SnapshotKey("nflverse", "crosswalk")
    ts = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert key.relative_path(ts, ext="csv") == "nflverse/crosswalk/na/na/20260102T030405Z.csv.gz"
