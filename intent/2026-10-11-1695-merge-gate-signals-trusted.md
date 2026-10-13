---
status: approved
issue: 1695
author: olafkfreund
---

# Intent: merge-gate signals come from a trusted source

Follow-up to #1672 ("Known gap (D-gap)" and Q1/Q2 in
`plan/2026-10-10-1672-agent-writable-merge-signals.md`). Still open on dev
(e176db99): no commit or PR fixes it.

## Problem

For a low-tier task, the auto-merge decision rests on four signals: the
TFactory verdict, achieved VAL against its floor, CI parity, and host-CI green.
`merge_gate_signals` in `apps/web-server/server/services/pr_endgame.py`
(:400-452) reads all of them from `spec_dir/task_metadata.json`. The coding
agent can write that file. Nothing fetches the signals from TFactory or GitHub.

#1672 moved only the review tier to the signed contract. These signals stayed
where they were.

As a result:

- When a signal is missing, it falls back to a default that passes. No code in
  `apps/` writes these keys, so a low-tier task auto-merges with nothing
  measured.
- Any negative the gate could act on (`ciParity: false`, `hostCiGreen: false`,
  `achievedVal` below `valFloor`, a failing verdict) has to be written by the
  agent, and the agent can also overwrite or delete it. A probe against the
  real code turned each of these from a hold into `auto-merge` by editing or
  removing one key.
- Forging a pass gains nothing today, because the defaults already pass. The
  risk becomes real once something starts writing true negatives into the
  metadata.

Medium and hard tiers never auto-merge, and hosts that are not isolated
already withhold auto-merge (#1667). So the hole is open only for low-tier
tasks on an isolated host.

## Proposed outcome

- The low-tier merge gate decides on verdict, VAL, floor, parity and host-CI
  values that the coding agent cannot write or delete.
- A failure measured by the trusted source holds the merge. Nothing the agent
  writes into the spec dir can clear it.
- Both routes into the gate get the same signals: the auto-PR endgame and the
  human-approval clearance.

## Affected users and systems

- AIFactory web-server: `pr_endgame.py` (`merge_gate_signals`,
  `merge_disposition`, `run_pr_endgame`, `_clears_with_human_approval`).
- AIFactory backend: `merge/merge_policy.py` (`decide_merge`, consumer only).
- TFactory, as the source of verdict, VAL and parity. AIFactory has no client
  today that reads these back.
- GitHub checks, as the source of host-CI status.
- Operators who rely on low-tier auto-merge.

## Constraints

- Must not loosen the gate. A recorded negative must keep holding the merge
  until a trusted value replaces it. A re-fetch may add a red result; it must
  never invent a green one.
- Fix once, in `merge_gate_signals`, so both callers get it. Keep the keyword
  interface of `decide_merge`.
- Leave alone: the #1672 signed-tier block, the `host_isolated` check (#1667),
  and `trusted_contract.py` while PR #1751 (#1708) is in flight.
- The handback receipt (`handback_received.json`) is #1696. Keep it out of
  scope.
- GitHub calls reuse the endgame's existing runner (`_default_runner`, which
  wraps `gh` in `github_env(child_env())`, #1680), not a new bare `gh`
  subprocess with the server's full environment.
- Fetched values written to logs must pass CodeQL `py/log-injection`.
- When TFactory or GitHub is slow or unreachable, the endgame must not hang or
  crash. It must reach a defined result.
- Any change to TFactory itself is cross-repo and needs its own issue and
  release order.

## Open questions

1. Unreachable or unmeasured source: hold (fail closed), or keep today's
   "nothing measured, nothing blocks"?
2. Source per signal: TFactory for verdict, VAL, floor and parity, and GitHub
   checks for host-CI? Or only some of these now, with the rest in later
   issues?
3. Metadata reads, once a trusted source exists: drop them, or keep
   agent-written negatives as an input that can only tighten the gate?
4. Scope: verified trusted tasks only (as in #1672), or legacy and from-issue
   tasks too? Those overlap with #1697.
5. Cross-repo: is a TFactory change (endpoint, auth, or a VAL/parity schema)
   in scope, or must AIFactory use only what TFactory exposes today
   (`GET /{spec_id}/verdicts.json`, contents and auth not yet confirmed)?

## Approver decisions (2026-10-11)

- Scope: verified trusted tasks only (as #1672); legacy and from-issue tasks stay with #1697.
- For those tasks, hold the merge when no trusted result exists (fail closed).
- Sources now: TFactory for the verdict, GitHub checks for host-CI; VAL and parity become follow-ups.
- Keep the metadata values as tighten-only inputs (agent-written negatives still hold).
- Use only what TFactory exposes today; no TFactory change in scope.
