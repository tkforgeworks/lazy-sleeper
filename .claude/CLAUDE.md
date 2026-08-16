# lazy-sleeper — Claude reference

Draft + in-season NFL fantasy helper for Sleeper leagues. **Python backend repo** (ingestion, scoring,
providers, metrics, FastAPI). Flutter client is a separate repo (`lazy-sleeper-app`) with no domain logic.
Keep this file in sync as decisions land.

## Deadline & milestones

Live draft **Fri 2026-09-04 8 PM ET**. Milestones (full detail in `docs/execution-plan-analysis_20260816.md` §2/§7):
M0 bootstrap ✅ → M1 scoring engine + join spine → M2 benchmark scoreboard → M3 consensus draft board →
M4 live draft companion → M5 ForgeModel (first thing to cut) → M7 in-season → M8 productionization.
Product/architecture spec: `docs/draft-companion-execution-plan_20260816.md`.

## Stack (decided 2026-08-16)

- Python ≥3.12, hatchling, ruff (line 100), pytest. CLI entrypoint `ls` (`lazy_sleeper.jobs.cli`).
- Postgres via SQLAlchemy 2 + Alembic. Local: `docker compose up -d` (port 5433). Hosted target: Supabase
  free tier (Pro is an accepted fallback). Schemas `raw` / `core` / `derived`; plain Postgres only, no
  local-only extensions — migrations must run unchanged on Supabase.
- FastAPI is the only API. Flutter consumes it; a minimal HTML page from FastAPI is the draft-night fallback.
  No PostgREST-direct reads from clients.
- Remote access for draft night: Tailscale. Later: Cloudflare Tunnel + Access.
- Prototype code from earlier Claude.ai sessions is **not** a source — re-implement from the spec docs.

## Data conventions

- Every external pull → `SnapshotStore` (gzip → `data/snapshots/`, mirrored to Supabase Storage when
  configured) + a `raw.snapshots` metadata row. Snapshots are immutable and dated; never overwrite.
- Raw payloads never go into Postgres (118 MB/day raw). Parsed, narrow tables live in `core.*`.
- Validate shape/count on ingest; failed validation is still stored (`valid=false`) and loaders skip it.
- Stat-level everything: never ingest pre-scored fantasy points as truth; the scoring engine (M1) applies
  the league's literal `scoring_settings` map.
- Join spine = dynastyprocess crosswalk on `sleeper_id`; `sportradar_id` is the verification key. Sleeper's
  own espn/gsis/yahoo ids are sparse — don't rely on them. CSVs from R use `NA` for null.
- Verified asset names live in `lazy_sleeper/ingest/nflverse.py` docstring (stats_player_week_YYYY, etc.).

## Code conventions

- Constructor injection; no module-level engines/clients. Wire collaborators in `jobs/cli.py::_Ctx` or
  `api/app.py::create_app`.
- Tests test behavior; fixtures under `tests/fixtures/` are trimmed real payloads (keep them small).
- Bash tool on this machine wraps commands in single quotes — heredocs containing `'` fail; use the Write tool.

## Repo / process

- `main` is protected (org ruleset): PR-only, no force-push, no bypass. Branch from `main`, open PR,
  self-merge OK. CI job name is `ci` (`.github/workflows/ci.yml`) — **once it has reported on a PR, PATCH the
  ruleset to require it** (see org doc "Updating an existing ruleset"). Not yet done.
- Commit subjects → release notes: `LS-N: Imperative summary`; bug fixes `LS-N: Fix ...`. Jira project
  "Lazy Sleeper", key `LS` (tickets not yet drafted — epics E1–E9 in analysis doc §4).
- Org shared standards (`tkforgeworks/.github`) have TS/Electron CI only; this repo hand-rolls Python CI.
