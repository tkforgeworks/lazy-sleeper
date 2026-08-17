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

## Dev setup (new machine)

Prereqs: **Python ≥ 3.12**, **Docker Desktop** (local Postgres), **git** + **gh** (`gh auth login`), and the
Supabase `sb_secret_...` key for the `lazy-sleeper` project (Project Settings → API Keys).

```bash
gh repo clone tkforgeworks/lazy-sleeper && cd lazy-sleeper
python -m venv .venv && . .venv/Scripts/activate      # Windows; source .venv/bin/activate elsewhere
pip install -e ".[dev]"
cp .env.example .env                                   # then fill SUPABASE_URL / SUPABASE_SECRET_KEY
docker compose up -d && lazy db upgrade
```

**Things that are NOT in git and must be moved by hand:**

1. **`data/snapshots/`** — the local raw archive (~30 MB gz). It contains the irreplaceable 2026-08-16
   preseason vintage; providers revise their data, so it cannot be re-pulled. Copy the folder (or restore it
   from Supabase Storage once `lazy sync` / LS-12 exists), then rebuild the DB from it:
   ```bash
   lazy snapshots reindex          # re-registers every archive file in raw.snapshots
   lazy load players && lazy load crosswalk && lazy load stats
   ```
   Also keep `data_pulls/ff-projections-2026-08-16.zip` (16 MB, the original pull) somewhere off-machine.
2. **`.env`** — never committed. Only `SUPABASE_URL` / `SUPABASE_SECRET_KEY` need real values today.
3. **`docker compose` volume** — the dev DB is per-machine; rebuild it with the commands above rather than
   copying the volume.

Everyday loop: `lazy pull daily` (fresh Sleeper/ESPN/crosswalk snapshots) → `lazy load stats` →
`pytest -q && ruff check .` → branch `LS-N-…` → PR to `main` (self-merge OK once `ci` is green).

Claude Code notes: repo instructions live in `.claude/CLAUDE.md` (travels with the repo). Per-machine bits
(`~/.claude/CLAUDE.md`, custom subagents, session memory) do not — set those up once on the new box.

## Quick start

```bash
python -m venv .venv && . .venv/Scripts/activate      # or source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                                   # local Docker Postgres by default
docker compose up -d                                   # Postgres 16 on localhost:5433
lazy db upgrade                                          # alembic upgrade head

lazy pull daily                                          # players + 2026 proj/ADP + ESPN + crosswalk
lazy load players && lazy load crosswalk                   # → core.players / core.crosswalk
lazy load stats                                          # → core.projections/actuals/adp/snap_counts/expected_points
lazy score rules                                         # the league's scoring_settings (latest league snapshot)
lazy score preview --position RB --top 20                # score latest 2026 projections; --actuals/--week/--source too
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
                                          loaders → core.*  (players, crosswalk, projections, actuals, adp,
                                                            snap_counts, expected_points)
```

- **Snapshots are immutable and dated.** Every external pull is kept forever; providers revise their data
  silently, so the archive we build is the only one we trust. Failed validation is still snapshotted
  (`valid=false`) — loaders skip it and use the last valid one.
- **Raw payloads never go into Postgres.** ~118 MB/day raw → ~15 MB gzipped on disk/Storage; Postgres holds
  metadata + narrow parsed tables. Fits the Supabase free tier for a season.
- **Plain Postgres schema** (`raw`, `core`, `derived`) — the same Alembic migrations run on Docker and Supabase.
- **One stat vocabulary, two tables.** `core.projections` and `core.actuals` share a `stats` JSONB column keyed
  by Sleeper stat names — the same names the league's `scoring_settings` map uses — so any row from any
  provider scores the same way. ESPN's numeric stat ids are decoded on load (`ingest/espn_stats.py`, verified
  against nflverse actuals). **Projections are vintages** (one row per snapshot, kept forever); **actuals are
  facts** (one row per source/season/week/player, latest load wins). `week` NULL = season. Rows with no
  stat content are dropped at load.

## Layout

```
lazy_sleeper/
  config.py     Settings (env / .env)
  db/           SQLAlchemy models + session factory        alembic/   migrations
  ingest/       http, sleeper, espn, nflverse clients; snapshots; validate; loaders; pipeline
  jobs/cli.py   `lazy` CLI
  api/          FastAPI app (health, snapshots; board/draft endpoints arrive in M3/M4)
  scoring/      rules (league scoring_settings map), engine (score/breakdown, per-position normalizer hook)
  metrics/ providers/ model/ benchmark/   (M1+)
tests/          unit tests + trimmed real-payload fixtures
```

## Contributing / branch policy

`main` is protected by the TK ForgeWorks standard repository ruleset (see
[`tkforgeworks/.github/docs/branch-protection-ruleset.md`](https://github.com/tkforgeworks/.github/blob/main/docs/branch-protection-ruleset.md)):
no direct pushes, all changes via PR, no bypass. CI job `ci` (ruff + pytest + migration round-trip) runs on
every PR. Commit subjects follow `LS-N: Imperative summary` (Jira key `LS`); bug fixes start with `Fix`.
