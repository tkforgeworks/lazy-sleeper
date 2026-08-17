# lazy-sleeper — Claude reference

Draft + in-season NFL fantasy helper for Sleeper leagues. **Python backend repo** (ingestion, scoring,
providers, metrics, FastAPI). Flutter client is a separate repo (`lazy-sleeper-app`) with no domain logic.
Keep this file in sync as decisions land.

## Deadline & milestones

Live draft **Fri 2026-09-04 8 PM ET**. Milestones (full detail in `docs/execution-plan-analysis_20260816.md` §2/§7):
M0 bootstrap ✅ → M1 scoring engine + join spine ✅ (2026-08-17) → M2 benchmark scoreboard → M3 consensus
draft board → M4 live draft companion → M5 ForgeModel (first thing to cut) → M7 in-season → M8 productionization.
Product/architecture spec: `docs/draft-companion-execution-plan_20260816.md`.

## Status (updated 2026-08-17 — refresh this block whenever a story merges)

- **Done:** LS-10, 13, 14, 15, 18, 19, 20, 21, 22 (PRs #1–#9). 131 tests, `ci` required on `main`.
- **Next up, in order:** M2 = **LS-23** (season scoreboard Sleeper/ESPN/naive 2024–25, MAE + Spearman by
  position, `lazy benchmark season`, committed CSV) → LS-24 (weekly) → LS-25 (inverse-error blend weights as
  versioned JSON + `ProjectionProvider`/`SleeperProvider`/`EspnProvider`/`EnsembleProvider`). Then slot
  **LS-17** daily scheduler (+ LS-11/12 Supabase bucket & `lazy sync`) before the board. M3 = LS-26→30
  (baselines, flex-aware VORP, tiers/cliffs, ADP delta/disagreement, `/board` + `lazy board regen`).
  M4 = LS-16 → LS-31–38.
- **Open loose ends (not blocking M2/M3, blocking draft-night reliability):** LS-11/12 (archive is
  single-machine since the 08-16 backup), LS-17 (nothing runs `lazy pull daily` unattended), LS-16.
- **Guidance for LS-23 (from the 2026-08-17 review):** stored 2024/25 season projections are genuine
  preseason vintages (`last_modified` = Jan-after is ADP churn; Spearman vs actuals 0.3–0.7, MAE 40–80 →
  no leakage). Quick top-N-by-projection numbers: 2025 RB Sleeper 0.67 / ESPN 0.70, WR 0.59/0.59,
  QB 0.33/0.15, TE 0.36/0.33 — the plan doc's 0.71/0.86 anchor does **not** reproduce; define the pool
  (recommend top-N by preseason ADP per position), report MAE + Spearman on it, and expect QB/TE weights
  to need shrinkage toward market. K/DEF: don't blend Sleeper naively (missing short FGs / no pts-allowed).
- Jira: move the story to In Progress when the branch opens, Done when the PR merges.

## Stack (decided 2026-08-16)

- Python ≥3.12, **uv** (`uv sync` → `.venv`; `uv.lock` committed, CI installs `--locked`; dev deps in
  `[dependency-groups] dev`; run tools as `uv run pytest` / `uv run ruff` / `uv run lazy`), hatchling,
  ruff (line 100), pytest. CLI entrypoint `lazy` (`lazy_sleeper.jobs.cli`) — was `ls`, renamed to avoid
  shadowing the shell command.
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
