---
status: approved
issue: 1588
author: Olaf Krasicki-Freund
---

# Intent: A generated file a bot cannot regenerate makes its gate red by construction

## Problem

`techdocs.yml` regenerates `docs/dependencies.md` and fails when the committed copy differs
(`techdocs.yml:97-118`). `scripts/generate-techdocs-deps.py` reads dependency **versions**
out of `apps/frontend-web/package.json`, which is also one of the workflow's trigger paths.
So a Dependabot PR that bumps a frontend dependency makes the committed file stale, and
Dependabot cannot run the generator — the gate is red the moment the PR opens, and the PR
cannot merge on its own.

**The cost is already being paid by hand.** Of the 9 open Dependabot PRs, the two that
touched `apps/frontend-web/package.json` are green only because a human pushed a
`docs(techdocs): regenerate dependencies.md…` commit onto each (#1536, #1531).

**Scope is narrower than "every npm bump"** (the issue title): the other 7 touch lock files
only. They trigger the workflow via `docs/**`, but the generator does not read those files,
so the diff is empty and the gate passes. This bites frontend dependency bumps.

A gate that is guaranteed red for a whole class of PR is the problem, not the inconvenience:
it trains everyone to ignore a red check, and it turns routine dependency maintenance into
hand work that will eventually be skipped. The fleet has one of these already
(Factory#2938, `github/codeql-action` bumps).

## Proposed outcome

A frontend dependency bump reaches green without a human regenerating anything, and what the
repo publishes as its dependency list still matches what it actually depends on. Nobody
learns to ignore this gate.

## Affected users and systems

- `.github/workflows/techdocs.yml` (the gate), `scripts/generate-techdocs-deps.py`,
  `docs/dependencies.md`
- Dependabot PRs touching `apps/frontend-web/package.json`, and whoever maintains them
- The TechDocs site that publishes the table

## Constraints

- **The published list must not become a lie.** Whatever ships, the table must either stay
  accurate or stop claiming what it no longer tracks.
- **No PR code may run with write access.** Any automation that writes back to a bot branch
  has to check out trusted workflow code, not the PR's.
- The same gate also guards `apps/web-server/openapi.yaml`. Nothing here may cause that file
  to be regenerated from a PR branch — it is the API contract (#1569 regenerated it by hand
  for exactly this reason).
- No weakening of the gate for non-bot PRs: a human who forgets to regenerate must still fail.

## Open questions

1. **Which fix?**
   - **(a) Regenerate in CI for bot PRs** and push the result back. Keeps the table accurate
     and the gate strict. The permission question has a working precedent in this repo:
     `dependabot-digest-automerge.yml` runs on `pull_request` with `contents: write` and has
     succeeded on a Dependabot PR, so a bot-triggered run here can hold a writable token.
   - **(b) Drop version numbers from the table**, listing dependency names only. The drift
     class disappears entirely, with no token and no write-back. The table stops answering
     "at what version", which is most of why someone reads it.
   - **(c) Stop committing the file** and generate it at publish time. Also removes the class,
     but the table is no longer visible in review.
2. If (a): should the write-back cover **only** `docs/dependencies.md`, leaving any
   `openapi.yaml` drift to fail as it does today? (My instinct is yes — one is derived from a
   manifest, the other is the API contract.)
