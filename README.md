# lazy-sleeper

Draft-day companion and in-season roster assistant for [Sleeper](https://sleeper.com) NFL fantasy leagues.
Answers "who should I take right now, and why?" from the league's exact scoring, live draft state, and a
tunable projection ensemble.

This repo is the **Python backend**: ingestion, scoring engine, projection providers, metrics, and the
FastAPI surface. The Flutter client lives in a separate repo (`lazy-sleeper-app`) and contains no domain logic.

## Status

M0 (bootstrap) — ingestion + immutable snapshot archive + DB schema + CLI + CI. See
[`docs/execution-plan-analysis_20260816.md`](docs/execution-plan-analysis_20260816.md) for the milestone plan
and [`docs/draft-companion-execution-plan_20260816.md`](docs/draft-companion-execution-plan_20260816.md) for
the product/architecture spec.

## Quick start

```bash
python -m venv .venv && . .venv/Scripts/activate      # or source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                                   # local Docker Postgres by default
docker compose up -d                                   # Postgres 16 on localhost:5433
lazy db upgrade                                          # alembic upgrade head

lazy pull daily                                          # players + 2026 proj/ADP + ESPN + crosswalk
lazy load players && lazy load crosswalk                   # → core.players / core.crosswalk
uvicorn lazy_sleeper.api.app:app --reload              # http://127.0.0.1:8000/docs
```

`lazy --help` lists every command (`pull projections 2025 --week 3`, `pull league`, `pull picks`,
`pull nflverse 2025`, `backfill <dir> --pulled-at <date>`, ...).

## How data flows

```
external source → HttpClient → validate (shape + count) → SnapshotStore
                                                            ├─ data/snapshots/**/*.gz   (local, gitignored)
                                                            ├─ Supabase Storage          (mirror, when configured)
                                                            └─ raw.snapshots             (metadata row in Postgres)
                                          loaders → core.*  (players, crosswalk, … parsed tables)
```

- **Snapshots are immutable and dated.** Every external pull is kept forever; providers revise their data
  silently, so the archive we build is the only one we trust. Failed validation is still snapshotted
  (`valid=false`) — loaders skip it and use the last valid one.
- **Raw payloads never go into Postgres.** ~118 MB/day raw → ~15 MB gzipped on disk/Storage; Postgres holds
  metadata + narrow parsed tables. Fits the Supabase free tier for a season.
- **Plain Postgres schema** (`raw`, `core`, `derived`) — the same Alembic migrations run on Docker and Supabase.

## Layout

```
lazy_sleeper/
  config.py     Settings (env / .env)
  db/           SQLAlchemy models + session factory        alembic/   migrations
  ingest/       http, sleeper, espn, nflverse clients; snapshots; validate; loaders; pipeline
  jobs/cli.py   `ls` CLI
  api/          FastAPI app (health, snapshots; board/draft endpoints arrive in M3/M4)
  scoring/ metrics/ providers/ model/ benchmark/   (M1+)
tests/          unit tests + trimmed real-payload fixtures
```

## Contributing / branch policy

`main` is protected by the TK ForgeWorks standard repository ruleset (see
[`tkforgeworks/.github/docs/branch-protection-ruleset.md`](https://github.com/tkforgeworks/.github/blob/main/docs/branch-protection-ruleset.md)):
no direct pushes, all changes via PR, no bypass. CI job `ci` (ruff + pytest + migration round-trip) runs on
every PR. Commit subjects follow `LS-N: Imperative summary` (Jira key `LS`); bug fixes start with `Fix`.
