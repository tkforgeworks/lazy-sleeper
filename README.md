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

Prereqs: **[uv](https://docs.astral.sh/uv/)** (installs Python 3.12 itself if needed), **Docker Desktop**
(local Postgres), **git** + **gh** (`gh auth login`), and the Supabase `sb_secret_...` key for the
`lazy-sleeper` project (Project Settings → API Keys).

```bash
gh repo clone tkforgeworks/lazy-sleeper && cd lazy-sleeper
uv sync                                                # creates .venv from uv.lock (runtime + dev deps)
cp .env.example .env                                   # then fill SUPABASE_URL / SUPABASE_SECRET_KEY
docker compose up -d && uv run lazy db upgrade
```

Every command below is `uv run <cmd>` — or activate the venv once (`source .venv/bin/activate`;
`.venv\Scripts\activate` on Windows) and drop the prefix. `uv.lock` is committed; `uv sync` never
changes it. To add/upgrade a dependency use `uv add <pkg>` / `uv lock --upgrade-package <pkg>` and commit
the lockfile.

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
`uv run pytest -q && uv run ruff check .` → branch `LS-N-…` → PR to `main` (self-merge OK once `ci` is
green; `ci` runs the same `uv sync --locked` + ruff + pytest + migration round-trip).

Claude Code notes: repo instructions live in `.claude/CLAUDE.md` (travels with the repo). Per-machine bits
(`~/.claude/CLAUDE.md`, custom subagents, session memory) do not — set those up once on the new box.

## Quick start

```bash
uv sync && source .venv/bin/activate                   # .venv\Scripts\activate on Windows
cp .env.example .env                                   # local Docker Postgres by default
docker compose up -d                                   # Postgres 16 on localhost:5433
lazy db upgrade                                          # alembic upgrade head

lazy pull daily                                          # players + 2026 proj/ADP + ESPN + crosswalk
lazy load players && lazy load crosswalk                   # → core.players / core.crosswalk
lazy load stats                                          # → core.projections/actuals/adp/snap_counts/expected_points
lazy score rules                                         # the league's scoring_settings (latest league snapshot)
lazy score preview --position RB --top 20                # score latest 2026 projections; --actuals/--week/--source too
lazy score def-rank                                      # season-average DEF streaming rank (2024–25 actuals)
lazy score parity                                        # engine vs nflverse PPR on 2025 weekly actuals
lazy check freshness && lazy check joins                 # data-quality audit (see below)
lazy check player "ja'marr chase" -t CIN                 # one-player dossier: ids, projections vs ours, actuals
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

## Data quality — checks to run before trusting the DB

Run these after every `lazy pull daily` + `lazy load stats`, and always the morning of the draft. Each one
takes seconds; together they cover freshness, identity joins, and scoring.

| # | Command | What good looks like | If it's off |
|---|---------|---------------------|-------------|
| 1 | `lazy check freshness` | Every feed you care about is hours old, not days; no `STALE`/`INVALID` flags; `projections_week` shows 18 weeks for past seasons; row counts steady (Sleeper players ≈12k, projections ≈3.3k, ESPN kona ≈1k) | A missing/old row = a pull failed silently → rerun `lazy pull <feed>` and check the log. `INVALID` = provider changed shape → look at `raw.snapshots.validation_notes`. |
| 2 | `lazy check joins` | Crosswalk ≈6.3k rows, sportradar agree ≈99%, ≤ a handful of conflicts; **top-300 ≥ 299/300**; resolution ≥ 99% per feed with **no unresolved row ≥ 20 pts**; ESPN DST `OK` (32/32); duplicates `0` in both tables | An unresolved row with real points is a player the board would silently drop (2026 rookies until the crosswalk catches up). The name tier resolves exact name+position+team; if it doesn't, note the id and fix by hand (or wait for the next `lazy pull crosswalk`). Duplicates > 0 = two source rows mapped to one player — stop and investigate before building a board. |
| 3 | `lazy score rules` | The league's map matches what Sleeper shows in Settings → Scoring (4-pt pass TD, 0.04/yd, −1 INT, PPR, −2 fum lost, FG 3/3/3/4/5/6, DEF 10…−4) | Wrong map = someone edited league scoring; `lazy pull league` refreshes it. |
| 4 | `lazy score preview --position QB --top 10` (repeat RB/WR/TE) | Names you recognise in a sane order; `pts` ≈ `provider` for Sleeper season rows (this league is Sleeper's default map) | Big gaps = stat vocabulary drift on a feed → check the latest snapshot's keys against `ingest/espn_stats.py` / `nflverse_loaders.py`. |
| 5 | `lazy score preview --position K --top 10` and `--source espn` | Sleeper and ESPN top kickers within ~5% of each other (Sleeper is imputed — see CLAUDE.md) | Sleeper far below ESPN = the imputation isn't firing (keys changed). |
| 6 | `lazy score preview --position DEF --source espn --top 10` and `lazy score def-rank` | Plausible DEF order; ESPN season DEF 120–160 pts; def-rank top ~10 ppg | Sleeper DEF is expected to be ~20% low (no points-allowed data) — don't use it for DEF totals. |
| 7 | `lazy score parity` | mean \|Δ\| < 0.02, only −2.00 residuals (return fumbles) | Anything else = the engine or the nflverse loader changed → the parity test in CI should also be red. |
| 8 | `lazy check player "trey smack" -t GB` (repeat for a star QB and a rookie WR; `--weeks` adds weekly rows) | One `players` row with the right team/position; crosswalk present with matching sportradar id (or `ABSENT` for a rookie, with ESPN/nflverse rows still attached via the name tier); every source's projection scores close to `provider` for offense; actuals `ours` == `provider` for nflverse | Wrong person attached (different src id / sportradar mismatch) → identity bug; right person, wrong points → scoring/vocabulary bug. Compare the Sleeper season row against the number in the Sleeper app. |

Known, accepted misses (as of 2026-08-17): top-300 miss #104 Thomas Odukoya (free-agent TE, Sleeper
`search_rank` artefact, no team — no impact); 3 sportradar conflicts on same-name players (Greg Jones,
Devon Johnson, Ryan Smith TE↔CB) — the crosswalk maps the wrong namesake, none fantasy-relevant. ESPN's
"Matthew Hibner" ≠ Sleeper's "Matt Hibner" (nickname; below 20 pts, ignored).

## Layout

```
lazy_sleeper/
  config.py     Settings (env / .env)
  db/           SQLAlchemy models + session factory        alembic/   migrations
  ingest/       http, sleeper, espn, nflverse clients; snapshots; validate; loaders; pipeline
  jobs/cli.py   `lazy` CLI
  api/          FastAPI app (health, snapshots; board/draft endpoints arrive in M3/M4)
  ingest/audit  data-quality queries behind `lazy check joins|freshness|player`
  scoring/      rules (league scoring_settings map), engine (score/breakdown, per-position normalizer hook),
                kicking (FG distance-mix normalizer), defense (brackets/TD roll-ups + streaming rank),
                league (rules + distributions from DB), parity (engine vs nflverse PPR)
  metrics/ providers/ model/ benchmark/   (M1+)
tests/          unit tests + trimmed real-payload fixtures
```

## Contributing / branch policy

`main` is protected by the TK ForgeWorks standard repository ruleset (see
[`tkforgeworks/.github/docs/branch-protection-ruleset.md`](https://github.com/tkforgeworks/.github/blob/main/docs/branch-protection-ruleset.md)):
no direct pushes, all changes via PR, no bypass. CI job `ci` (ruff + pytest + migration round-trip) runs on
every PR. Commit subjects follow `LS-N: Imperative summary` (Jira key `LS`); bug fixes start with `Fix`.
