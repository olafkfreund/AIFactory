---
status: approved
issue: 2109
author: olafkfreund
---

# Intent: Stamp a Plan-Slug trailer on factory commits

## Problem

Nothing joins a merge commit or an image-SHA bump to the plan that produced it
(Factory#1958 acceptance item 3, gap d). PFactory's `plan.governance_cli --repo`
reads a `Plan-Slug: <correlation_key>` trailer. AIFactory's
`core/commit_trailers.py` stamps Agent-Identity, Autonomy-Tier and Originator but
`requirements.json` carries no plan correlation key, so no `Plan-Slug` is written,
and the factory-gitops image bump drops any trailer.

## Proposed outcome

- A task built from a trusted PFactory plan records the plan's `correlation_key`
  in `requirements.json`.
- With `AIFACTORY_AGENT_COMMIT_TRAILERS` on, every commit the framework creates for a task
  (the paths already using commit_trailers: safety-net, wave, worktree merge,
  squash-merge body). Commits an agent makes itself via `git commit` in the coder
  prompt are out of scope carries one `Plan-Slug: <key>`
  trailer. No plan, no trailer.
- The AIFactory deploy workflow copies the `Plan-Slug` trailer of the resolved deployed ref
  (`needs.changes.outputs.ref`, not necessarily `github.sha`) onto the factory-gitops image-SHA bump commit.

## Affected users and systems

AIFactory backend (`core/commit_trailers.py`, trusted-plan ingest in
`web-server/server/routes/execution.py`), `.github/workflows/deploy.yml`,
PFactory governance reader (consumer, unchanged), factory-gitops (receives the
trailer, no file change).

## Constraints

- Same opt-in flag as the existing trailers; off means AIFactory-created commits
  are byte-identical. The deploy copy is a pure copy of a trailer already present,
  so it needs no flag.
- Value is printable, single line (forged-trailer safe), never duplicated.
- No change to the live cluster.

## Open questions

Intake from a plain GitHub issue (no plan) gets no trailer. Decision (owner
delegation 2026-11-17): yes, omit; only a plan's key is a plan slug.
