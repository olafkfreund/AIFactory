---
status: draft
issue: 1700
author: olafkfreund
---

# Intent: wire the remaining GitHub paths to per-org installation tokens (#1700, #2038)

Predecessor: `intent/2026-11-14-1700-github-app-installation-credential.md`
(PR #2039). Also closes the review follow-ups in #2038.

## Problem

With `AIFACTORY_GITHUB_APP_ORG_INSTALLATIONS` set, the server fails closed:
only the code paths wired in #2039 get a GitHub token. The paths listed under
"Not wired" in that plan's Deviations (the interactive GitHub, PR and git
routes, the PR data service, Copilot dispatch, task-branch git calls, the
merger, the shared `gh` helper, the control plane's worktree fetch) still run
with no token, so an org on its own installation cannot use them. #2038 lists
five smaller defects from the same review: overlapping project paths deny a
token, a token read can mint on the event loop, the endgame warms up when
auto-PR is off, `_parse_org_map` blames the wrong part of an entry, and the
chart does not validate org keys.

## Proposed outcome

- In multi-installation mode every listed path uses the installation token of
  the org that owns the project it works on. An unbound or unknown org still
  gets no token. Without the map nothing changes.
- Kubernetes spec Jobs keep getting no GitHub token (they never had one; the
  code pops it). The docs say so instead of listing them as unwired.
- Registering or repointing a project whose path overlaps another org's
  project is refused in the mode.
- The PR endgame's context gathering runs in a thread, and the warm-up runs
  only when auto-PR is on.
- `_parse_org_map` names the part that is wrong. The chart rejects org keys
  containing `,`, `=` or whitespace at render time.

## Affected users and systems

Operators running the per-org map (new, opt-in); web-server routes
`github`, `git`, `pr`, `execution`; services `gh`, `pr_data_service`,
`task_branch`, `merger`, `build_backend`, `completion_orchestration`;
`project_registry`; the `aifactory` chart; `docs/docs/concepts/github-app.md`.

## Constraints

- Fail closed; no token ever crosses orgs; no behaviour change without the map.
- Minting stays off the event loop where the call site is async.
- No live cluster change. The live test with a second installation is an
  owner action (needs a second installation id).
- #1701 is out of scope.

## Open questions

None. Pre-approved by the owner for this bulk run.
