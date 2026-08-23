"""LS-52: duplicate-content pulls are deduped; freshness still counts them. DB-free."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from lazy_sleeper.ingest.audit import freshness
from lazy_sleeper.ingest.pipeline import Puller
from lazy_sleeper.ingest.snapshots import SnapshotKey, SnapshotStore
from lazy_sleeper.ingest.stat_loaders import duplicate_scope_ids
from lazy_sleeper.ingest.validate import validate_json_any

T0 = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


class FakeRepo:
    """SnapshotRepository stand-in: rows live in a list; latest() = newest matching key."""

    def __init__(self) -> None:
        self.rows: list[SimpleNamespace] = []
        self._id = 0

    def add(self, rec: Any) -> SimpleNamespace:
        self._id += 1
        row = SimpleNamespace(
            id=self._id, source=rec.key.source, kind=rec.key.kind, season=rec.key.season,
            week=rec.key.week, pulled_at=rec.pulled_at, last_seen_at=None, sha256=rec.sha256,
            storage_path=rec.storage_path, valid=rec.valid,
        )  # fmt: skip
        self.rows.append(row)
        return row

    def latest(self, key: SnapshotKey, *, valid_only: bool = True) -> SimpleNamespace | None:
        match = [
            r for r in self.rows
            if (r.source, r.kind, r.season, r.week) == (key.source, key.kind, key.season, key.week)
        ]  # fmt: skip
        return max(match, key=lambda r: r.pulled_at) if match else None


def _puller(tmp_path: Path) -> tuple[Puller, FakeRepo, SnapshotStore]:
    store = SnapshotStore(tmp_path)
    p = Puller(session=None, store=store, sleeper=None, espn=None, nflverse=None)  # type: ignore[arg-type]
    repo = FakeRepo()
    p._repo = repo  # noqa: SLF001 — behavior test through the public snapshot()
    return p, repo, store


def test_identical_payload_is_deduped_no_new_file_or_row(tmp_path: Path) -> None:
    p, repo, store = _puller(tmp_path)
    key = SnapshotKey("sleeper", "adp", 2026)
    first = p.snapshot(key, b'[{"a":1}]', validate_json_any, pulled_at=T0)
    again = p.snapshot(key, b'[{"a":1}]', validate_json_any, pulled_at=T0 + timedelta(days=1))
    assert again is first and len(repo.rows) == 1
    assert again.last_seen_at == T0 + timedelta(days=1)
    files = list(tmp_path.rglob("*.json.gz"))
    assert len(files) == 1  # no second file, hence no second Storage object


def test_changed_payload_still_stored_exactly_as_before(tmp_path: Path) -> None:
    p, repo, _ = _puller(tmp_path)
    key = SnapshotKey("sleeper", "adp", 2026)
    p.snapshot(key, b'[{"a":1}]', validate_json_any, pulled_at=T0)
    second = p.snapshot(key, b'[{"a":2}]', validate_json_any, pulled_at=T0 + timedelta(days=1))
    assert len(repo.rows) == 2 and second.last_seen_at is None
    assert len(list(Path(p._store._root).rglob("*.json.gz"))) == 2  # noqa: SLF001


def test_same_bytes_different_scope_is_not_deduped(tmp_path: Path) -> None:
    p, repo, _ = _puller(tmp_path)
    p.snapshot(SnapshotKey("sleeper", "adp", 2026), b"[]", validate_json_any, pulled_at=T0)
    p.snapshot(
        SnapshotKey("sleeper", "adp", 2025), b"[]", validate_json_any,
        pulled_at=T0 + timedelta(minutes=1),
    )  # fmt: skip
    assert len(repo.rows) == 2


def _snap(i: int, sha: str, *, kind: str = "kona", week: int | None = None, at: datetime = T0):
    return SimpleNamespace(
        id=i, source="espn", kind=kind, season=2026, week=week, sha256=sha, pulled_at=at
    )


def test_duplicate_scope_ids_skips_content_dupes_of_loaded_and_batch() -> None:
    loaded = _snap(1, "aaa")
    dup_of_loaded = _snap(2, "aaa", at=T0 + timedelta(days=1))
    fresh = _snap(3, "bbb", at=T0 + timedelta(days=2))
    dup_in_batch = _snap(4, "bbb", at=T0 + timedelta(days=3))
    other_scope = _snap(5, "aaa", week=1)  # same bytes, different (kind/week) scope → load it
    dupes = duplicate_scope_ids(
        [loaded, dup_of_loaded, fresh, dup_in_batch, other_scope], loaded_ids={1}
    )
    assert dupes == {2, 4}


class StubSession:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def scalars(self, _stmt: Any) -> list[SimpleNamespace]:
        return sorted(self._rows, key=lambda r: r.pulled_at, reverse=True)


def test_freshness_counts_a_deduped_pull_as_fresh() -> None:
    old = SimpleNamespace(
        source="nflverse", kind="crosswalk", season=None, week=None, pulled_at=T0,
        last_seen_at=T0 + timedelta(days=6), valid=True, record_count=100,
    )  # fmt: skip
    rows = freshness(StubSession([old]), now=T0 + timedelta(days=6, hours=1))  # type: ignore[arg-type]
    assert len(rows) == 1 and rows[0].age_hours == 1.0 and rows[0].latest == old.last_seen_at


def test_freshness_prefers_the_most_recently_seen_row_per_feed() -> None:
    a = SimpleNamespace(
        source="sleeper", kind="adp", season=2026, week=None, pulled_at=T0,
        last_seen_at=T0 + timedelta(days=3), valid=True, record_count=10,
    )  # fmt: skip
    b = SimpleNamespace(
        source="sleeper", kind="adp", season=2026, week=None, pulled_at=T0 + timedelta(days=1),
        last_seen_at=None, valid=True, record_count=11,
    )  # fmt: skip
    rows = freshness(StubSession([a, b]), now=T0 + timedelta(days=3))  # type: ignore[arg-type]
    assert len(rows) == 1 and rows[0].latest == a.last_seen_at  # the dedup stamp wins
