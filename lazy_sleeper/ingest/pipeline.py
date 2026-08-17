"""Pull orchestration: fetch → validate → snapshot → record metadata (→ optionally load).

`Puller` is constructed with its collaborators; no globals. Each `pull_*` returns the
persisted `Snapshot` row. `backfill_dir` imports the pre-existing PowerShell-era archive.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from lazy_sleeper.db.models import Snapshot
from lazy_sleeper.ingest.espn import EspnClient
from lazy_sleeper.ingest.nflverse import NflverseClient
from lazy_sleeper.ingest.sleeper import SleeperClient
from lazy_sleeper.ingest.snapshots import SnapshotKey, SnapshotRepository, SnapshotStore
from lazy_sleeper.ingest.validate import (
    ValidationResult,
    validate_csv,
    validate_espn_kona,
    validate_json_any,
    validate_sleeper_players,
    validate_sleeper_projections,
)

log = logging.getLogger(__name__)

Validator = Callable[[bytes], ValidationResult]


class Puller:
    def __init__(
        self,
        *,
        session: Session,
        store: SnapshotStore,
        sleeper: SleeperClient,
        espn: EspnClient,
        nflverse: NflverseClient,
    ) -> None:
        self._session = session
        self._store = store
        self._repo = SnapshotRepository(session)
        self._sleeper = sleeper
        self._espn = espn
        self._nflverse = nflverse

    # --- generic -------------------------------------------------------------
    def snapshot(
        self,
        key: SnapshotKey,
        payload: bytes,
        validator: Validator,
        *,
        pulled_at: datetime | None = None,
        ext: str = "json",
        meta: dict[str, Any] | None = None,
    ) -> Snapshot:
        result = validator(payload)
        if not result.valid:
            log.warning("validation FAILED for %s: %s", key, result.notes)
        rec = self._store.write(
            key,
            payload,
            pulled_at=pulled_at,
            ext=ext,
            record_count=result.record_count,
            valid=result.valid,
            validation_notes=result.notes,
            meta=meta,
        )
        row = self._repo.add(rec)
        log.info(
            "snapshot %s/%s s=%s w=%s → %s (%d bytes, %s records, valid=%s)",
            key.source,
            key.kind,
            key.season,
            key.week,
            rec.storage_path,
            rec.byte_size,
            rec.record_count,
            rec.valid,
        )
        return row

    # --- sleeper -------------------------------------------------------------
    def pull_sleeper_projections(self, season: int, week: int | None = None) -> Snapshot:
        kind = "projections_week" if week is not None else "projections_season"
        payload = self._sleeper.projections(season, week)
        return self.snapshot(
            SnapshotKey("sleeper", kind, season, week),
            payload,
            validate_sleeper_projections,
            meta={"endpoint": "api.sleeper.com/projections"},
        )

    def pull_sleeper_stats(self, season: int, week: int | None = None) -> Snapshot:
        kind = "stats_week" if week is not None else "stats_season"
        payload = self._sleeper.stats(season, week)
        return self.snapshot(
            SnapshotKey("sleeper", kind, season, week),
            payload,
            validate_sleeper_projections,
            meta={"endpoint": "api.sleeper.com/stats"},
        )

    def pull_sleeper_players(self) -> Snapshot:
        payload = self._sleeper.players()
        return self.snapshot(SnapshotKey("sleeper", "players"), payload, validate_sleeper_players)

    def pull_league_state(self, league_id: str, draft_id: str) -> list[Snapshot]:
        out = []
        for kind, fn in (
            ("league", lambda: self._sleeper.league(league_id)),
            ("league_users", lambda: self._sleeper.league_users(league_id)),
            ("league_rosters", lambda: self._sleeper.league_rosters(league_id)),
            ("draft", lambda: self._sleeper.draft(draft_id)),
            ("draft_picks", lambda: self._sleeper.draft_picks(draft_id)),
        ):
            out.append(self.snapshot(SnapshotKey("sleeper", kind), fn(), validate_json_any))
        return out

    def pull_draft_picks(self, draft_id: str) -> Snapshot:
        return self.snapshot(
            SnapshotKey("sleeper", "draft_picks"),
            self._sleeper.draft_picks(draft_id),
            validate_json_any,
        )

    # --- espn ----------------------------------------------------------------
    def pull_espn_kona(self, season: int) -> Snapshot:
        payload = self._espn.kona(season)
        return self.snapshot(SnapshotKey("espn", "kona", season), payload, validate_espn_kona)

    # --- nflverse ------------------------------------------------------------
    def pull_nflverse_stats(self, season: int) -> Snapshot:
        payload = self._nflverse.stats_player_week(season)
        return self.snapshot(
            SnapshotKey("nflverse", "stats_player_week", season),
            payload,
            lambda b: validate_csv(
                b, required_columns=("player_id", "season", "week"), min_rows=1000
            ),
            ext="csv",
        )

    def pull_nflverse_snaps(self, season: int) -> Snapshot:
        payload = self._nflverse.snap_counts(season)
        return self.snapshot(
            SnapshotKey("nflverse", "snap_counts", season),
            payload,
            lambda b: validate_csv(
                b, required_columns=("pfr_player_id", "season", "week"), min_rows=1000
            ),
            ext="csv",
        )

    def pull_crosswalk(self) -> Snapshot:
        payload = self._nflverse.crosswalk()
        return self.snapshot(
            SnapshotKey("nflverse", "crosswalk"),
            payload,
            lambda b: validate_csv(
                b, required_columns=("sleeper_id", "sportradar_id", "gsis_id"), min_rows=1000
            ),
            ext="csv",
        )

    # --- reindex: rebuild raw.snapshots from the local archive ------------------
    _PATH = re.compile(
        r"^(?P<source>[a-z]+)/(?P<kind>[a-z_]+)/(?P<season>\d{4}|na)/(?P<week>\d{2}|na)/"
        r"(?P<stamp>\d{8}T\d{6}Z)\.(?P<ext>json|csv)\.gz$"
    )
    _VALIDATORS: dict[tuple[str, str], Validator] = {
        ("sleeper", "projections_season"): validate_sleeper_projections,
        ("sleeper", "projections_week"): validate_sleeper_projections,
        ("sleeper", "stats_season"): validate_sleeper_projections,
        ("sleeper", "stats_week"): validate_sleeper_projections,
        ("sleeper", "players"): validate_sleeper_players,
        ("espn", "kona"): validate_espn_kona,
    }

    def reindex(self) -> tuple[int, int]:
        """Register archive files that have no raw.snapshots row (e.g. after a DB rebuild).

        Returns (registered, skipped). Validation is re-run; CSV kinds get a shape-only check.
        """
        from sqlalchemy import select

        known = set(self._session.scalars(select(Snapshot.storage_path)))
        registered = skipped = 0
        for f in sorted(self._store.root.rglob("*.gz")):
            rel = f.relative_to(self._store.root).as_posix()
            if rel in known:
                skipped += 1
                continue
            m = self._PATH.match(rel)
            if not m:
                log.warning("reindex: unrecognized path %s", rel)
                continue
            key = SnapshotKey(
                m["source"],
                m["kind"],
                None if m["season"] == "na" else int(m["season"]),
                None if m["week"] == "na" else int(m["week"]),
            )
            pulled_at = datetime.strptime(m["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            payload = self._store.read(rel)
            validator = self._VALIDATORS.get(
                (key.source, key.kind), validate_csv if m["ext"] == "csv" else validate_json_any
            )
            self._repo.add(
                self._store.record_existing(key, payload, rel, pulled_at, validator(payload))
            )
            registered += 1
        return registered, skipped

    # --- backfill of the PowerShell-era archive -------------------------------
    _SLEEPER_SEASON = re.compile(r"^sleeper_proj_(\d{4})_season\.json$")
    _SLEEPER_WEEK = re.compile(r"^sleeper_proj_(\d{4})_wk(\d{2})\.json$")
    _ESPN = re.compile(r"^espn_kona_(\d{4})\.json$")

    def backfill_dir(self, directory: Path, pulled_at: datetime) -> list[Snapshot]:
        """Import files named by data_pull_script.ps1 conventions as snapshots dated `pulled_at`."""
        if pulled_at.tzinfo is None:
            pulled_at = pulled_at.replace(tzinfo=UTC)
        out: list[Snapshot] = []
        for f in sorted(directory.iterdir()):
            if not f.is_file():
                continue
            key: SnapshotKey | None = None
            validator: Validator | None = None
            if m := self._SLEEPER_SEASON.match(f.name):
                key = SnapshotKey("sleeper", "projections_season", int(m[1]))
                validator = validate_sleeper_projections
            elif m := self._SLEEPER_WEEK.match(f.name):
                key = SnapshotKey("sleeper", "projections_week", int(m[1]), int(m[2]))
                validator = validate_sleeper_projections
            elif m := self._ESPN.match(f.name):
                key = SnapshotKey("espn", "kona", int(m[1]))
                validator = validate_espn_kona
            if key is None or validator is None:
                log.info("backfill: skipping unrecognized file %s", f.name)
                continue
            out.append(
                self.snapshot(
                    key,
                    f.read_bytes(),
                    validator,
                    pulled_at=pulled_at,
                    meta={"backfill_from": str(f)},
                )
            )
        return out
