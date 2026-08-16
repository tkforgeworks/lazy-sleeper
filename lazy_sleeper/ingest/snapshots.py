"""Immutable dated snapshot store.

Payload bytes → gzip → local archive (always) → Supabase Storage (when configured).
Metadata → raw.snapshots (via SnapshotRepository). Nothing here ever overwrites an existing object.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from sqlalchemy.orm import Session

from lazy_sleeper.db.models import Snapshot

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnapshotKey:
    source: str  # sleeper | espn | nflverse
    kind: str  # players | projections_season | projections_week | kona | stats_player_week | ...
    season: int | None = None
    week: int | None = None

    def relative_path(self, pulled_at: datetime, ext: str = "json") -> str:
        s = str(self.season) if self.season is not None else "na"
        w = f"{self.week:02d}" if self.week is not None else "na"
        stamp = pulled_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{self.source}/{self.kind}/{s}/{w}/{stamp}.{ext}.gz"


@dataclass
class SnapshotRecord:
    key: SnapshotKey
    pulled_at: datetime
    sha256: str
    byte_size: int
    storage_path: str
    remote_path: str | None = None
    record_count: int | None = None
    valid: bool = True
    validation_notes: str | None = None
    schema_version: str = "1"
    meta: dict[str, Any] = field(default_factory=dict)


class RemoteStorage(Protocol):
    def upload(self, path: str, data: bytes, content_type: str) -> str: ...


class SupabaseStorage:
    """Minimal Supabase Storage REST client (no SDK). Service-role key; upsert disabled."""

    def __init__(self, url: str, service_key: str, bucket: str, *, timeout_s: float = 60.0) -> None:
        self._base = url.rstrip("/")
        self._bucket = bucket
        self._client = httpx.Client(
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {service_key}", "apikey": service_key},
        )

    def upload(self, path: str, data: bytes, content_type: str = "application/gzip") -> str:
        resp = self._client.post(
            f"{self._base}/storage/v1/object/{self._bucket}/{path}",
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "false"},
        )
        resp.raise_for_status()
        return f"{self._bucket}/{path}"


class SnapshotStore:
    def __init__(self, root: Path, remote: RemoteStorage | None = None) -> None:
        self._root = root
        self._remote = remote

    @property
    def root(self) -> Path:
        return self._root

    def write(
        self,
        key: SnapshotKey,
        payload: bytes,
        *,
        pulled_at: datetime | None = None,
        ext: str = "json",
        record_count: int | None = None,
        valid: bool = True,
        validation_notes: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> SnapshotRecord:
        pulled_at = pulled_at or datetime.now(UTC)
        rel = key.relative_path(pulled_at, ext=ext)
        target = self._root / rel
        if target.exists():
            raise FileExistsError(f"snapshot already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)

        gz = gzip.compress(payload, compresslevel=6)
        target.write_bytes(gz)
        sha = hashlib.sha256(payload).hexdigest()

        remote_path = None
        if self._remote is not None:
            try:
                remote_path = self._remote.upload(rel, gz, "application/gzip")
            except Exception:  # noqa: BLE001 — local copy is authoritative; remote is a mirror
                log.exception("remote upload failed for %s (local copy kept)", rel)

        return SnapshotRecord(
            key=key,
            pulled_at=pulled_at,
            sha256=sha,
            byte_size=len(payload),
            storage_path=rel,
            remote_path=remote_path,
            record_count=record_count,
            valid=valid,
            validation_notes=validation_notes,
            meta=meta or {},
        )

    def read(self, storage_path: str) -> bytes:
        return gzip.decompress((self._root / storage_path).read_bytes())


class SnapshotRepository:
    """Persists snapshot metadata. Session is injected; commit is the caller's."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, rec: SnapshotRecord) -> Snapshot:
        row = Snapshot(
            source=rec.key.source,
            kind=rec.key.kind,
            season=rec.key.season,
            week=rec.key.week,
            pulled_at=rec.pulled_at,
            sha256=rec.sha256,
            byte_size=rec.byte_size,
            storage_path=rec.storage_path,
            remote_path=rec.remote_path,
            record_count=rec.record_count,
            schema_version=rec.schema_version,
            valid=rec.valid,
            validation_notes=rec.validation_notes,
            meta=rec.meta or None,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def latest(self, key: SnapshotKey, *, valid_only: bool = True) -> Snapshot | None:
        from sqlalchemy import select

        stmt = (
            select(Snapshot)
            .where(Snapshot.source == key.source, Snapshot.kind == key.kind)
            .where(
                Snapshot.season.is_(None) if key.season is None else Snapshot.season == key.season
            )
            .where(Snapshot.week.is_(None) if key.week is None else Snapshot.week == key.week)
            .order_by(Snapshot.pulled_at.desc())
            .limit(1)
        )
        if valid_only:
            stmt = stmt.where(Snapshot.valid.is_(True))
        return self._session.scalars(stmt).first()
