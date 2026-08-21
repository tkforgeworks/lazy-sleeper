# lazy-sleeper — Claude reference

Draft + in-season NFL fantasy helper for Sleeper leagues. **Python backend repo** (ingestion, scoring,
providers, metrics, FastAPI). Flutter client is a separate repo (`lazy-sleeper-app`) with no domain logic.
Keep this file in sync as decisions land.

## Deadline & milestones

Live draft **Fri 2026-09-04 8 PM ET**. Milestones (full detail in `docs/execution-plan-analysis_20260816.md` §2/§7):
M0 bootstrap ✅ → M1 scoring engine + join spine ✅ (2026-08-17) → M2 benchmark scoreboard ✅ (2026-08-19) →
M3 consensus draft board ✅ (2026-08-20) → M4 live draft companion → M5 ForgeModel (first thing to cut) → M7 in-season → M8 productionization.
Product/architecture spec: `docs/draft-companion-execution-plan_20260816.md`.

## Status (updated 2026-08-20 — refresh this block whenever a story merges)

- **Done:** LS-10, 11, 12, 13, 14, 15, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30 (PRs
  #1–#20). 191 tests, `ci` required on `main`. **M2 complete (2026-08-19). M3 complete 2026-08-20**
  (LS-26 baselines → LS-27 VORP → LS-28 tiers/cliffs → LS-29 flags → LS-30 persisted `/board`).
- **Next up: LS-32** (draft-state model: per-team rosters/needs/slot/picks-until-my-turn — consumes
  `PickEvent`s and `core.draft_picks`; LS-31's `DraftPoller.my_slot(user_id)` + `Settings.sleeper_user_id`
  already resolve "my seat"). LS-31 in PR #23 (2026-08-21).
- **Poller (LS-31, `draft/poller.py`, `lazy draft poll`):** `DraftPoller(source, sink, draft_id)` —
  `poll_once()` = snapshot picks → `sink.sync` (= `load_draft_picks`) → diff pick_nos against what the
  table held → one `PickEvent` per new pick (pick_no order; carries slot/round/sleeper_id/`metadata`
  name; `picked_by None` = autopick). `run(on_pick, stop=, max_polls=, until_complete=)` loops at
  `interval_s` 5, backoff `interval×2^failures` capped `max_backoff_s` 60 (+≤25 % jitter), resets on
  success, never dies; stops on draft `status == complete` or `picks ≥ rounds×teams` (+ a final
  draft-doc refresh so `core.drafts` ends `complete`). Draft doc re-read **every poll until it has
  `draft_order` and status `drafting`** (Sleeper filled `draft_order` in mid-draft on the 8/21 mock),
  then every 10. CLI tags `<-- you` on `picked_by == sleeper_user_id` (slot match only for
  autopicks) and writes INFO logs to `data/logs/draft_poll_<id>_<stamp>.log` (one `poll …`/`pick …`
  key=value line each — the parseable record of the night); console shows picks + WARNING only. Identical payload (sha256) → snapshot still
  written, DB sync skipped. Undo = `PollResult.removed`, not an event. Sync callback on the poll
  thread (LS-34 wraps it in a thread or runs `lazy draft poll` as its own process). Ports:
  `SleeperPickSource`(session per poll)/`DbPickSink` in prod, `ReplaySource`/`MemorySink` in tests.
  **Replay fixture** `tests/fixtures/mock_draft_1396298350046760960.json.gz` (5.7 KB) = the mock's
  final 180 picks (metadata trimmed) + the 14 poll cut points — LS-36's offline replay head start.
  Second mock `1396601438095835136` (2026-08-21, Tim slot 9) ran through `lazy draft poll` live:
  56 polls, 180 picks, 0 failures, ~5.5 s cadence; also in `core.*` as dev data.
- **Mock-draft rehearsal (2026-08-20):** league mock `1396298350046760960` (`metadata.type=league_mock`,
  started from the real league: 12 teams, 15 rds, 120 s) drafted to `complete`; polled with
  `lazy pull picks --draft-id … --load` every 20 s → 12 polls, 180 picks, 0 removals, loader unchanged.
  Its rows stay in `core.drafts`/`core.draft_picks` as **dev data for M4** (static — do not re-pull).
  Lessons for the state model: key seats on **`draft_slot`** (mock picks have `roster_id` NULL and the
  draft has `league_id` NULL; `slot_to_roster_id` exists in both) with `core.drafts.slot_to_roster_id`
  as the bridge; `draft_order` = `{user_id: slot}` — Tim = `1268591266036203520`, slot 8 in the mock
  (add `sleeper_user_id` to Settings for "my seat"); CPU picks have `picked_by ""` → NULL; payload
  also carries `reactions` (null, ignored). Status seen: `pre_draft` → `complete` (`last_picked` set).
- **Draft state (LS-16, `ingest/league_loaders.py`, migration 0009):** `core.drafts` (one row per
  draft: status/type/rounds/teams/pick_timer lifted from `settings`, `slot_to_roster_id`,
  `draft_order` JSONB), `core.draft_picks` (PK `draft_id, pick_no`; **sync** semantics — upsert what's
  in the payload, delete that draft's picks that aren't → commissioner undo converges), `core.rosters`
  (PK `league_id, roster_id`; player lists JSONB), `core.league_users` (`display_name`, `team_name`
  from metadata). **Sleeper picks carry no timestamp** → `first_seen_at` = `pulled_at` of the snapshot
  that first showed the pick, kept on conflict (accurate to the poll interval). `picked_by == ""` =
  autopick → NULL. `parse_*` are pure (tested on fixtures; the picks fixture is hand-built from
  Sleeper's documented shape — no real 2026 picks exist yet); `load_*` upsert via attr→column map
  (`metadata_` ↔ `metadata`). CLI: `lazy load league [--draft-id]`, `lazy pull league --load`,
  `lazy draft poll --draft-id <mock>` = the way to exercise the poller on a Sleeper mock draft
  before 9/4 (`lazy pull picks --load` is the one-shot equivalent). Not in the daily workflow (draft state is a draft-night poll, not a daily pull).
- **Board serving (LS-30, `board/store.py` + `board/render.py`, migration 0008):** `regenerate(session,
  provider, rules, scorer, season, baseline=)` = `build_board` + `core.players` name/injury join →
  `derived.boards` (dated, immutable, `config` JSONB snapshot) + `derived.board_rows` (flattened
  `BoardRow`, `ROW_FIELDS`). `BoardRepository.latest(season, provider)` / `.rows(board_id, position,
  limit)`. API: `GET /board` (provider default `ensemble`, 404 until first regen), `GET /board.html`
  (`to_html`, self-contained, draft-night fallback), `POST /board/regen`. CLI `lazy board regen` also
  writes `data/boards/board_<season>_<provider>_<stamp>.{csv,html}` + `board_latest.*` (gitignored;
  uploaded as a 14-day artifact by `daily-pull.yml`, which runs regen after freshness). Provider
  names resolve in one place: `providers.make_provider(session, scorer, name)`; `_Ctx.provider` and
  the API both use it. `ingest.snapshots.store_from_settings(settings)` builds the SnapshotStore.
- **Open loose ends:** LS-51 (freshness flags historical seasons
  STALE) parked for 0.2.0. LS-52 (skip identical snapshots by sha256) + LS-53 (`core.projections` →
  latest-wins upsert with pre-game freeze) — Supabase growth was ~9.5 MB/day DB + ~7 MB/day Storage
  as of 2026-08-19 (each pull appends a 17k-row ESPN kona vintage); land before the free tier bites.
- **Baselines (LS-26, `board/baselines.py`, `lazy board baselines`):** replacement level = points of
  the last starter; cutoffs derived from `roster_positions` × `total_rosters` (now on `ScoringRules`)
  + flex seats filled greedily by value (most-restrictive seat first). 2025 actuals reproduce the plan
  anchors within one flex seat (QB12 282.9, RB30/WR40/TE14 ≈ 147–150). Historical = per-season
  2023–25 baselines averaged (`HistoricalBaselines.average` — the VORP input); DEF averages 2024–25
  only (no 2023 ESPN weekly DEF). Live = `live_baselines(provider, shape, season)` on the ensemble.
- **VORP (LS-27, `board/vorp.py`, `lazy board vorp`):** `vorp_board(projections, baselines)` →
  `PlayerValue` (points, baseline, vorp, pos_rank, ensemble `components` passed through for LS-29).
  Default baseline is **live** (`live_vorp` — derived from the same projection table, so provider
  bias cancels and the last starter sits at exactly 0); `--baseline historical` = 2023–25 actuals
  average x-check. 2025 flex skew is a DB-free test on the LS-21 parity fixture (16 WR/6 RB/2 TE —
  one seat off the plan's observed 17/5/2; weekly start/sit isn't recoverable from season totals).
- **Tiers/cliffs (LS-28, `board/tiers.py`, migration 0006):** adaptive gap-based per position —
  tier break at `gap ≥ max(min_gap, gap_multiplier × median gap over the position's depth window)`
  (depth = benchmark pool sizes); **cliff** = absolute `gap_to_next ≥ cliff_gap` (default 15 season
  pts ≈ 1/wk). Thresholds live in **`derived.board_config`** (single row, defaults 15/2.0/4.0),
  app-adjustable via `GET/PUT /board/config` and `lazy board config` — the draft-day "scary" dial.
  `assign_tiers(values, TierConfig) → BoardRow(value, tier, cliff, gap_to_next)`; `lazy board vorp`
  shows tier/gap/CLIFF columns (`--cliff-gap` overrides per run). LS-30's `/board` renders BoardRow.
- **Flags (LS-29, `board/flags.py`, migration 0007):** `flag_adp(rows, latest_adp(s, season), cfg)` —
  `adp_delta = adp_ppr − overall board rank`, flag `value`/`reach` at `|Δ| ≥ max(adp_min_delta 12,
  adp_pct 0.25 × adp)`; must run on the *unfiltered* VORP-ordered board (rank = row index).
  `flag_disagreement(rows, cfg)` — spread between ensemble `components` (sleeper/espn), flag at
  `≥ max(disagree_min_pts 20, disagree_pct 0.15 × points)`; **`debias_disagreement`** (default on)
  rescales each member by its position-median ratio to the blend first (raw: 27/32 DEFs flag from
  Sleeper's systematic DEF under-projection; debiased: only real splits). All five thresholds live on
  `TierConfig` / `derived.board_config` (`lazy board config --no-debias …`, `PUT /board/config`).
  `build_board(provider, shape, season, cfg, adp_by_id, baselines=None)` = vorp → tiers → flags
  in one call (LS-30's entry point). Known read: K/DEF all show `value` because the market drafts them
  50–100 picks after their VORP rank — a per-position ADP rank would fix it (possible follow-up).
  `lazy board vorp` has `adp/dadp/spread/flags` columns and `--flags-only`; header uses `dadp` not `Δ`
  (Windows cp1252 console can't print it).
- **Ops state (2026-08-19, LS-11/12/17 done):** **the shared DB is Supabase Postgres** — cut over via
  `pg_dump | psql` (row counts verified identical; `check joins` baseline reproduced), migrations 0001–0006
  applied (0005 = RLS on `public.alembic_version` for the Supabase advisor). Local `.env` `DATABASE_URL` →
  Supabase session pooler (`postgresql+psycopg://postgres.<ref>:…@aws-0-us-east-1.pooler.supabase.com:5432/
  postgres`); the Docker DB is kept as `DATABASE_URL_LOCAL` for diffs only. Archive fully mirrored to
  Storage bucket `raw-snapshots` (`lazy sync push|pull`; `SnapshotStore.read` fetches on demand). Daily pull
  = `.github/workflows/daily-pull.yml`, 06:00 ET, checks out `main` (no puller box to update), repo secrets
  set, **first run green 2026-08-19 05:23 UTC incl. ESPN**. New machine: clone → `uv sync` → same `.env`
  → optional `lazy sync pull`. Fallback host if GitHub runners ever get blocked: homelab k8s CronJob.
- **Benchmark (LS-23, `lazy benchmark season`, `benchmark/season.py`, `metrics/`):** pool = top-N by
  preseason Sleeper ADP per position (QB 24 / RB 60 / WR 72 / TE 24 / K 24 / DEF 24, ADP ≤ 300 — the
  ADP tail is unsigned kickers), providers `sleeper`/`espn` = latest stored season vintage, `naive` =
  prior-season actual; actuals = Σ weekly (nflverse for offense+K, ESPN weekly for DEF); pool players
  with no actuals score 0. Result (`data/benchmarks/season_scoreboard.csv`, committed): RB/WR ρ 0.55–0.74
  and MAE 55–70 for both providers, QB/TE weak (2025 QB ρ 0.17/−0.07), K ≈ noise, both beat naive
  except K; 2025 bias +45–70 on QB/WR (busts). Sleeper DEF −12 / ESPN DEF +17 bias. Stored 2024/25
  projections are genuine preseason vintages (`last_modified` = Jan-after is ADP churn). The plan doc's
  0.71/0.86 anchor does not reproduce under any pool. LS-25: shrink QB/TE/K toward market, DEF → ESPN.
  **Weekly (LS-24, `lazy benchmark weekly`, `benchmark/weekly.py`):** week pool = ADP pool ∩ projected > 0
  by any provider; latest pre-game vintage per (source, season, week) (verified: ρ 0.3–0.5, no leakage;
  ESPN `week 0` = season mirror, ignored); naive = trailing per-game mean (wk 1 = prior season). Roll-up
  pools MAE over player-weeks, `spearman` = mean per-week ρ. Sleeper ≈ ESPN everywhere (MAE 5–6, RB ρ
  0.62–0.66, WR 0.45–0.52, QB 0.24–0.31, K ≈ 0.1); both beat naive by 0.3–0.5 MAE; weekly bias ≈ 0.
- **Providers + ensemble (LS-25, `providers/`):** `ProjectionProvider` protocol; `SleeperProvider`/
  `EspnProvider` = latest stored vintage scored under league rules; `EnsembleProvider(members, weights)`
  renormalizes per player over the members that have him (rookie fallback), keeps `components` per member.
  Weights live in **`derived.ensemble_weights`** (fitted, append-only `version`, `w ∝ 1/MAE` pooled over
  seasons — ≈50/50 everywhere; `lazy benchmark fit-weights` also writes `data/benchmarks/ensemble_weights.json`),
  **`derived.weight_overrides`** (manual λ), **`derived.ensemble_config`** (`use_overrides`, `weights_version`
  pin). Resolution in `WeightRepository.resolve_all`. CLI `lazy weights show|set|clear|config`; API
  `GET/PUT/DELETE /ensemble/{weights,overrides,config}`. Wire providers via `_Ctx.provider(session, name)`.
- Jira: move the story to In Progress when the branch opens, Done when the PR merges. LS-51 (freshness
  flags historical seasons STALE) is parked for 0.2.0.

## Stack (decided 2026-08-16)

- Python ≥3.12, **uv** (`uv sync` → `.venv`; `uv.lock` committed, CI installs `--locked`; dev deps in
  `[dependency-groups] dev`; run tools as `uv run pytest` / `uv run ruff` / `uv run lazy`), hatchling,
  ruff (line 100), pytest. CLI entrypoint `lazy` (`lazy_sleeper.jobs.cli`) — was `ls`, renamed to avoid
  shadowing the shell command.
- Postgres via SQLAlchemy 2 + Alembic. **Shared DB = Supabase Postgres** (free tier; Pro accepted
  fallback); `docker compose up -d` (port 5433) is a throwaway local DB for experiments/CI. Schemas `raw` / `core` / `derived`; plain Postgres only, no
  local-only extensions — migrations must run unchanged on Supabase.
- FastAPI is the only API. Flutter consumes it; a minimal HTML page from FastAPI is the draft-night fallback.
  No PostgREST-direct reads from clients.
- Remote access for draft night: Tailscale. Later: Cloudflare Tunnel + Access.
- Prototype code from earlier Claude.ai sessions is **not** a source — re-implement from the spec docs.

## Data conventions

- Every external pull → `SnapshotStore` (gzip → `data/snapshots/`, mirrored to Supabase Storage when
  configured) + a `raw.snapshots` metadata row. Snapshots are immutable and dated; never overwrite.
- Raw payloads never go into Postgres (118 MB/day raw). Parsed tables live in `core.*`:
  `players`, `crosswalk`, `projections` (per-snapshot *vintages*), `actuals` (*facts*: unique per
  source/season/week/player, latest wins), `adp` (Sleeper market data per season snapshot). Projections and
  actuals share the `stats` JSONB column in Sleeper vocabulary; `week` NULL = season; empty rows dropped.
  Also `snap_counts` (nflverse, fantasy positions) and `expected_points` (ffverse xFP, `ep` JSONB).
- nflverse column → Sleeper key map lives in `ingest/nflverse_loaders.py`; nflverse uses `LA` for the Rams
  (Sleeper `LAR`) and `NA` for nulls/unattributed player ids. nflverse `fantasy_points_ppr` excludes kicking.
- ESPN stat ids → Sleeper keys via `ingest/espn_stats.py` (empirically verified 2026-08-16). Where ESPN buckets
  don't align (K <40 yd; DEF pts-allowed 14-17/18-21/22-27/35-45/46+) the ESPN-native key is kept, not merged.
- ESPN espn_id → sleeper_id via crosswalk (authoritative) → `core.players.espn_id` → exact normalized
  (name, position, team) match on `core.players` when unique (`stat_loaders.normalize_name`; audited via
  `resolver.resolved_by_name`) — needed for 2026 rookies (Smack/Smyth/Zvada Ks) until the crosswalk catches
  up. DEF via proTeamId → team abbr == Sleeper DEF id (32/32 verified, LS-22).
- `lazy check joins|freshness|player <name> -t TEAM` (`ingest/audit.py`) are the data-quality gate — README §"Data
  quality" has the manual checklist + accepted misses. Baseline 2026-08-17: Sleeper 100%, ESPN 99.9%,
  nflverse 99.8% resolved; top-300 299/300; 0 duplicates.
- Validate shape/count on ingest; failed validation is still stored (`valid=false`) and loaders skip it.
- Stat-level everything: never ingest pre-scored fantasy points as truth; `scoring/` applies the league's
  literal `scoring_settings` map (`ScoringRules.from_league` → `Scorer.score(stats, position)`), i.e.
  `Σ weight[k]·stats[k]` — no hardcoded constants. K/DEF plug in as `Scorer.normalizers` (LS-19/20).
  Evidence: Sleeper's *weekly* QB `pts_ppr` implies 0.05/pass yd vs its own 0.04 map; season totals match.
  `provider_points` on rows is an x-check only. `lazy score rules|preview|kmix` for eyeballing.
- K (LS-19): `scoring/kicking.py` re-expresses any FG line in league buckets by parsing keys as yard
  intervals; coarse ranges (`fgm`, `fgm_0_39`, `fgm_50p`) split by the 2023–25 nflverse distance mix
  (`DEFAULT_MIX`, refresh via `lazy score kmix` after each season). **Sleeper season K projections omit
  all <40-yd FGs and the `fgm` total** — treated as unobserved and imputed from the long range (lands
  within ~2% of ESPN). Misses/XP-misses are inferred from `fga-fgm` / `xpa-xpm`, never imputed.
  Use `default_scorer(rules)` (wires K + DEF normalizers), not bare `Scorer`.
- DEF (LS-20): `scoring/defense.py`. Points-allowed keys parsed as intervals; ESPN's `18_21` straddles the
  league's 14_20|21_27 edge and is split by `DEFAULT_PA_PMF` (ESPN weekly actuals 2024–25); single-game
  rows with integral `pts_allow` bucket exactly. TD roll-ups: `def_td = max(def_td, def_fum_td +
  def_int_td|pass_int_td)`; ESPN `def_st_td` is blocked-kick TDs only, so returns (`def_kr_td`/`def_pr_td`
  or `kr_td`/`pr_td`) are added unless already covered. **Sleeper DEF projections (season + weekly) carry
  no points-allowed data** and only TD sub-keys — never imputed, so Sleeper DEF totals run ~20% under
  ESPN's; LS-25 must not blend them naively. Streaming rank v1 (`streaming_ranks`, `lazy score def-rank`)
  = mean league pts/game over ESPN weekly DEF actuals 2024–25 (nflverse team stats aren't ingested).
- Parity (LS-21): `tests/test_scoring_parity.py` scores all 5,712 2025 weekly offense actuals from a
  77 KB gz fixture vs nflverse `fantasy_points_ppr` — mean |Δ| 0.0098 after the three known map diffs
  (`scoring/league.py::NFLVERSE_PPR_DIFFS`: INT −1 vs −2; FR-TD +6 vs 0; nflverse doesn't charge fumbles
  lost on returns — the 28 residuals, all exactly −2). Regenerate the fixture each season with
  `lazy score parity --write-fixture tests/fixtures/nflverse_actuals_2025_weekly.json.gz`.
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
  self-merge OK. CI job name is `ci` (`.github/workflows/ci.yml`) and is a **required status check** on the
  repo ruleset `main` (id 20916324, non-strict; done 2026-08-17, LS-10). Editing the ruleset is a `PUT`
  that replaces the whole `rules` array — fetch it first and resend all rules.
- Commit subjects → release notes: `LS-N: Imperative summary`; bug fixes `LS-N: Fix ...`. Jira project
  "Lazy Sleeper", key `LS`. Epics LS-1..9 (E1..E9), stories LS-10..50 — key map in analysis doc §8.
  Fix versions: 0.1.0 draft / 0.2.0 season / 1.0.0 production. Layers are labels (no components).
- Org shared standards (`tkforgeworks/.github`) have TS/Electron CI only; this repo hand-rolls Python CI.
