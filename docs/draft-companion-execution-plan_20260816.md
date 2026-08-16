# Lazy Sleeper - Draft Companion Execution Plan

**Project:** Sleeper fantasy football draft & season companion (tkforgeworks)
**Working name:** TBD — *Draftworks?* (fits the Anvil / COG / Budgetworks convention)
**Version:** 0.1 · 2026-08-16 · Owner: Tim (tkm3d1a)
**Status:** Data layer validated end-to-end. Next: benchmark scoreboard (Phase 1).
**Hard deadline:** Live draft **Friday, Sept 4, 2026, 8:00 PM ET** (19 days from v0.1).

---

## 1. Mission

Build a draft-day companion and in-season roster assistant for a 12-team Sleeper league that answers, at every decision point, **"who should I take right now, and why?"** — grounded in the league's exact scoring, live draft state, and a tunable projection engine that differs (correctly) from the consensus board every leaguemate is staring at.

Explicitly *not* a static cheat sheet: the tool consumes live draft state, recomputes value in seconds, and continues working through the season for waivers and start/sit.

## 2. Product shape

Three deliverables sharing one pipeline:

1. **2026 Draft board (pre-draft)** — ranked player pool under league scoring: VORP, tiers, ADP, disagreement flags. Regenerated daily as projections and injuries move.
2. **Live draft companion (Sept 4)** — polls the draft, maintains the board, and surfaces: best available by VORP, tier cliffs, positional runs, ADP-value fallers, and survival probability to the user's next pick. Must recompute within the 120-second pick timer with time to spare.
3. **In-season assistant** — weekly waiver rankings (RoS VORP + FAAB bid suggestion from the $100 budget), start/sit with floor/ceiling, and usage-trend alerts. Delivery can ride the existing forge-digest Resend rails.

**Differentiator:** an in-house projection model ("ForgeModel") whose value is tuneability and *calibrated disagreement* with the public board — not raw forecast supremacy. Consensus feeds remain in the system as benchmark and blend anchor.

**End state:** once the spec stabilizes through these prototypes, productionize as a proper application via Claude Code (Java/Spring Boot service + scheduled ingestion; UI form TBD — see Open Decisions).

## 3. League parameters ("The League", 2026)

| Parameter | Value |
| --- | --- |
| League / Draft / User IDs | `1392685475625443328` / `1392685476523024384` / `1268591266036203520` |
| Format | 12-team redraft, full PPR (`rec: 1.0`), no TE premium, no yardage bonuses |
| Lineup | QB, 2 RB, 2 WR, TE, **2 FLEX**, K, DEF + 5 BN (15 rounds × 12 = 180 drafted) |
| QB scoring | 4-pt pass TD, 0.04/pass yd, −1 INT; rushing scores fully (mobile-QB boost) |
| Turnovers | −2 fumble lost |
| K | Distance-scaled FG (3/3/3/4/5/6 by bucket), −1 misses, ±1 XP |
| DEF | Standard categories + bracketed points-allowed (10 at shutout → −4 at 35+) |
| Draft | Snake, 120-sec timer, CPU autopick on, order **not yet assigned** |
| Season | FAAB $100 (min $0) weekly waivers, trade deadline wk 11, playoffs 6 teams wks 15-17, weekly lineups |

**Implications baked into the design:** ~130 of 180 picks will be RB/WR (deep replacement level); QB and TE are ~12-14 picks each (wait-and-pounce / elite-or-punt); K/DEF are the last two picks; flex demand skews WR in full PPR (2025 actuals: flex filled 17 WR / 5 RB / 2 TE).

## 4. Architecture

```
[Ingestion jobs]                [Store]              [Engines]                 [Surfaces]
Sleeper league/draft/rosters →  dated snapshots  →   Scoring engine        →   Draft board
Sleeper players (daily)         (JSON archive,       (league map × stats)      Live companion
Sleeper projections + ADP        never revised)      VORP / tiers / market     In-season digest
ESPN kona (proj + actuals)                           Survival model
nflverse historicals                                 ForgeModel + Ensemble
```

Core principles established:

- **Stat-level everything.** Never ingest pre-scored fantasy points; every projection and actual runs through the scoring engine parameterized by the league's literal `scoring_settings` map. Correct for any league, robust to rule changes.
- **Pluggable projections.** `ProjectionProvider` interface: `SleeperProvider`, `EspnProvider`, `ForgeModelProvider`, composed by an `EnsembleProvider` with per-position weights (fitted from backtests, adjustable by hand).
- **Join spine = dynastyprocess crosswalk**, keyed on `sleeper_id`, with `sportradar_id` as the universal verification key. Sleeper's own `espn/yahoo/gsis` foreign keys are sparsely populated (17-24% fill) — never rely on them.
- **Snapshot archive.** Every external pull is saved dated and immutable (players, projections, ADP). Providers can silently revise stored data; the only projection archive fully trusted is the one we build. First snapshots: players 2026-08-16, projections 2026-08-16.
- **Projections ≠ market.** Projections (what a player will score) feed VORP; market data (what drafters will do — Sleeper ADP + `search_rank`) feeds survival probability. Edge = the delta between them.

