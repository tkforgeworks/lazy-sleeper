# lazy-sleeper

Draft-day companion and in-season roster assistant for [Sleeper](https://sleeper.com) NFL fantasy leagues.
Answers "who should I take right now, and why?" from the league's exact scoring, live draft state, and a
tunable projection ensemble.

This repo is the **Python backend**: ingestion, scoring engine, projection providers, metrics, and the
FastAPI surface. The Flutter client lives in a separate repo (`lazy-sleeper-app`) and contains no domain logic.

## Status

**M0–M2 done (2026-08-19):** ingestion + immutable snapshot archive, league scoring engine (QB–DEF, parity
vs nflverse), season + weekly provider benchmarks, provider abstraction + fitted ensemble weights, and the
ops floor — shared Supabase Postgres (cut over 2026-08-19), archive mirrored to Supabase Storage, daily
pull running from GitHub Actions. **M3 complete (2026-08-20):** replacement baselines, flex-aware VORP,
tiers + cliff flags, ADP-delta + provider-disagreement flags, persisted daily boards served at `GET /board`
/ `/board.html` (`lazy board …`, see "Draft board" below). Next: M4 live draft companion. See
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
cp .env.example .env                                   # DATABASE_URL (Supabase), SUPABASE_URL / SECRET_KEY
uv run lazy sync pull                                  # optional: fetch the raw archive from Storage
```

(`docker compose up -d && uv run lazy db upgrade` gives you a throwaway local Postgres instead — same
migrations; point `DATABASE_URL` at `localhost:5433`.)

Every command below is `uv run <cmd>` — or activate the venv once (`source .venv/bin/activate` on
macOS/Linux; `.\.venv\Scripts\Activate.ps1` in Windows PowerShell) and drop the prefix. On Windows,
`uv run lazy …` from the repo root works with no activation at all. `uv.lock` is committed; `uv sync` never
changes it. To add/upgrade a dependency use `uv add <pkg>` / `uv lock --upgrade-package <pkg>` and commit
the lockfile.

**Where the state lives (after LS-11/12/17):**

- **Supabase Postgres** is the shared DB — every machine and the daily-pull workflow point `DATABASE_URL`
  at it. The Docker Postgres in `docker-compose.yml` is for local experiments and CI only.
- **Supabase Storage** bucket `raw-snapshots` mirrors the raw archive; the local `data/snapshots/` is a
  cache. `SnapshotStore.read` downloads any file it doesn't have, and `lazy sync pull` fetches the whole
  archive up front. `lazy sync push` uploads anything registered in `raw.snapshots` that the bucket lacks
  (idempotent — the 2026-08-16 preseason vintage was pushed this way and is safe).
- **`.env`** — never committed: `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SLEEPER_*`.

So a new machine is: clone → `uv sync` → `.env` → `lazy sync pull` (optional; things fetch on demand)
→ done. Rebuilding an *empty* DB from the archive is `lazy db upgrade` → `lazy snapshots reindex` →
`lazy load players && lazy load crosswalk && lazy load stats` → `lazy benchmark fit-weights`.

### Supabase setup (LS-11)

Project `lazy-sleeper` (free tier: 500 MB Postgres, 1 GB Storage; ~165 MB / ~70 MB used as of 2026-08-19
with two seasons of history — growing ~9.5 MB/day DB + ~7 MB/day Storage until LS-52/LS-53 land, since
every daily pull appends a full ESPN kona vintage; Pro is the accepted fallback). One-time:

1. Storage → **New bucket** `raw-snapshots`, private. (`SUPABASE_BUCKET` defaults to that name.)
2. Project Settings → API Keys → create an `sb_secret_…` key → `SUPABASE_SECRET_KEY`; the project URL
   → `SUPABASE_URL`. The store mirrors every pull automatically once both are set.
3. Project Settings → Database → connection string. Use the **Session pooler** (port 5432 — IPv4,
   supports migrations); *not* the direct `db.<ref>.supabase.co` host (IPv6-only on free tier) and *not* the
   transaction pooler `:6543?pgbouncer=true` (breaks Alembic). It's the **database** password (Settings →
   Database → Reset database password if unsure), not your Supabase login. Swap the scheme to psycopg 3:
   `DATABASE_URL=postgresql+psycopg://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`.
4. Supabase's Security Advisor wants RLS on anything in `public`; migration `0005` enables it on
   `public.alembic_version` and revokes the `anon`/`authenticated` grants (our `raw`/`core`/`derived` schemas
   aren't exposed by PostgREST, so they don't trigger it).

**Cut-over from a local Docker DB — done 2026-08-19** (kept as the recipe; the local DB is now just
`DATABASE_URL_LOCAL` in `.env` for diffing):

```bash
# 1. schema on Supabase (DATABASE_URL already pointing at it)
uv run lazy db upgrade
# 2a. exact copy of the local DB (keeps snapshot ids, fitted weights, config) — what was actually run:
URL_PSQL=...   # DATABASE_URL with postgresql:// instead of postgresql+psycopg://
docker compose exec -T db psql "$URL_PSQL" -c "delete from derived.ensemble_config"   # 0004 seeds a row
docker compose exec -T db pg_dump -U lazysleeper --data-only --no-owner \
  --schema=raw --schema=core --schema=derived lazysleeper > /tmp/dump.sql
docker compose exec -T db psql "$URL_PSQL" -v ON_ERROR_STOP=1 -f - < /tmp/dump.sql
# 2b. … or rebuild from the archive instead (slower, no pg tools needed)
uv run lazy snapshots reindex && uv run lazy load players && uv run lazy load crosswalk && \
  uv run lazy load stats && uv run lazy benchmark fit-weights
# 3. mirror the archive and verify
uv run lazy sync push && uv run lazy check freshness && uv run lazy check joins
```

Verified after cut-over: identical row counts on every table (81 snapshots / 102,561 projections /
32,692 actuals / weights v1), `check joins` baseline reproduced (top-300 299/300, 3 known conflicts).

### Daily pull — the scheduler is a GitHub Actions workflow (LS-17)

[`.github/workflows/daily-pull.yml`](.github/workflows/daily-pull.yml) runs `lazy pull daily` →
`lazy load players/crosswalk/stats` → `lazy check freshness` at **06:00 ET** every day (and on demand
from the Actions tab). Every run checks out `main`, so **there is no puller box to keep updated** — merge
to `main` and tomorrow's pull runs it. Snapshots land in the bucket, rows in Supabase Postgres; the
runner's local archive is thrown away. A failed run is a red workflow + GitHub's failure email; the
`Freshness audit` step is what to read when something looks off.

Repository secrets it needs (Settings → Secrets and variables → Actions — **set 2026-08-19**): `DATABASE_URL`,
`SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SLEEPER_LEAGUE_ID`, `SLEEPER_DRAFT_ID`, `SLEEPER_USER_ID`. First manual
run went green end-to-end on 2026-08-19 (Sleeper, ESPN kona, crosswalk all reachable from GitHub runners);
the schedule takes it from here. Caveats: GitHub cron is
best-effort (minutes of drift; the occasional skipped slot on busy hours — re-run by hand); scheduled
workflows are disabled after 60 days without repo activity. Fallback if GitHub runner IPs ever get blocked
by a provider: the same steps as a homelab k8s CronJob (image build in CI); the code path is identical.

Everyday loop: `lazy pull daily` (fresh Sleeper/ESPN/crosswalk snapshots) → `lazy load stats` →
`uv run pytest -q && uv run ruff check .` → branch `LS-N-…` → PR to `main` (self-merge OK once `ci` is
green; `ci` runs the same `uv sync --locked` + ruff + pytest + migration round-trip).

Claude Code notes: repo instructions live in `.claude/CLAUDE.md` (travels with the repo). Per-machine bits
(`~/.claude/CLAUDE.md`, custom subagents, session memory) do not — set those up once on the new box.

## Quick start

```bash
uv sync && source .venv/bin/activate                   # PowerShell: .\.venv\Scripts\Activate.ps1
cp .env.example .env                                   # local Docker Postgres by default
docker compose up -d                                   # Postgres 16 on localhost:5433
lazy db upgrade                                          # alembic upgrade head

lazy pull daily                                          # players + 2026 proj/ADP + ESPN + crosswalk
lazy load players && lazy load crosswalk                   # → core.players / core.crosswalk
lazy load stats                                          # → core.projections/actuals/adp/snap_counts/expected_points
lazy pull league --load                                  # league/users/rosters/draft/picks → core.drafts/draft_picks/rosters/league_users
lazy pull picks --draft-id <mock draft id> --load        # draft-night poll target; point at a Sleeper mock to rehearse
lazy serve                                               # run the API + draft companion; prints all the URLs
lazy score rules                                         # the league's scoring_settings (latest league snapshot)
lazy score preview --position RB --top 20                # score latest 2026 projections; --actuals/--week/--source too
lazy score def-rank                                      # season-average DEF streaming rank (2024–25 actuals)
lazy score parity                                        # engine vs nflverse PPR on 2025 weekly actuals
lazy benchmark season / weekly                           # Sleeper/ESPN/naive vs 2024–25 actuals → data/benchmarks/
lazy benchmark fit-weights && lazy weights show          # inverse-MAE blend weights → derived.ensemble_weights
lazy board baselines                                     # replacement level per position (2023–25 avg + live)
lazy board vorp --top 30                                 # the draft board: VORP, tier/CLIFF, ADP delta, disagreement
lazy board config --cliff-gap 12                         # draft-day dial: stored tier/cliff/flag thresholds
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

## Benchmarks — which provider to trust (E4)

`lazy benchmark season` scores each provider's stored **preseason** season projection under the league's
own rules and compares it to what the same players actually scored (Σ weekly actuals under the same rules:
nflverse for QB/RB/WR/TE/K, ESPN weekly for DEF). The comparison pool is *the market's*, not a provider's:
top-N by preseason Sleeper ADP per position (QB 24 / RB 60 / WR 72 / TE 24 / K 24 / DEF 24, ADP ≤ 300;
`--pool RB=48`, `--max-adp`). Providers: `sleeper`, `espn` (latest stored vintage) and `naive` = the
player's previous-season actual total. Pool players with no actual rows count as 0 (they were drafted and
produced nothing); pool players a provider didn't project are simply missing from its `n`.

Per (season, position, provider): `n_pool`, `n`, `mae`, `bias` (mean pred − actual; + = provider too
high), `rmse`, `spearman`, `mean_actual` (scale for MAE). Output is printed and written to
[`data/benchmarks/season_scoreboard.csv`](data/benchmarks/season_scoreboard.csv) (committed — regenerate
after re-pulling actuals); `--players-out <csv>` dumps the per-player detail behind it.

Reading the 2024–25 scoreboard: Sleeper and ESPN are close on RB/WR (ρ 0.55–0.74, MAE ≈ 55–70 on a
~150-pt mean); both are weak on QB and TE (2025 QB ρ ≈ 0.2 / −0.1), and both beat naive except at K,
where nothing beats anything. Both over-project WR/QB in 2025 by 45–70 pts on average (injury busts count
as 0). Sleeper DEF runs ~12 pts under actual (no points-allowed data), ESPN ~17 over. This is the input to
LS-25's blend weights — expect QB/TE/K weights to shrink toward the market and DEF to lean ESPN.

`lazy benchmark weekly` is the same comparison one week at a time (start/sit horizon): the week's pool is
the season ADP pool ∩ players at least one provider expected to play (projected > 0 — drops byes/injured),
providers are the latest stored **pre-game** vintage for (source, season, week) — verified genuine: ρ 0.3–0.5,
not ~1 — and `naive` = the player's per-game mean over earlier weeks this season (week 1: prior season's
per-game mean). The roll-up per (season, position, provider) pools MAE/bias/RMSE over all player-weeks and
reports `spearman` as the **mean of per-week ρ** plus `spearman_min` (worst week) and `weeks`. Written to
[`data/benchmarks/weekly_scoreboard.csv`](data/benchmarks/weekly_scoreboard.csv); `--weeks-out` dumps the
per-week rows, `--players-out` the player-weeks.

Reading the weekly scoreboard: Sleeper and ESPN are near-identical (weekly MAE ≈ 5–6 pts on ~11-pt RB/WR
means, ≈ 6.2 on ~18-pt QBs); RB ρ ≈ 0.62–0.66, WR 0.45–0.52, TE 0.36–0.40, QB 0.24–0.31, DEF 0.31–0.34,
K ≈ 0.1 (min weeks go negative everywhere except RB). Both beat naive by 0.3–0.5 MAE and 0.05–0.2 ρ — the
providers add real week-to-week signal on top of "he'll do what he's been doing", but not much at K/DEF.
Weekly bias is within ±1 pt for both, so the season-total DEF offsets (Sleeper −12 / ESPN +17) are a
season-projection artefact — the weekly DEF products are fine to blend.

### Ensemble weights (LS-25)

`lazy benchmark fit-weights` turns the two scoreboards into per-horizon, per-position blend weights:
`w_p ∝ 1 / MAE_p` with MAE pooled across seasons (n-weighted), normalized over Sleeper + ESPN (naive is an
opponent, not a member). Each run appends a new **version** to `derived.ensemble_weights` and rewrites the
committed artefact [`data/benchmarks/ensemble_weights.json`](data/benchmarks/ensemble_weights.json). With
the 2024–25 numbers everything lands ≈ 50/50 (Sleeper 52–53 % on season, 50 % weekly) — the point is that
the weights are *measured*, and that ForgeModel slots in as a third member later.

What the ensemble actually uses is resolved from three `derived.*` tables (`providers/weights.py`):

| Table | Role |
|---|---|
| `ensemble_weights` | fitted weights, append-only by `version` (with the `mae`/`n` behind each) |
| `weight_overrides` | manual per-(horizon, position, provider) weights — the λ override; normalized on read |
| `ensemble_config` | one row: `use_overrides` (the app flag) and `weights_version` (pin a fit; NULL = latest) |

Resolution: `use_overrides` on **and** override rows exist for the position → those; else fitted at the pinned
/ latest version; else equal split. Per player, weights are renormalized over the members that projected
him — a rookie only one feed carries gets that feed at 100 % (the spec's consensus fallback).

```bash
lazy weights show [--horizon weekly]                     # in force / fitted / override per position
lazy weights set QB sleeper=0.7 espn=0.3 --enable        # override QB and flip use_overrides on
lazy weights config --use-fitted | --use-overrides       # the switch; --version 2|latest pins a fit
lazy weights clear [QB]                                  # drop overrides (flag untouched)
lazy score preview --source ensemble --position RB       # blended points + each member's column
```

Same over HTTP for the Flutter app: `GET /ensemble/weights?horizon=season`, `PUT /ensemble/overrides`
(`{horizon, position, weights, note}`), `DELETE /ensemble/overrides?horizon=&position=`,
`PUT /ensemble/config` (`{use_overrides, weights_version | latest}`).

Providers (`providers/`): `ProjectionProvider` protocol → `SleeperProvider` / `EspnProvider` (latest stored
vintage in `core.projections`, scored under league rules) and `EnsembleProvider(members, weights)`, whose
`PlayerProjection.components` keeps each member's points for the disagreement flags in LS-29.

## Draft board (E5)

The board is four layers in `board/`, each pure functions over the previous one (LS-26/27/28/29);
`build_board(...)` runs them end to end:

1. **Replacement baselines** (`board/baselines.py`) — replacement level = the points of the *last starter*
   per position. Cutoffs are derived from the league payload (`total_rosters` × dedicated slots), and the
   flex seats are filled greedily by value, most-restrictive seat first — so flex demand moves the RB/WR/TE
   cutoffs with the data instead of a hardcoded share. On 2025 actuals this lands QB12 / RB30 / WR40 / TE14
   (the plan doc's anchors within one flex seat). Historical baseline = per-season 2023–25 actuals averaged;
   live baseline = re-derived from the current ensemble projections.
2. **Flex-aware VORP** (`board/vorp.py`) — `points − baseline[position]`, ranked. Defaults to the **live**
   baseline: measured against the same projection table, provider bias cancels (the benchmarks show
   preseason QB/WR projections running +45–70 hot) and the last starter per position sits at exactly 0.
   `--baseline historical` is the actuals-average x-check. Each row keeps the ensemble members' points
   (`components`) for LS-29's disagreement flags.
3. **Tiers + cliffs** (`board/tiers.py`) — adaptive gap-based tiers per position: a new tier starts where the
   drop between consecutive players ≥ `max(min_gap, gap_multiplier × the position's median gap)` over its
   draftable depth. The **CLIFF** flag is absolute and independent: drop to the next player at the position
   ≥ `cliff_gap` season points (default 15 ≈ 1 pt/week) — "last chair before the music stops".
4. **Market + disagreement flags** (`board/flags.py`) — two signals beside VORP, not inside it.
   **ADP delta** = Sleeper `adp_ppr` (latest 2026 `core.adp` snapshot) − overall board rank: `+` means the
   room lets him fall past where we rank him (**value**), `−` means his ADP is a **reach** by our numbers.
   The flag fires at `|Δ| ≥ max(adp_min_delta, adp_pct × ADP)` (12 picks / 25%) so a 10-pick gap at pick 6
   counts and at pick 150 doesn't. **Disagreement** = the spread between the ensemble members' league-scored
   points on the same player (`components`), flagged at `≥ max(disagree_min_pts, disagree_pct × points)`
   (20 pts / 15%). Sleeper and ESPN carry *systematic* position offsets (Sleeper DEF ~20% low — no
   points-allowed data), so by default each member is rescaled by its position-median ratio to the blend
   first (`debias_disagreement`); with it off, 27 of 32 DEFs flag, with it on only the genuine splits do.
   Known read: K/DEF show `value` across the board because the market deliberately drafts them 50–100
   picks after their VORP rank — read the ADP flag on those positions as "when the run starts", not value.

Thresholds live in `derived.board_config` (one row, seeded by migrations 0006/0007) so they're adjustable
mid-draft without a deploy: `lazy board config --cliff-gap 12 --disagree-min-pts 30 --no-debias`, or from
the app via `GET /board/config` / `PUT /board/config` (partial updates). `lazy board vorp --cliff-gap 20`
overrides for one run without touching the stored values.

```bash
lazy board baselines                        # both baseline tables, cutoffs + flex fills shown
lazy board vorp --top 30                    # full board: … tier / gap / adp / dadp / spread / flags
lazy board vorp --flags-only --top 40       # just the value / reach / DISAGREE rows
lazy board vorp -p RB --baseline historical # one position, actuals-average baseline
lazy board config                           # show stored thresholds (no options = read-only)
lazy board regen                            # persist a dated board + data/boards/board_latest.{csv,html}
```

### Serving the board (LS-30)

`lazy board regen` runs the pipeline over whatever is in `core.*` right now and **persists** the result as a
new dated board (`derived.boards` + one `derived.board_rows` row per player: everything above plus name,
team and Sleeper `injury_status`), and writes CSV + a self-contained HTML page under `data/boards/`
(gitignored; the daily workflow uploads `board_latest.*` as a 14-day artifact). Every run is a new board —
nothing is overwritten, so a board can always be pinned to "what we saw that morning".

The API serves the **latest persisted board**, not a per-request recompute — stable between regens, and a
half-loaded pull can't leak into the draft-night view:

| Endpoint | What |
|---|---|
| `GET /board?season=2026&provider=ensemble&position=RB&limit=50` | `{board: meta, rows: [...]}` — ranked rows with points / VORP / tier / cliff / ADP delta + flag / spread + disagree / injury. `rank` is the overall board rank even when filtered. 404 until the first regen. |
| `GET /board.html` | The same board as a single HTML file (position buttons, flag colours) — the draft-night fallback if the Flutter client misbehaves. |
| `POST /board/regen` `{season, provider, baseline}` | On-demand regen under the current `board_config` — how a `PUT /board/config` change becomes visible without waiting for the 06:00 ET job. |

The daily workflow (`daily-pull.yml`) runs `lazy board regen` after the pull + load + freshness steps.

## Draft state (LS-16, the M4 input)

`lazy pull league --load` / `lazy load league` parse the Sleeper league-state snapshots into current-state
tables (upsert, re-runnable as often as the draft-night poller likes):

| Table | Key | Notes |
|---|---|---|
| `core.drafts` | `draft_id` | status / type / `rounds` / `teams` / `pick_timer` lifted from `settings`; `slot_to_roster_id` and `draft_order` kept as JSONB |
| `core.draft_picks` | `draft_id, pick_no` | **sync** semantics: picks in the payload are upserted, that draft's picks *not* in the payload are deleted — a commissioner "undo pick" converges. Sleeper's pick payload has no timestamp, so `first_seen_at` = `pulled_at` of the snapshot that first showed the pick (kept on later polls; accurate to the poll interval). `picked_by = ""` (autopick) → NULL |
| `core.rosters` | `league_id, roster_id` | `owner_id` → `league_users`; `players` / `starters` / `reserve` / `taxi` / `keepers` as JSONB lists |
| `core.league_users` | `league_id, user_id` | `display_name`, `team_name` (from metadata), `is_owner` (commissioner) |

Draft state is not part of the daily workflow; it's polled on draft night (below).

## Draft night — the live companion (E6) and how to rehearse it

The loop is **poller → state → recompute → `/draft/{id}/state` → page**, all inside the API process
(`DraftHost`, one runner per draft on a daemon thread). Board + ADP + rank map are built once when the
runner starts (~5–10 s); each pick then costs ~50 ms. Measured on the 2026-08-23 mock: 118 polls,
2 s cadence, recompute avg 49 ms.

### Procedure (mock or the real thing)

1. Fresh data: the daily pull has run (or `lazy pull projections 2026 && lazy pull adp 2026`), and
   `lazy board regen` shows a sane `/board.html`. Check the dials on `/board/config.html`.
2. Start the API: **`uv run lazy serve`** (or double-click a shortcut to `scripts/serve.ps1`).
   It prints the exact URLs for the app, the phone, and every page below. Never `--reload` —
   the draft runner lives in the process.
3. Find the draft id (Sleeper room URL `sleeper.com/draft/nfl/<id>`; the real one is
   `SLEEPER_DRAFT_ID` in `.env`, so `/draft.html` needs no id). Open
   `http://<host>:8000/draft/<id>/state.html` on the second monitor and/or phone.
4. Press **start draft runner** *before* the room starts drafting. The status line shows
   `recompute #n … poll 2s`; the clock strip shows your slot once Sleeper assigns `draft_order`
   (on mocks it can land mid-draft — the page picks it up; set `MY_DRAFT_SLOT` in `.env` and restart
   the API if it stays `?`).
5. Draft. **YOU ARE ON THE CLOCK** in yellow = your turn; top row is the recommendation
   (`score` = VORP − what you'd lose by waiting + need bonus; `surv` = chance he survives to your next
   pick; `RUN n` / `CLIFF` tags). Position buttons filter. Tune mid-draft from the **tuning** link:
   unstarred dials apply instantly, starred ones need the restart button (board rebuild, state restored
   from `core.draft_picks`).
6. If the API dies: restart it, press start again — state comes back from the database. If the
   page dies: `lazy draft advise` in a terminal is the same engine one-shot, `lazy draft poll --advise`
   the same loop without a browser.
7. Afterwards: `lazy draft fixture --draft-id <id> --since <UTC start>` writes
   `tests/fixtures/mock_draft_<id>.json.gz` — the night as an offline replay (below).

### Draft-day runbook (2026-09-04, 8 PM ET)

**Morning:**

- [ ] Daily pull ran green (Actions → daily-pull); if not, `lazy pull projections 2026 && lazy pull adp 2026 --load`
- [ ] `lazy pull players --load` — **injury statuses are only as fresh as this pull**; the draft
      surface shows them next to names
- [ ] `lazy check freshness` and `lazy check joins` clean
- [ ] `lazy board regen` → eyeball `/board.html` (top ~30 look sane, no missing names)
- [ ] Dials: `/board/config.html` shows what the last mock settled on

**T-30 minutes:**

- [ ] `SLEEPER_DRAFT_ID` in `.env` is the real draft (Sleeper room URL); `MY_DRAFT_SLOT` set once
      the order is known (else the page learns it from `draft_order`)
- [ ] `uv run lazy serve` (or the desktop shortcut) — copy the printed phone URL
- [ ] Open `/draft.html` on the monitor and phone (LAN IP); press **start draft runner**; status
      line shows `poll 2s` and a recompute under ~200 ms
- [ ] Second terminal ready as backup: `uv run lazy draft poll --advise`

**During:**

- API died → restart uvicorn, press start again (state rebuilds from `core.draft_picks`)
- Page frozen but API alive → refresh; still dead → the backup terminal is the same engine
- K/DEF creeping up the advice mid-draft → tuning page, raise `late_rounds` or lower their weights
- Commissioner undo → handled automatically (rows rebuild); a `poll … removed` line is normal
- Route 404s that "should exist" → a stale server is squatting the port; kill by port, restart

**After:**

- [ ] `lazy draft fixture --draft-id <id> --since <UTC start>` → commit the fixture
- [ ] Keep `data/logs/draft_poll_*.log` (the parseable record of the night)

### Offline replay (CI / regression)

A fixture = the final pick list + every poll's pick count (`ReplayFixture`). `ReplaySource` replays it
through the real poller/engine with no network or DB; `tests/test_draft_poller.py` and
`tests/test_draft_host.py` run both recorded mocks (2026-08-20 slot 8 / 2026-08-23 slot 8) end-to-end
on every CI run, and `test_full_draft_recompute_is_fast` holds the recompute under the LS-34 bound.
Polls that aren't a prefix of the final list (a commissioner undo) are reported and dropped by the
builder — the format stores one pick list.

| Surface | What |
|---|---|
| `GET /draft/{id}/state.html`, `GET /draft.html` | the page (`?limit=&refresh=&interval=`) |
| `GET /draft/{id}/state?position=&limit=` | the decision document (typed in `/openapi.json`) |
| `POST /draft/{id}/start` `{season, interval_s, forever}` / `stop` | runner lifecycle |
| `GET /board/config.html`, `PUT /board/config`, `POST /draft/{id}/config?restart=` | tuning |
| `lazy draft poll --advise`, `lazy draft advise`, `lazy draft fixture` | the CLI equivalents |

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
  metrics/      mae / bias / rmse / spearman (pure Python)
  benchmark/    season + weekly scoreboards: ADP pool → provider projections vs scored actuals; report (CSV)
  providers/    ProjectionProvider; SleeperProvider/EspnProvider (stored vintages); EnsembleProvider; weights
                (inverse-MAE fit + derived.* repository: fitted versions, overrides, config)
  board/        draft board: baselines (roster-derived cutoffs), vorp (PlayerValue rows), tiers
                (BoardRow: tier/cliff/gap), config (derived.board_config repository)
  model/        (M5 ForgeModel)
tests/          unit tests + trimmed real-payload fixtures
```

## Contributing / branch policy

`main` is protected by the TK ForgeWorks standard repository ruleset (see
[`tkforgeworks/.github/docs/branch-protection-ruleset.md`](https://github.com/tkforgeworks/.github/blob/main/docs/branch-protection-ruleset.md)):
no direct pushes, all changes via PR, no bypass. CI job `ci` (ruff + pytest + migration round-trip) runs on
every PR. Commit subjects follow `LS-N: Imperative summary` (Jira key `LS`); bug fixes start with `Fix`.
