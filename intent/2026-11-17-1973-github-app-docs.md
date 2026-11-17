---
status: draft
issue: 1973
author: olafkfreund
---

# Intent: Document the GitHub App and Copilot review setup for users (#1973, #1701)

## Problem

The AIFactory GitHub App and Copilot review were set up live on the owner's deployment. What was learned is not written down where a new user looks. `docs/docs/concepts/github-app.md` already has operator setup and the one-window cutover rule, but it omits traps hit in practice: the `convert` Secret does not match the keys the deployment reads, the manifest redirect URL does not load, the install scope decides which projects can push, and the observed Copilot probe result. `getting-started.md` and the Factory hub onboarding guide never mention GitHub identity. factory-gitops `docs/aifactory-github-app-cutover.md` still reads as "nothing done yet".

## Proposed outcome

A user can follow the docs from "no App" to "first PR authored by the App", and can choose a Copilot review mode from documented evidence. The Copilot text states its test conditions and separates what was observed (App token: probe listed no Copilot; ruleset fallback in place) from what is untested (re-review on new pushes). Three places change, each linking to the one source of truth instead of copying it:

- AIFactory: update `concepts/github-app.md` (single source) and add a short GitHub step to `getting-started.md`.
- factory-gitops: update `docs/aifactory-github-app-cutover.md` with the verified sequence, the gotchas and the Copilot result.
- Factory hub: add a short step to `docs/guides/onboarding.md` that links to the AIFactory page.

## Affected users and systems

New AIFactory operators, operators of the factory-gitops deployment, readers of the Factory hub site. Docs only: no code, no cluster change.

## Constraints

- Update existing pages; do not duplicate.
- No secret values. Owner-specific ids (App id, installation id) appear only in the factory-gitops runbook, marked as this deployment's values.
- The docs include a success check: the first task's PR is authored by `app/<slug>`, and the pod has no PAT in its environment.
- Each repo's docs build or lint passes.
- PRs are opened and left for the owner to merge.

## Open questions

None. The owner approved intent, spec and plan in advance for this session (approve-unseen, 2026-11-17).