## 5. Data source registry

| Source | Role | Access | Status | Risk / mitigation |
| --- | --- | --- | --- | --- |
| Sleeper documented API | League config, rosters, live draft picks, transactions, trending | Public, no auth, ~1000 calls/min | ✅ Validated 08-16 | Low |
| Sleeper players (`?active=true` incl. 32 DEF) | ID spine, injury/depth/`search_rank`/`team_changed_at` metadata | Daily pull, cached | ✅ Validated 08-16 (9,412 entries) | Low |
| Sleeper projections (undocumented `api.sleeper.com/projections/...`) | Primary consensus feed: stat-level season + weekly, **plus platform ADP** (`adp_ppr` et al.); history ≥2024 confirmed | No auth; season/week parameterized | ✅ Validated 08-16, preseason vintage confirmed | Undocumented → schema validation on ingest + last-good fallback |
| ESPN kona (undocumented, `X-Fantasy-Filter` header) | Second consensus feed: seasonal + weekly projections *and* actuals per payload; ownership %, ESPN ranks | No auth; numeric stat IDs (decoder table in nntrn gist) | ✅ Validated 08-16, preseason vintage confirmed (~1,050 players/season) | Undocumented; DST entries lack crosswalk ids (join players only — confirm in Phase 1) |
| nflverse (`stats_player_week`, `snap_counts`, dynastyprocess `playerids`) | Historical source of record 2023-25+ (weekly stats, usage, kicker distance splits); crosswalk | GitHub releases (container-allowlisted) | ✅ Validated 08-16 (100% sleeper_id match on top-300) | Low; reconciled vs official gamebooks |
| nflverse `ff_opportunity` (xFP) | Extra benchmark + ForgeModel feature source | GitHub releases | ☐ Pull in Phase 2 | Low |
| Boris Chen tiers / FantasyPros ECR | Tier boundaries, market consensus (secondary) | Free text files / gated | ☐ Optional, Phase 3 | Scrape fragility → optional |
| ESPN core API (odds, injuries, news-by-player — per nntrn gist) | In-season matchup context (implied totals) | No auth | ☐ Phase 5 | Undocumented |
| Sportradar | **Not used** as a data source; `sportradar_id` is a join key only | Trial = 30-day eval, non-commercial ToS; production = enterprise | ✖ Rejected | Trial expiry time bomb |

## 6. Scoring engine

Contract: raw stat lines in → league points out. Fields required per position: QB (pass_yd/td/int/2pt + rush + fum_lost), RB/WR/TE (rec/rec_yd/rec_td + rush + 2pts + fum_lost + st_td), K (FG made/missed **by distance bucket**, XP made/missed), DEF (sacks, INT, FR, TDs, safeties, blocks, points-allowed distribution).

- **Parity check (done):** engine vs nflverse `fantasy_points_ppr` over 6,037 weekly 2025 rows — mean |Δ| 0.03 pts after the two documented rule differences (INT −1 vs −2; ST TDs). Engine is exact.
- **Kicker distance gap:** projections provide total FGs only → apply expected distance mix from 2023-25 league-wide distributions (data in hand). Low stakes; accepted approximation.
- **DEF derivation (open):** team-level aggregation + game scores for points-allowed brackets, buildable from nflverse. Scheduled Phase 3; DEF is drafted last and streamed, so precision is secondary.

## 7. Derived metrics

**Draft:** flex-aware VORP (2025 actual baselines: QB12 ≈ 283 pts, RB29 / WR41 / TE14 all ≈ 146-147 — recompute on 2023-25 average and again on 2026 projections); tier boundaries + "tier cliff" warnings; ADP delta (value fallers); positional run detection; survival probability to next pick from Sleeper ADP + `search_rank` + opposing roster needs; Sleeper-vs-ESPN-vs-ForgeModel disagreement flags (volatility markers).

**In-season:** rest-of-season VORP for every add/drop pair; FAAB bid suggestion; weekly floor/ceiling for start/sit; usage-trend alerts (snap %, target share move ~1-2 weeks ahead of points); weeks 15-17 matchup weighting late season.

## 8. ForgeModel v0 (the in-house projection engine)

Design: **opportunity × efficiency decomposition** — project volume (targets, carries, attempts; sticky) separately from efficiency (yards/target, TD rates; regresses hard). Components: multi-year EWMA of per-game rates; shrinkage to positional mean scaled by sample size; TD regression via yardage + red-zone usage; modifiers for age curve, team change (`team_changed_at`), depth-chart order; durability-adjusted games-played projection.

Exposed tuning knobs (v0): **(1)** recency weights (e.g., 55/30/15 across 2025/24/23), **(2)** regression-to-mean strength, **(3)** durability/games assumption. Knob defaults get fitted by backtest, then remain hand-adjustable.

