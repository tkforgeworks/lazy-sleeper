from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from lazy_sleeper.ingest.snapshots import MirrorError, SnapshotKey, SnapshotStore, SupabaseStorage


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


class MemoryRemote:
    """Fake mirror with the full RemoteStorage surface (upload / exists / download)."""

    bucket = "bucket"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload(self, path: str, data: bytes, content_type: str) -> str:
        self.objects[path] = data
        return f"bucket/{path}"

    def exists(self, path: str) -> bool:
        return path in self.objects

    def download(self, path: str) -> bytes:
        if path not in self.objects:  # the real client's contract for a missing object
            raise FileNotFoundError(f"bucket/{path} is not in remote storage")
        return self.objects[path]


def test_read_falls_back_to_remote_and_caches_locally(tmp_path: Path) -> None:
    remote = MemoryRemote()
    store = SnapshotStore(tmp_path, remote)
    rec = store.write(SnapshotKey("sleeper", "league"), b'{"name":"The League"}')
    (tmp_path / rec.storage_path).unlink()  # simulate a fresh machine / CI runner

    assert store.fetch(rec.storage_path) is True
    assert (tmp_path / rec.storage_path).exists()
    assert store.fetch(rec.storage_path) is False  # already local now
    assert store.read(rec.storage_path) == b'{"name":"The League"}'


def test_read_missing_everywhere_raises(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path, MemoryRemote())
    with pytest.raises(FileNotFoundError):
        store.read("sleeper/league/na/na/20260101T000000Z.json.gz")
    with pytest.raises(FileNotFoundError):
        SnapshotStore(tmp_path).read("sleeper/league/na/na/20260101T000000Z.json.gz")


def test_require_mirror_discards_the_snapshot_when_upload_fails(tmp_path: Path) -> None:
    """On an ephemeral runner a row with no file behind it poisons every later load."""
    store = SnapshotStore(tmp_path, ExplodingRemote(), require_mirror=True)
    with pytest.raises(MirrorError, match="supabase down"):
        store.write(SnapshotKey("espn", "kona", 2026), b'{"players":[]}')
    assert not list(tmp_path.rglob("*.gz"))


def test_require_mirror_is_a_no_op_when_the_upload_succeeds(tmp_path: Path) -> None:
    remote = FakeRemote()
    store = SnapshotStore(tmp_path, remote, require_mirror=True)
    rec = store.write(SnapshotKey("espn", "kona", 2026), b'{"players":[]}')
    assert rec.remote_path == f"bucket/{rec.storage_path}" and remote.uploads


def _storage(handler) -> SupabaseStorage:  # noqa: ANN001
    s = SupabaseStorage("https://x.supabase.co", "sb_secret", "raw-snapshots")
    s._client = httpx.Client(transport=httpx.MockTransport(handler))  # noqa: SLF001
    return s


def test_supabase_download_maps_not_found_400_to_file_not_found() -> None:
    """Supabase answers a missing object with 400 {"error":"not_found"} — the 2026-09-04 daily
    pull died on exactly this after two days of failed uploads."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"statusCode": "404", "error": "not_found", "message": "Object not found"}
        )

    with pytest.raises(FileNotFoundError, match="raw-snapshots/espn/kona"):
        _storage(handler).download("espn/kona/2026/na/20260902T135815Z.json.gz")


def test_supabase_download_other_errors_still_raise_http_status() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(544, text="")

    with pytest.raises(httpx.HTTPStatusError):
        _storage(handler).download("espn/kona/2026/na/x.json.gz")
