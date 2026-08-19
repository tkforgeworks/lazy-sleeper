"""`lazy sync` — reconcile the local archive with the Supabase Storage mirror (LS-12).

push: every raw.snapshots row without ``remote_path`` (or whose object is missing) gets uploaded
      from the local file and its ``remote_path`` set. Idempotent: an object that already exists
      remotely (409) is simply recorded. Local files with no DB row are *not* touched — run
      `lazy snapshots reindex` first.
pull: every row whose local file is missing gets downloaded from the mirror. This is how a fresh
      machine (or a CI runner) gets the archive after pointing DATABASE_URL at the shared DB.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from lazy_sleeper.db.models import Snapshot
from lazy_sleeper.ingest.snapshots import SnapshotStore, SupabaseStorage

log = logging.getLogger(__name__)


@dataclass
class SyncReport:
    uploaded: int = 0
    already_remote: int = 0
    downloaded: int = 0
    skipped: int = 0
    missing_local: list[str] = field(default_factory=list)  # push: no file to upload
    failed: list[tuple[str, str]] = field(default_factory=list)  # (storage_path, error)


class Syncer:
    def __init__(self, session: Session, store: SnapshotStore) -> None:
        if store.remote is None:
            raise RuntimeError("Supabase Storage is not configured (SUPABASE_URL / SECRET_KEY)")
        self._session = session
        self._store = store
        self._remote = store.remote

    def _bucket(self) -> str:
        return getattr(self._remote, "bucket", "remote")

    def push(self, *, verify: bool = False, dry_run: bool = False) -> SyncReport:
        """Upload archive files the mirror lacks; ``verify`` re-checks rows with a remote_path."""
        rep = SyncReport()
        stmt = select(Snapshot).order_by(Snapshot.pulled_at)
        for snap in self._session.scalars(stmt):
            path = snap.storage_path
            if snap.remote_path and not verify:
                rep.skipped += 1
                continue
            if not self._store.has_local(path):
                rep.missing_local.append(path)
                continue
            try:
                if self._remote.exists(path):
                    rep.already_remote += 1
                    if snap.remote_path is None and not dry_run:
                        snap.remote_path = SupabaseStorage.remote_path(self._bucket(), path)
                    continue
                if dry_run:
                    rep.uploaded += 1
                    continue
                remote_path = self._remote.upload(
                    path, self._store.local_bytes(path), "application/gzip"
                )
                snap.remote_path = remote_path
                self._session.flush()
                rep.uploaded += 1
                log.info("uploaded %s", path)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409:  # raced/duplicate — it's there
                    snap.remote_path = SupabaseStorage.remote_path(self._bucket(), path)
                    rep.already_remote += 1
                else:
                    rep.failed.append((path, f"HTTP {e.response.status_code}"))
            except Exception as e:  # noqa: BLE001 — keep going, report at the end
                rep.failed.append((path, str(e)))
        return rep

    def pull(self, *, dry_run: bool = False) -> SyncReport:
        """Download archive files that are registered in the DB but missing on disk."""
        rep = SyncReport()
        for snap in self._session.scalars(select(Snapshot).order_by(Snapshot.pulled_at)):
            path = snap.storage_path
            if self._store.has_local(path):
                rep.skipped += 1
                continue
            if dry_run:
                rep.downloaded += 1
                continue
            try:
                self._store.fetch(path)
                rep.downloaded += 1
            except Exception as e:  # noqa: BLE001
                rep.failed.append((path, str(e)))
        return rep