Known blind spot: **rookies** (no NFL history) → ensemble falls back to consensus for them; draft-capital priors are a future extension.

## 9. Benchmarking & ensemble methodology

- **Vintage validated (08-16):** stored 2025 season projections are true preseason forecasts — e.g., Kyler Murray held at 307-314 projected despite a 5-game season in both sources; correlations vs actuals 0.71 (Sleeper) / 0.86 (ESPN), preseason-typical.
- **Benchmark data in hand:** Sleeper season + weekly projections 2024-25 (36 weekly files), ESPN seasonal + weekly projections/actuals 2024-25, nflverse actuals 2023-25 — all scoreable under league rules.
- **Metrics:** MAE and rank correlation, by position, in league scoring; season-long and weekly horizons.
- **Opponents:** naive last-year baseline, Sleeper, ESPN, xFP, ForgeModel.
- **Ensemble weights:** fitted per-position from historical error (inverse-error weighting); λ remains a manual override. "Is my model adding anything?" becomes a measured, per-position answer.

## 10. Execution phases

| Phase | Deliverable | Status / target |
| --- | --- | --- |
| 0. Data validation | Sources proven, joins verified, scoring engine exact, projections + ADP + history in hand | ✅ Done 08-16 |
| 1. Benchmark scoreboard | 2024-25 accuracy of Sleeper vs ESPN vs naive, by position, in league scoring; fitted blend weights | Next session |
| 2. ForgeModel v0 + backtest harness | Own projections for 2026 + accuracy scorecard vs Phase-1 board; knobs exposed | ~Aug 22 |
| 3. 2026 draft board | Ensemble VORP board + tiers + ADP + disagreement flags; DEF derivation; daily regen script | ~Aug 26 |
| 4. Draft-day companion MVP | Live pick polling + recompute loop + decision surface (UI form TBD); dry-run vs a mock draft | **Sept 1-3, hard stop Sept 4** |
| 5. In-season mode | Tue waiver run, Thu/Sun lineup run, digest delivery via Resend rails | Weeks 1-2 of season |
| 6. Productionization | Claude Code build: Spring Boot service, providers, persistent store, schedulers; spec from this doc + prototypes | Post-draft / in-season |

## 11. Risks & mitigations

1. **Undocumented endpoints change or vanish** → schema validation on every ingest; dated snapshots mean the tool degrades to last-good rather than breaking; nflverse + documented Sleeper API are stable floors.
2. **Draft-order unknown until assigned** → survival model parameterized by slot; board works slot-agnostic until then.
3. **Rookie projection blindness** → consensus fallback in ensemble (accepted for 2026).
4. **Time to Sept 4** → phases ordered so a usable board exists even if Phase 4 slips to "board + manual polling"; companion MVP scope kept minimal (decision surface, not a pretty app).
5. **ESPN DST join gap** → confirm in Phase 1; worst case DEF market data comes from Sleeper only (sufficient).

## 12. Open decisions (Tim)

1. **Draft-day UI form:** terminal/TUI vs local web page vs Claude-built artifact vs spreadsheet export. Drives Phase 4 scope.
2. **Project name** (Draftworks is a placeholder).
3. **Ensemble defaults:** accept fitted weights as-shipped, or hand-set λ per position after seeing Phase 1/2 results.
4. **DEF modeling depth:** brackets-from-Vegas-totals (better) vs season-average streaming rank (simpler).
5. **Production stack details** for Phase 6 (store, scheduler, hosting on homelab) — defer until spec freeze.

## Appendix A — Artifacts produced to date

`2025_rankings_league_scoring.csv` (top 350 under league scoring, VORP, ID crosswalk) · `2025_rankings_enriched_live.csv` (+ live team/injury/depth/`search_rank`) · this document. Local data archive: Sleeper players 2026-08-16; Sleeper projections 2024/25/26 season + 2024-25 weekly (36 files); ESPN kona 2024/25/26; nflverse 2023-25 weekly stats, kicking, snap counts, crosswalk.

## Appendix B — Endpoint quick reference

Documented Sleeper: `api.sleeper.app/v1/` — user, league, rosters, users, matchups, transactions, **draft + `/draft/{id}/picks`** (live poll), players, trending. Undocumented Sleeper: `api.sleeper.com/projections/nfl/{yr}[/{wk}]?season_type=regular&position[]=...` (stats variant: swap `projections`→`stats`); payload includes `adp_*` fields and `gp`. ESPN: `lm-api-reads.fantasy.espn.com/.../seasons/{yr}/segments/0/leaguedefaults/3?view=kona_player_info` + `X-Fantasy-Filter` header; decode stat IDs per nntrn gist; `statSourceId` 0=actual/1=projected, `statSplitTypeId` 0=season/1=week. nflverse: GitHub releases `stats_player`, `snap_counts`, `ff_opportunity`; crosswalk at dynastyprocess `db_playerids.csv`.
