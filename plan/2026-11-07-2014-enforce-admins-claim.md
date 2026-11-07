---
status: draft
issue: 2014
spec: spec/2026-11-07-2014-enforce-admins-claim.md
coders:
  1: coder   # a small text edit in one TOML file: no tricky logic for codex, too small for agy
  2: coder   # running the generator in the same session keeps step 1's context
reviewers:
  - codex    # checks that the claim text and the regenerated outputs match the measured state
  - agy      # independent second read of the same diff
---

# Plan: make the control mapping's branch-protection claim match what holds now

Summary of approved decisions (spec): a data-only change. Six controls in
`docs/compliance/control-objectives.toml` carry a branch-protection `claim`
that says `enforce_admins` is false. Since 2026-10-22 (Factory#943) it is true
on `main` in AIFactory, PFactory, TFactory, CFactory and Factory, and false on
every `dev` branch and on factory-gitops `main` (re-read 2026-11-07). The claim
is replaced with one exact new text, the comment above it is updated, and the
outputs are regenerated. No change to the generator, its `--check` or the
workflow. Driving the claim from the live API, dropping it, and a test that
pins the text were all rejected.

## Steps

1. `docs/compliance/control-objectives.toml`
   - Lines 30, 36, 42, 84, 90, 96: these are the `claim` lines of
     `policy.tier.low`, `policy.tier.medium`, `policy.tier.hard`,
     `gate.merge.merge_policy`, `gate.server.services.pr_review_service` and
     `gate.server.services.pr_endgame`. Each now reads `claim = "Enforced by
     branch protection for non-admin merges; ... (measured 2026-10-07,
     Factory#943)."`. Replace each with this single-line, double-quoted value:

     ```
     claim = "Enforced by branch protection. enforce_admins is on for main in AIFactory, PFactory, TFactory, CFactory and Factory (applied 2026-10-22, re-read 2026-11-07, Factory#943), so there the required checks bind admins too. It is off on every dev branch and on factory-gitops main, where the checks bind non-admin merges only: the fleet's merge paths do not use admin bypass, but GitHub does not prevent it."
     ```
   - Lines 22-24 (comment): replace with

     ```
     # The branch-protection `claim` comes from the step 0 measurement
     # (2026-10-07, Factory#943) as updated by #943's decision (enforce_admins on
     # main, applied 2026-10-22, re-read 2026-11-07, AIFactory#2014). It is repeated
     # on every control whose enforcement relies on required status checks (the
     # policy.tier.* and gate.* controls).
     ```
   - Leave every other `claim` alone. `wiring.live_overlay` has a different
     claim.

   → Verify: `grep -c "while enforce_admins is false" docs/compliance/control-objectives.toml`
   prints `0` (and exits 1, which is grep's status for no match),
   `grep -c "re-read 2026-11-07, Factory#943), so there" docs/compliance/control-objectives.toml`
   prints `6`, and `grep -c "AIFactory#2014" docs/compliance/control-objectives.toml`
   prints `1` (the comment).
   Traps: the value must stay on one line with no `|` (the generator puts it in
   a markdown table cell without escaping, `scripts/gen_autonomy_matrix.py:104`).
   The apostrophe in "fleet's" needs no escaping inside a TOML double-quoted
   string.

2. Regenerate `docs/docs/compliance/autonomy-matrix.md` and
   `docs/static/compliance/autonomy-matrix.json`. Never hand-edit them. From
   the worktree root, set
   `PY=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/python`
   and run `$PY scripts/gen_autonomy_matrix.py`, then
   `$PY scripts/gen_autonomy_matrix.py --check`. Use `$PY` for every run below. The worktree has no venv
   of its own, so this uses the main checkout's venv. The script resolves paths
   from its own location, so it reads the worktree's files.
   → Verify: `--check` exits 0. `git diff --stat` lists only the TOML and the
   two outputs. In the JSON, only the six `claim` values and the six matching
   `rows` cells change.
   Traps: if the shared venv lacks a dependency, create a throwaway venv the
   way CI does (`.github/workflows/autonomy-matrix.yml`: Python 3.12, backend
   and web-server requirements) in the scratchpad, and point `$PY` at it. Do not
   commit a venv.

## Tests

- `$PY scripts/gen_autonomy_matrix.py --check` → exit 0.
- `git grep -n "while enforce_admins is false" -- ':!intent' ':!spec' ':!plan'`
  → no output (grep exits 1 for no match).
- On the PR: `autonomy matrix matches the policy (--check, blocking)` is green.

## Known risks (from the spec, accepted)

- The dated claim goes stale again if `enforce_admins` changes, for example
  through the break-glass in Factory `docs/compliance/branch-protection.md`.
  The dates make that visible. Keeping it current is out of scope.
- The matching hub row, `Factory/docs/compliance/control-matrix.md:53`, is
  fixed in a separate one-line Factory PR that links this one. If Factory#324's
  open branch edits the same line, whichever merges second gets a merge
  conflict there.

## Rollback

`git revert` the implementation commit. The outputs come back with the TOML.
