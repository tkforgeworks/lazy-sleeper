"""`lazy sync` push/pull over an in-memory mirror and a stub session (LS-12)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from lazy_sleeper.db.models import Snapshot
from lazy_sleeper.ingest.snapshots import SnapshotKey, SnapshotStore
from lazy_sleeper.ingest.sync import Syncer
from tests.test_snapshots import MemoryRemote


class StubSession:
    """Just enough of a Session for Syncer: scalars(select) → the rows we hold."""

    def __init__(self, rows: list[Snapshot]) -> None:
        self.rows = rows
        self.flushed = 0

    def scalars(self, _stmt):  # noqa: ANN001, ANN201
        return list(self.rows)

    def flush(self) -> None:
        self.flushed += 1


def _row(store: SnapshotStore, key: SnapshotKey, payload: bytes, *, remote: bool) -> Snapshot:
    rec = store.write(key, payload, pulled_at=datetime.now(UTC))
    return Snapshot(
        source=key.source,
        kind=key.kind,
        season=key.season,
        week=key.week,
        pulled_at=rec.pulled_at,
        sha256=rec.sha256,
        byte_size=rec.byte_size,
        storage_path=rec.storage_path,
        remote_path=rec.remote_path if remote else None,
        valid=True,
    )


def test_push_uploads_missing_sets_remote_path_and_is_idempotent(tmp_path: Path) -> None:
    remote = MemoryRemote()
    plain = SnapshotStore(tmp_path)  # writes without mirroring → "backfilled" files
    a = _row(plain, SnapshotKey("sleeper", "players"), b"{}", remote=False)
    b = _row(plain, SnapshotKey("espn", "kona", 2026), b"{}", remote=False)
    remote.objects[b.storage_path] = b"already there"  # exists remotely, DB doesn't know
    session = StubSession([a, b])
    store = SnapshotStore(tmp_path, remote)

    rep = Syncer(session, store).push()
    assert (rep.uploaded, rep.already_remote, rep.skipped, rep.failed) == (1, 1, 0, [])
    assert a.remote_path == f"bucket/{a.storage_path}"
    assert b.remote_path == f"bucket/{b.storage_path}"
    assert remote.objects[a.storage_path] == (tmp_path / a.storage_path).read_bytes()

    rep2 = Syncer(session, store).push()
    assert (rep2.uploaded, rep2.skipped) == (0, 2)


def test_push_reports_missing_local_and_dry_run(tmp_path: Path) -> None:
    remote = MemoryRemote()
    store = SnapshotStore(tmp_path)
    a = _row(store, SnapshotKey("sleeper", "players"), b"{}", remote=False)
    (tmp_path / a.storage_path).unlink()
    b = _row(store, SnapshotKey("sleeper", "league"), b"{}", remote=False)
    rep = Syncer(StubSession([a, b]), SnapshotStore(tmp_path, remote)).push(dry_run=True)
    assert rep.missing_local == [a.storage_path]
    assert rep.uploaded == 1 and remote.objects == {}  # dry run uploads nothing
    assert b.remote_path is None


def test_push_treats_409_as_present(tmp_path: Path) -> None:
    class Conflicting(MemoryRemote):
        def upload(self, path: str, data: bytes, content_type: str) -> str:
            req = httpx.Request("POST", "https://x/" + path)
            raise httpx.HTTPStatusError(
                "dup", request=req, response=httpx.Response(409, request=req)
            )

    store = SnapshotStore(tmp_path)
    a = _row(store, SnapshotKey("sleeper", "players"), b"{}", remote=False)
    rep = Syncer(StubSession([a]), SnapshotStore(tmp_path, Conflicting())).push()
    assert rep.already_remote == 1 and not rep.failed
    assert a.remote_path == f"bucket/{a.storage_path}"


def test_pull_downloads_only_missing(tmp_path: Path) -> None:
    remote = MemoryRemote()
    store = SnapshotStore(tmp_path, remote)
    a = _row(store, SnapshotKey("sleeper", "players"), b'{"a":1}', remote=True)
    b = _row(store, SnapshotKey("sleeper", "league"), b'{"b":2}', remote=True)
    (tmp_path / a.storage_path).unlink()
    rep = Syncer(StubSession([a, b]), store).pull()
    assert (rep.downloaded, rep.skipped, rep.failed) == (1, 1, [])
    assert store.read(a.storage_path) == b'{"a":1}'


def test_syncer_requires_remote(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        Syncer(StubSession([]), SnapshotStore(tmp_path))
