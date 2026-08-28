# Changelog

## Unreleased — 0.1.2

- **The API no longer hangs for minutes on a stale database connection after idle** — pooled
  connections are recycled every 5 min, kept alive with TCP keepalives, and bounded by connect
  (10 s) and statement (30 s) timeouts (`DB_*` settings); an unreachable database answers **503**
  `database unavailable: …` from `POST /draft/{id}/start`, `GET /board` and `/board/config`
  instead of blocking, and one slow start no longer serializes starts for other drafts (LS-69).
- **A runner started for a nonexistent draft no longer retries forever or blocks shutdown** — a
  Sleeper 404 for the draft is retried once and then stops the runner (`running: false`,
  `poller.runner_error` says why); transient errors keep the capped backoff. `lazy serve` stops
  every runner from the lifespan shutdown and caps in-flight requests at 4 s, so Ctrl-C finishes
  in seconds (LS-70).
- **Draft state carries the pick clock, the on-the-clock team's name, and a recent-pick feed** —
  `clock.pick_deadline` / `pick_timer_s` (derived server-side from Sleeper's timer and when the
  current pick started; stable within a pick so a client ticks it locally), `clock.
  on_the_clock_team_name` (from `draft_order` + league users), and `recent_picks` (last 8
  league-wide, most recent first). The fallback page shows the countdown, the team and the feed
  (LS-56).

## v0.1.1 — draft-2026 fixes (2026-08-28)

The six bugs from the 2026-08-26 architecture review of 0.1.0, all on the live-draft path.

- **Draft advice keeps flowing during a database outage** — snapshots and `core.draft_picks` are
  written by a background `Persister` that retries and catches up; the poll thread only talks to
  Sleeper and diffs against its own last payload. `/state.poller` reports the writer (LS-62).
- **Same-window undo-and-repick no longer leaves the board showing the wrong players** — the pick
  diff is keyed on `(pick_no, player)` and a re-delivered pick replaces, never double-seats (LS-66).
- **A consumer error during a poll no longer silently kills the draft runner** — `on_poll` is
  guarded, a failed rebuild is retried on the next changed poll, and a runner that does die is
  reported as `runner_error` in `/state` and on the page (LS-64).
- **Draft polling recovers from network blips in seconds instead of minutes** — its own 5 s,
  no-retry HTTP client (`DRAFT_HTTP_TIMEOUT_S`), a 15 s backoff cap (`DRAFT_MAX_BACKOFF_S`), and an
  interval measured poll-start to poll-start (LS-65).
- **The draft page surfaces poller stalls and restarts instead of freezing on stale data** — clock,
  status and banner redraw every tick; the table gate only moves forward and resets per runner; one
  request in flight at a time (LS-63).
- **Mobile hardening ahead of the real-device pass** — table in its own scroll box, sticky header
  row, thumb-sized filter buttons, immediate poll on wake; firewall / network-profile / auto-lock
  notes in the runbook; verified end-to-end on desktop Chrome and a phone over Wi-Fi (LS-67).

## v0.1.0 — draft-2026 (2026-08-26)

Everything needed to draft on 2026-09-04: league-exact scoring, a benchmarked consensus board,
and a live draft companion that recomputes advice on every pick. Built 2026-08-16 → 2026-08-23.

### Live draft companion (E6 — M4)

- **`lazy serve`** — the draft-night command: runs the API and prints the exact URLs for the
  app, the phone, and every page below.
- **`GET /draft/{id}/state.html`** — self-contained live page (no build step): clock strip with
  on-the-clock / until-my-turn, my needs and picks, best-available table by pick score with
  tier / cliff / run / survival / injury columns, position filters, ~2 s refresh (LS-37).
- **`GET /draft/{id}/state`** — the typed decision document behind it; OpenAPI-documented for
  client generation, with `docs/api/` as the committed hand-off contract (LS-35).
- **Recompute engine** — board built once pre-draft; advice recomputed on every pick in ~50 ms
  (bound: <10 s avg / <30 s worst inside the 120 s pick timer); failures fall back to the
  last-good board with an error flag (LS-34).
- **Pick poller** — snapshots `/draft/{id}/picks` every 2 s with capped backoff; survives
  errors, commissioner undo, and mid-draft `draft_order` assignment (LS-31).
- **Draft state** — per-team rosters seated greedily (dedicated → FLEX → bench), open seats,
  positional needs, snake math: who's on the clock, my next pick, the window before it (LS-32).
- **Signals** — survival probability (ADP + search-rank pseudo-ADP, demand-stretched), position
  run detection, and pick score = VORP − option value + need bonus; waiver-aware K/DEF (LS-33).
- **Tuning page** `GET /board/config.html` — every dial editable mid-draft; signal dials apply
  instantly, board-time dials via a one-click runner restart (LS-36).
- **Rehearsal kit** — three recorded mock drafts; `lazy draft fixture` turns any polled draft
  into an offline replay fixture; two mocks replay end-to-end in CI; draft-day runbook in the
  README (LS-36).

### Consensus draft board (E5 — M3)

- Replacement baselines from the league's exact roster shape; flex-aware VORP where the last
  starter sits at zero (LS-26, LS-27).
- Adaptive tiers and cliff flags; ADP value/reach and provider-disagreement flags, debiased for
  per-position provider bias (LS-28, LS-29).
- `GET /board`, `/board.html`, `POST /board/regen`; boards persisted daily with the config that
  built them (LS-30).

### Benchmarks & ensemble (E4 — M2)

- Season and weekly scoreboards: Sleeper / ESPN / naive vs 2024–25 actuals under league scoring
  (RB/WR ρ 0.55–0.74; both providers beat naive except K) (LS-23, LS-24).
- Provider abstraction + inverse-MAE ensemble with fitted weights, manual overrides, and a
  version-pinned switchboard (`/ensemble/*`) (LS-25).

### Scoring engine & data spine (E1–E3 — M0/M1)

- Stat-level scoring from the league's literal `scoring_settings` — no hardcoded points; K
  distance-mix and DEF points-allowed normalizers; parity vs nflverse 2025: mean |Δ| < 0.01
  (LS-18–LS-21).
- Ingestion: Sleeper, ESPN, nflverse, dynastyprocess crosswalk → immutable gzip snapshots
  (mirrored to Supabase Storage) + parsed `core.*` tables; join audit 99.8–100 % resolved
  (LS-13–LS-16, LS-22).
- Daily pull on GitHub Actions, 06:00 ET (LS-11, LS-12, LS-17).

### Storage hygiene

- Byte-identical pulls dedup onto the existing snapshot (sha256); freshness unaffected (LS-52).
- `core.projections` collapsed to one current row per player/scope (−83 % rows), frozen
  pre-game once a week kicks off so benchmark inputs can't be rewritten by post-game feeds
  (LS-53).

[Full commit list](https://github.com/tkforgeworks/lazy-sleeper/commits/main) · Jira project `LS`,
fix version 0.1.0.
