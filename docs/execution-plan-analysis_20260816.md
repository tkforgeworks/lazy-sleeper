# Lazy Sleeper — Execution Plan Analysis & Jira Scope

**Companion to:** `draft-companion-execution-plan_20260816.md` (v0.1)
**Date:** 2026-08-16 · **Days to live draft:** 19 (Fri Sept 4, 8:00 PM ET)
**Purpose:** turn the v0.1 plan into a sequenced, ticketable body of work; surface the decisions that block starting.

---

## 1. Assessment of the v0.1 plan

**What is solid**

- Data layer is genuinely de-risked: every source validated, joins verified (100% sleeper_id match on top-300), scoring engine parity-checked (mean |Δ| 0.03 over 6,037 rows), preseason vintage confirmed for benchmark data. Phase 0 is done, not "mostly done."
- Architecture principles (stat-level everything, pluggable providers, immutable dated snapshots, projections ≠ market) are the right calls and are cheap to honor from day one.
- Phase ordering is correct in spirit: a usable board exists even if the live companion slips.

**Where it is under-specified or at risk**

| # | Gap | Why it matters | Proposed handling |
| --- | --- | --- | --- |
| G1 | **Prototype code is not in the repo.** Scoring engine, parity check, VORP CSVs, crosswalk join were built in a Claude.ai session against an uploaded zip. The repo holds only `data_pull_script.ps1`. | Phase 1 cannot start until the scoring engine and join logic exist as runnable, version-controlled code. Everything downstream depends on it. | First ticket of the project: land scoring engine + crosswalk join + parity test in the repo (re-implement from the doc's spec; parity fixture = nflverse 2025 weekly `fantasy_points_ppr`). |
| G2 | **Ingestion is split across three places**: the PS script (Sleeper proj + ESPN), a chat session (Sleeper players, nflverse), and nothing yet for league/draft/rosters. No schema validation, no last-good fallback despite the doc calling for both. | Undocumented endpoints are risk #1 in the doc; the mitigations aren't built. | One ingestion module with a per-source fetcher, dated-snapshot writer, schema check. Replaces the PS script. |
| G3 | **Snapshot archive is on one laptop and gitignored.** "Never revised" only holds if it survives a disk. | The 2025 preseason projections are irreplaceable (providers revise). | Decide archive location now (OQ-5). Minimum: zip + copy to NAS/cloud after each pull. |
| G4 | **Timeline vs. availability.** 19 calendar days ≈ 2 full weekends (Aug 22–23, Aug 29–30) + evenings. Doc targets: Phase 2 by Aug 22, Phase 3 by Aug 26, Phase 4 Sept 1–3. That puts ForgeModel v0 + backtest harness on the first weekend and the live companion on the last three weeknights. | ForgeModel is the differentiator but the live companion is the deadline. If both slip, draft night is a spreadsheet. | Reorder (§2): guarantee board + companion first, ForgeModel in whatever time remains. |
| G5 | **Language for Phases 1–5 is unstated.** Doc says Phase 6 is Java/Spring Boot; nothing says what Phases 1–5 are written in. Org shared CI covers TS/Electron only — no Java or Python reusable workflow. | Determines repo skeleton, CI, and how much gets rewritten in Phase 6. | OQ-2. Recommendation in §3. |
| G6 | **Draft-day UI undecided** (doc OD-1). | Phase 4 scope swings by 2–3× depending on the answer. | OQ-3. Recommendation in §3. |
| G7 | **Mock-draft dry run** is a Phase 4 acceptance step with no mechanism. Sleeper mock drafts are ephemeral lobbies. | Untested polling loop on draft night. | Ticket: capture a Sleeper mock draft id, poll `/draft/{id}/picks` end-to-end, plus a recorded-picks replay fixture so the loop is testable offline. |
| G8 | **In-season hosting.** Phase 5 needs a scheduler running Tue/Thu/Sun. Doc defers hosting to Phase 6. | Phase 5 lands weeks 1–2 of season, before Phase 6. | Decide minimum host (homelab cron / k8s CronJob / laptop task scheduler) — OQ-9. |

---

## 2. Proposed execution sequence

Reordered from the v0.1 phase table to protect draft night. Consensus-only board + working companion is the floor; ForgeModel raises the ceiling.

| Seq | Milestone | Content | Target | Cut line |
| --- | --- | --- | --- | --- |
| **M0** | Repo bootstrap | Stack decision, project skeleton, CI (hand-rolled if non-TS), ruleset PATCH after first green run, ingestion module w/ dated snapshots + schema check, archive backup step | Aug 17–19 (evenings) | Must ship |
| **M1** | Scoring engine + join spine in repo | Scoring engine from `scoring_settings` map; crosswalk join; K distance-mix approximation; parity test vs nflverse 2025 as a fixture; players/proj/ESPN loaders | Aug 20–21 | Must ship |
| **M2** | Benchmark scoreboard (doc Phase 1) | Sleeper vs ESPN vs naive, 2024–25, MAE + rank corr by position, season + weekly; fitted inverse-error blend weights; ESPN DST join confirmed | Weekend Aug 22–23 | Must ship (weights feed M3) |
| **M3** | 2026 draft board, consensus ensemble (doc Phase 3-lite) | Ensemble VORP (Sleeper+ESPN, fitted weights), 2026 replacement baselines, tiers/cliffs, ADP delta, disagreement flags, DEF = season-avg streaming rank, daily regen | Aug 24–26 (evenings) | Must ship |
| **M4** | Live draft companion MVP (doc Phase 4) | `/draft/{id}/picks` polling, board recompute per pick, survival prob (ADP + search_rank + roster needs, slot-param), run detection, decision surface, mock-draft dry run + offline replay | Weekend Aug 29–30 + Sept 1–2 | Must ship; degrade to "board + manual refresh" if slipping |
| **M5** | ForgeModel v0 + backtest (doc Phase 2) | Opportunity×efficiency model, 3 knobs, backtest vs M2 scoreboard, add as third provider; rookies fall back to consensus | Fill-in Aug 24–Sept 3 | **First thing to cut.** Board is valid without it. |
| **M6** | Draft night | Sept 4, 8 PM ET. Freeze code Sept 3 evening; final board regen morning of. | Sept 4 | — |
| **M7** | In-season assistant (doc Phase 5) | Tue waiver run (RoS VORP + FAAB suggestion), Thu/Sun start-sit, usage alerts, digest via Resend | Season wks 1–2 | Post-draft |
| **M8** | Productionization (doc Phase 6) | Spec freeze → Spring Boot service, providers, store, schedulers, homelab deploy | Post-draft | Scope after M7 |

Rationale for moving ForgeModel after the board: M3 with fitted Sleeper/ESPN weights already delivers "league-scoring VORP + tiers + ADP edge," which is most of the draft-night value. ForgeModel adds calibrated disagreement — valuable, but not worth risking M4.

---

## 3. Recommendations on the open decisions

> **Superseded in part by §7 (decisions taken 2026-08-16):** stack = Python + Postgres + FastAPI; frontend = Flutter with a FastAPI-served HTML page as draft-night fallback. The tables below are kept for the record of tradeoffs considered.

**Stack for M0–M7 (G5 / OQ-2).**

| Option | Pros | Cons |
| --- | --- | --- |
| **A. Python for M1–M7, Java/Spring for M8** | Fastest iteration for backtests/model fitting (pandas); nflverse tooling is Python/R-adjacent | Two stacks; scoring engine + VORP rewritten in M8; no org CI template; existing subagents are Java-tuned |
| B. Java/Spring from day one | One stack; M8 becomes "harden," not "rewrite"; code-reviewer/test-writer subagents apply; scoring engine is a natural typed domain | Slower for model/backtest work; poor dataframe ergonomics; real risk to the 19-day deadline |
| C. TypeScript/Node throughout | Org CI reusable workflow exists; one stack | Weakest for numerical work; the doc already committed to Spring Boot for production |

**Recommendation: A**, with the mitigation that scoring rules, replacement baselines, and blend weights are all *data* (JSON/CSV committed with test fixtures), so the M8 Java port is a spec-driven reimplementation with a parity test, not archaeology. If you'd rather not run two stacks, B is defensible but M5 (ForgeModel) almost certainly drops from draft-2026.

**Draft-day UI (G6 / OQ-3).** Options: TUI · local web page · Claude artifact · spreadsheet export.
**Recommendation: local web page**, server-rendered, auto-refresh every ~5 s (or htmx polling), one view: best-available table by VORP with tier/cliff/run/survival columns, filterable by position. Least UI code, readable on a second monitor next to the Sleeper draft room, and maps directly onto the eventual Spring Boot surface. TUI is a close second; spreadsheet export is the fallback if M4 slips.

**DEF depth (doc OD-4).** Season-average streaming rank for 2026. Vegas-bracket modeling is an M7/M8 enhancement — DEF is pick 15 of 15.

**Ensemble defaults (doc OD-3).** Ship fitted weights; expose λ per position as an override. Decide after seeing M2 numbers.

---

## 4. Jira scope (no tickets drafted yet)

**Project setup suggestions**

- **Key:** depends on final name (OQ-4). `LS` if the repo name stands; `DW` if Draftworks. Pick before creating the project — the key propagates into commit subjects and release notes (`<KEY>-N: Fix ...`).
- **Type scheme:** Epic → Story/Task, plus Bug. Sub-tasks not needed at this scale.
- **Fix versions** (mirror the milestones): `draft-2026` (M0–M6), `season-2026` (M7), `1.0-production` (M8).
- **Components or labels** by pipeline layer: `ingestion`, `scoring`, `benchmark`, `model`, `board`, `companion`, `in-season`, `infra`.
- **Priority:** M4 critical path = Highest; M5 = Medium; M7/M8 = Low until draft is done.
- Record resolutions of the open questions somewhere durable (a "Decisions" section in the plan doc, or Confluence).

**Epics and indicative story breakdown** (~35–40 stories for `draft-2026`; M7/M8 scoped later)

| Epic | Fix version | Stories (indicative) |
| --- | --- | --- |
| **E1 Repo & tooling bootstrap** | draft-2026 | Stack decision recorded in CLAUDE.md · project skeleton + deps · CI workflow (lint/test) · PATCH ruleset with required check after first green run · README/CLAUDE.md update · snapshot archive backup step |
| **E2 Ingestion & snapshot archive** | draft-2026 | Sleeper documented API client (league, users, rosters, draft, picks, players, trending) · Sleeper projections/ADP fetcher (season + week) · ESPN kona fetcher + stat-id decoder · nflverse release loader (stats, snaps, ff_opportunity, crosswalk) · dated immutable snapshot writer · schema validation + last-good fallback · retire `data_pull_script.ps1` |
| **E3 Scoring engine & join spine** | draft-2026 | Scoring engine from `scoring_settings` (QB/RB/WR/TE/K/DEF) · parity test fixture vs nflverse 2025 · K distance-mix approximation · crosswalk join on sleeper_id / sportradar_id verify · ESPN DST join spike · DEF derivation (streaming rank v1) |
| **E4 Benchmark scoreboard** | draft-2026 | Score Sleeper/ESPN/naive 2024–25 season · weekly horizon · MAE + rank corr by position report · inverse-error blend weight fitting · weights committed as data |
| **E5 2026 draft board** | draft-2026 | 2026 replacement baselines (2023–25 avg + 2026 proj) · flex-aware VORP · tier boundaries + cliff flags · ADP delta / value fallers · Sleeper-vs-ESPN disagreement flags · daily regen script + output (CSV + HTML) |
| **E6 Live draft companion** | draft-2026 | Draft/pick poller w/ backoff · draft-state model (rosters, needs, slot) · survival probability (slot-param) · positional run detection · recompute loop under 120 s budget · decision surface UI (per OQ-3) · mock-draft dry run · recorded-picks replay fixture · draft-order ingestion once assigned |
| **E7 ForgeModel v0** | draft-2026 (stretch) | Per-game rate EWMA features · shrinkage/regression · TD regression · modifiers (age, team change, depth) · durability/games projection · 3 tuning knobs · backtest harness vs E4 · `ForgeModelProvider` in ensemble · rookie fallback |
| **E8 In-season assistant** | season-2026 | RoS VORP for add/drop pairs · FAAB suggestion · weekly floor/ceiling start-sit · usage-trend alerts · Tue/Thu/Sun schedule · Resend digest delivery · minimum host (OQ-9) |
| **E9 Productionization** | 1.0-production | Placeholder; stories after spec freeze (Spring Boot service, providers, store, schedulers, homelab deploy, migrate from prototype) |

---

## 5. Open questions

**Blocking M0/M1 — RESOLVED 2026-08-16 (see §7):**

1. ~~OQ-1 — Where is the prototype code?~~ **Re-implement in-repo from the spec.** Chat-session artifacts are structure only, not source.
2. ~~OQ-2 — Stack for M1–M7.~~ **Python**, with a relational store for raw + parsed data and a FastAPI layer serving whatever the frontend becomes.
3. ~~OQ-3 — Draft-day UI form.~~ **Flutter** (desktop + web + mobile from one codebase; aligns with an upcoming org Flutter project). Multi-device data parity required, scoped to single-user + free tiers (e.g. Supabase) for now.
4. ~~OQ-4 — Project name.~~ **Lazy Sleeper**, Jira key **`LS`**. Repo name stands; "Draftworks" retired.
5. ~~OQ-5 — Snapshot archive home.~~ **Nothing is backed up yet.** Backup is now an M0 day-one action (§7.4).

**Shaping scope:**

6. **OQ-6 — Hours budget to Sept 4.** Weekends only, or evenings too? Decides whether M5 (ForgeModel) is realistically in for draft night or is a post-draft item that still ships for the season.
7. **OQ-7 — Draft order.** When does Sleeper assign it (commissioner action vs randomized at a set time)? Survival model is slot-agnostic until then, but the companion needs it loaded before 8 PM.
8. **OQ-8 — Mock-draft mechanism.** Will you run a Sleeper mock draft in the days before so we can capture a live draft id and picks feed for the dry run? (Alternative: replay fixture only.)
9. **OQ-9 — In-season host.** Where does the Tue/Thu/Sun scheduler run pre-M8: homelab cron/CronJob, or laptop? Determines whether E8 needs a container image early.
10. **OQ-10 — forge-digest Resend rails.** What exactly is reusable — an existing service, a shared API key/domain, a template? Is the key already in a secret store we can reference?
11. **OQ-11 — CI approach for a non-TS stack.** Org has no Python/Java reusable workflow. Hand-roll in-repo (recommended now), or add a reusable `ci-python.yml` to `tkforgeworks/.github` as a side quest later?
12. **OQ-12 — Jira conventions.** Fix versions + components as suggested? Sprints (weekly, aligned to milestones) or Kanban? Do the plan doc + this analysis also live in Confluence, or is `docs/` the source of truth?

**Deferred (fine to leave open past M0):** ensemble λ overrides (after M2 numbers) · DEF depth (streaming rank for 2026) · M8 stack details (store, scheduler, hosting).

---

## 6. Immediate next actions

Superseded by §7.5.

---

## 7. Decisions log & revised plan (2026-08-16, second pass)

### 7.1 Decisions taken

| Decision | Choice | Consequence |
| --- | --- | --- |
| Prototype code | Re-implement in repo | M1 is a from-spec build; parity fixture (nflverse 2025 weekly) is the acceptance test |
| Backend stack | **Python** | pandas for benchmark/model work; FastAPI as the API surface; scoring/VORP/model logic lives here and nowhere else |
| Data store | **Relational DB** for raw dumps + parsed data | Postgres (see 7.2). Raw JSON is no longer the system of record for parsed data — the DB is |
| Frontend | **Flutter** (desktop + web + mobile) | Backend is API-first from day one; Flutter is a pure client of FastAPI. Reused for the upcoming org Flutter project |
| Multi-device parity | Required, single-user, free tiers OK | One hosted DB + one hosted/exposed API = parity by construction. Supabase free tier as the DB host |
| Name / Jira | Lazy Sleeper, key `LS` | Commit subjects `LS-N: ...`; release-notes workflow gets `ticket-prefix: LS` |
| Backups | None exist | Day-one action, before any other M0 work |

### 7.2 Revised architecture

```
[Ingestion — Python jobs]        [Store]                              [Engines — Python]        [API]        [Clients]
Sleeper league/draft/rosters →   raw.*   snapshot rows (metadata +    Scoring engine       →   FastAPI  →   Flutter app
Sleeper players (daily)          gz JSON pointer) — immutable         VORP / tiers / market                 (desktop, web,
Sleeper projections + ADP        core.*  parsed, normalized stats,    Survival model                        mobile)
ESPN kona                        players, projections, ADP            ForgeModel + Ensemble
nflverse historicals             derived.* boards, benchmark results  Backtest harness                      (draft-night
                                 ─────────────────────────────                                              fallback: FastAPI
                                 Postgres @ Supabase (free tier)                                             HTML page)
                                 Raw payloads @ Supabase Storage / local
```

**Storage sizing (drives the raw/parsed split).** One pull day = ~118 MB raw JSON (16 MB gzipped); ESPN kona alone is 10–17 MB per season file. Daily 2026 pulls (Sleeper players + season proj + ESPN 2026) ≈ 30 MB/day raw. Supabase free tier: 500 MB Postgres, 1 GB Storage, project pauses after 7 days idle. Therefore:

- **Raw payloads do NOT go into Postgres as JSONB.** They go gzipped to Supabase Storage (≈4 MB/day → whole season fits in ~1 GB with room) *and* stay on local disk. `raw.snapshots` in Postgres holds only metadata: source, kind, season, week, pulled_at, sha256, storage path, row count, schema-version, validation status.
- **Parsed tables** (`core.players`, `core.projections`, `core.actuals`, `core.adp`, `core.crosswalk`, `core.draft_picks`, …) are narrow and small — a few thousand rows per snapshot — well inside 500 MB for the season.
- Alembic migrations, SQLAlchemy models. Local dev: Docker Postgres with the same schema, or point straight at Supabase (single-user, fine).
- Free-tier idle pause is a real risk on draft night — mitigate with the daily regen job (keeps it warm) and a pre-draft health check.

**Backend layout (single Python package, `backend/` or repo root — see OQ-13):**

```
lazy_sleeper/
  ingest/       clients (sleeper, espn, nflverse), snapshot writer, schema validation
  db/           SQLAlchemy models, Alembic, session
  scoring/      engine from scoring_settings map, K distance mix, DEF derivation
  metrics/      VORP, tiers, ADP delta, disagreement, survival, run detection
  providers/    ProjectionProvider interface: Sleeper, ESPN, ForgeModel, Ensemble
  model/        ForgeModel v0 + backtest harness
  benchmark/    scoreboard, weight fitting
  api/          FastAPI app: board, draft state, players, in-season endpoints
  jobs/         CLI entrypoints: pull, regen-board, poll-draft, waiver-run
tests/          parity fixtures, unit tests
```

**API-first contract.** FastAPI endpoints (`/board`, `/draft/{id}/state`, `/players/{id}`, `/waivers/week/{n}`, …) are the interface Flutter consumes. OpenAPI spec is generated; Flutter client can be generated from it. This is also what makes the draft-night fallback cheap.

### 7.3 Frontend & parity — tradeoffs to be aware of

**Flutter for draft night is the risk in this plan.** It's a first Flutter project, on a 19-day clock, alongside the backend build. Options:

| Option | Pros | Cons |
| --- | --- | --- |
| **A. Flutter is the target; FastAPI serves a minimal HTML/htmx page as the draft-night fallback** | Draft night is protected regardless of Flutter progress; fallback costs ~1 evening; API contract is exercised early | Some throwaway UI code |
| B. Flutter only, no fallback | No wasted work | If Flutter slips, draft night = Swagger UI or a CSV |
| C. Flutter deferred entirely to in-season | Zero frontend risk on the clock | Draft night on the fallback page by design; Flutter learning starts later |

**Recommendation: A.** Start the Flutter app as a read-only board/companion view against the live API as soon as `/board` exists (~M3). If it's usable by Sept 3, use it; if not, the HTML page is there.

**Reaching the API from the phone.** Parity requires the API to be reachable from every device, not just the DB.

| Option | Pros | Cons |
| --- | --- | --- |
| **Tailscale** (API on laptop/homelab, tailnet only) | 15-minute setup, private, no auth needed for single-user, free | Devices must be on the tailnet; not shareable |
| Cloudflare Tunnel + Access | Public URL, homelab-hosted, free; Access gates it to your email | Slightly more setup; needs Access policy before exposing |
| Cloud host (Fly.io / Render free tier) | Always on, no homelab dependency | Free tiers sleep/cold-start; another platform to manage |
| Flutter → Supabase directly (PostgREST) for reads | No API hosting for reads | Splits logic between Python and DB views; undermines API-first |

**Recommendation:** Tailscale for draft night (single user, minimum moving parts). Cloudflare Tunnel + Access for in-season/M8 when the API moves to the homelab permanently. Do not go PostgREST-direct.

**Multi-device state.** Single-user, so "parity" = every client reads the same API. Client-side prefs (e.g. draft slot, watch-list, knob overrides) should be stored server-side (`user_prefs` table) so they follow you between phone and desktop.

### 7.4 Backup — day-one action

Nothing is backed up. Before anything else in M0:

1. Copy `data_pulls/ff-projections-2026-08-16.zip` (16 MB) to at least one off-machine location (Google Drive / NAS / Supabase Storage bucket once created).
2. Re-run the pull script today for a second local copy in case of transient corruption? No — the 08-16 vintage is what matters; just protect the existing zip.
3. M0's ingestion job then uploads raw payloads to Supabase Storage on every pull, so backup becomes automatic.

### 7.5 Revised M0 (bootstrap) — concrete scope

1. **Backup the 08-16 zip** (7.4).
2. Supabase project `lazy-sleeper` (free tier): Postgres + Storage bucket `raw-snapshots`. Connection string + service key into a local `.env` (gitignored); document in README.
3. Python project: `pyproject.toml` (uv or poetry), `lazy_sleeper/` skeleton per 7.2, ruff + pytest, Alembic initial migration (`raw.snapshots`, `core.players`, `core.crosswalk`).
4. `ingest/`: Sleeper (documented endpoints + projections/ADP), ESPN kona, nflverse loader; snapshot writer (gz → Storage + local, metadata row); schema validation stub per source. Backfill the 08-16 archive as the first snapshots. Retire `data_pull_script.ps1`.
5. GitHub Actions `ci.yml` (ruff + pytest) hand-rolled — no org Python template exists. After first green run on a PR, PATCH the ruleset to require it.
6. `.claude/CLAUDE.md` + README updated with stack, layout, milestones, `LS-N` commit convention.
7. Jira: fix versions `draft-2026` / `season-2026` / `1.0-production`, components per §4; draft E1–E3 stories first.

Flutter app repo/skeleton starts in parallel once `/board` exists (M3), not in M0.

### 7.6 Further decisions (2026-08-16, third pass)

| # | Decision |
| --- | --- |
| OQ-13 | **Two repos.** `lazy-sleeper` = Python backend (API, engines, ingestion, fallback page). `lazy-sleeper-app` = Flutter client, never touches logic. Separate CI per repo. |
| OQ-14 | **Draft-night fallback page: yes**, served from the backend repo only. |
| OQ-15 | **Tailscale** as the short-term remote-access solution. Cloudflare Tunnel + Access later. |
| OQ-16 | **Existing Supabase account.** Free tier is the plan; **Pro ($25/mo) is an accepted fallback** if quotas or features demand it (other org projects may need Pro anyway). |
| OQ-17 | **Docker Postgres for local dev.** Schema/migrations must stay Supabase-portable (plain Postgres, no local-only extensions); migration to Supabase is a config change, not a code change. |

### 7.7 Remaining open questions (non-blocking)

Carried from §5: OQ-6 hours budget, OQ-7 draft order timing, OQ-8 mock draft, OQ-9 in-season host, OQ-10 forge-digest Resend reuse, OQ-12 Jira conventions (sprints vs Kanban).

---

## 8. Jira key map (created 2026-08-16)

Project **LS** · fix versions `0.1.0` = draft-2026, `0.2.0` = season-2026, `1.0.0` = production · layers as labels.

| Epic | Key | Stories |
| --- | --- | --- |
| E1 Repo & tooling bootstrap (remaining) | LS-1 | LS-10 ruleset PATCH · LS-11 Supabase bucket + docs · LS-12 `ls sync` · LS-13 backup 08-16 archive |
| E2 Ingestion & snapshot archive | LS-2 | LS-14 projections/ADP → core · LS-15 nflverse actuals/snaps/kicking/xFP → core · LS-16 draft picks/rosters → core · LS-17 daily pull scheduler |
| E3 Scoring engine & join spine | LS-3 | LS-18 QB/RB/WR/TE scoring · LS-19 K distance mix · LS-20 DEF scoring + streaming rank · LS-21 parity test · LS-22 crosswalk/DST join verify |
| E4 Benchmark scoreboard | LS-4 | LS-23 season scoreboard · LS-24 weekly scoreboard · LS-25 blend weights + provider abstraction |
| E5 2026 draft board | LS-5 | LS-26 baselines · LS-27 flex-aware VORP · LS-28 tiers/cliffs · LS-29 ADP delta/disagreement · LS-30 `/board` + daily regen |
| E6 Live draft companion | LS-6 | LS-31 pick poller · LS-32 draft-state model · LS-33 survival + runs · LS-34 recompute ≤120 s · LS-35 `/draft/{id}/state` · LS-36 mock-draft dry run + replay · LS-37 HTML fallback · LS-38 Tailscale · LS-39 Flutter read-only view |
| E7 ForgeModel v0 | LS-7 | LS-40 EWMA/shrinkage · LS-41 TD regression + modifiers · LS-42 durability + knobs · LS-43 backtest + ensemble |
| E8 In-season assistant (0.2.0) | LS-8 | LS-44 RoS waivers · LS-45 FAAB · LS-46 floor/ceiling · LS-47 usage alerts · LS-48 wk 15–17 weighting · LS-49 scheduler · LS-50 Resend digests |
| E9 Productionization (1.0.0) | LS-9 | — |

Suggested order: LS-13 → LS-11/12 → LS-14/15 → LS-18–22 → LS-23–25 → LS-26–30 → LS-16 → LS-31–38 → LS-40–43 (fill-in) → LS-39.
