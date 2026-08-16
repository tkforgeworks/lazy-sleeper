# lazy-sleeper

A draft and in-season NFL fantasy league helper app centered around leagues from [Sleeper](https://sleeper.com).

## Status

Greenfield — architecture is still being worked out. No application code yet.

## Repo layout

- `data_pulls/` — `data_pull_script.ps1` fetches Sleeper + ESPN projection data for benchmarking. Its output (`ff-projections-<date>/`, `*.zip`) is gitignored.

## Contributing / branch policy

`main` is protected by the TK ForgeWorks standard repository ruleset (see [`tkforgeworks/.github/docs/branch-protection-ruleset.md`](https://github.com/tkforgeworks/.github/blob/main/docs/branch-protection-ruleset.md)): no direct pushes, no force-push/delete, all changes via PR, no bypass. A required CI status check will be added to the ruleset once CI exists.
