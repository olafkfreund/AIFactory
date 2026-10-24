---
status: draft
issue: Factory#1963
author: olafkfreund
---

# Intent: record the verify verdict and its vote split as PR evidence

## Problem

TFactory's verify verdict comes from model judges voting best-of-3. AIFactory's
merge gate reads that verdict at merge time
(`apps/web-server/server/services/pr_endgame.py:1831-1866`, through
`pfactory.tfactory_client.tfactory_verdict` at
`apps/backend/pfactory/tfactory_client.py:901`) and decides together with the
deterministic floor: host CI green, VAL floor met, CI parity
(`merge_gate_signals`, `pr_endgame.py:395`).

The verdict, the vote split and the floor values leave no trace a reviewer can
read. `tfactory_verdict` reduces TFactory's `verdicts.json` to `"pass"`,
`"fail"` or `None` and throws away the `verdict_vote` summary TFactory writes
(TFactory `apps/backend/agents/evaluator.py:2618-2626`). The PR body records
only the review tier (`pr_endgame.py:2003-2012`).

Factory#1963 has one open acceptance box: "The verdict appears in the artifact
header as recorded evidence with its vote split".

## Proposed outcome

On every auto-merge PR, the merge gate writes one evidence section into the PR
body. It holds:

- the verify verdict: pass, fail or hold
- the vote split as TFactory reported it, or "votes: not reported"
- the floor signals: host CI green, achieved VAL against the floor, CI parity
- one line saying the verdict is recorded as evidence and the deterministic
  floor decides

The section is rewritten in place when the evidence changes. It is never
appended twice.

## Affected users and systems

- AIFactory web server: `pr_endgame.py` (merge gate, PR body).
- AIFactory backend: `pfactory/tfactory_client.py` (`tfactory_verdict`).
- Human reviewers and auditors who read the PR.
- TFactory is read only. No TFactory change.

## Constraints

- The evidence is a record only. It must never change a merge decision, and a
  failure to write it must never fail or hold the gate.
- Nothing read from TFactory or task metadata is echoed raw into the PR body.
  Only validated numbers, split keys of the form `N-M`, and fixed labels.
- No secrets, hosts or URLs in the PR body.
- AIFactory only. Every existing caller and test of `tfactory_verdict` keeps
  working unchanged.

## Open questions

- Where is the "artifact header"? Decided (lead recommendation, user-approved
  2026-10-24): the PR evidence block in the PR body that `pr_endgame` already
  writes. Record the verdict (pass, fail or hold), the vote split as TFactory
  reports it, and the floor signals (host_ci_green, VAL, ci_parity), with one
  line saying the verdict is evidence and the deterministic floor decides. If
  TFactory's report has no vote split, record "votes: not reported".
