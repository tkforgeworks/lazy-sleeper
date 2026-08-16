# lazy-sleeper — Claude reference

Draft + in-season NFL fantasy helper for Sleeper leagues. Greenfield; architecture undecided. Keep this file in sync as decisions land.

## Repo state

- No app code yet. `data_pulls/data_pull_script.ps1` pulls Sleeper (season + weekly projections, 2024–2026) and ESPN kona payloads; output is gitignored.
- `main` is protected by the org standard ruleset (`tkforgeworks/.github/docs/branch-protection-ruleset.md`): PR-only, no force-push/delete, no bypass. **No `required_status_checks` rule yet** — CI does not exist. When CI is added, PATCH the ruleset to add the check (see the org doc's "Updating an existing ruleset"), and only after the check has reported on a PR at least once.
- Org shared standards (reusable CI, release notes) live in `tkforgeworks/.github` — adopt from there rather than hand-rolling.

## Working conventions

- All changes land via PR to `main` (direct push is rejected). Branch from `main`, open PR, merge (self-merge is allowed — 0 required approvals).
- Commit subjects become release-note lines under the org release-notes standard: imperative, `Fix ...` prefix for bug fixes.
