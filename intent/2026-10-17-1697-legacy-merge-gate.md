---
status: approved
issue: 1697
author: olafkfreund
---

# Intent: legacy and from-issue tasks need a real verified green to auto-merge

## Problem

From-issue (MODE 0) tasks have no signed task contract, so
`trusted_contract.resolve_contract` returns `legacy` for them. Their review
tier comes from `task_metadata.json`, which in turn came from a GitHub label.
For these tasks the endgame is looser than for signed ones:

- `_build_merge_gate` returns `None` for a legacy task
  (`apps/web-server/server/services/pr_endgame.py:1676-1677`), so the #1695
  merge-time gate (real TFactory verdict + host CI on the head commit) never
  runs for them.
- `merge_gate_signals` starts from `tfactory_verdict="pass"` and
  `host_ci_green=True` (`pr_endgame.py:413-424`) when nothing was fetched, so
  "nobody measured it" decides like "it passed".
- The path risk floor is advisory unless `AIFACTORY_PATH_RISK_FLOOR_ENFORCE`
  is on (`pr_endgame.py:207`, `:307-311`), so a legacy `low` task touching
  `auth/` still decides as `low`.

Net effect: with `AIFACTORY_AUTO_MERGE` on, on an isolated host, a legacy
`low` task can auto-merge with no TFactory verdict and no host-CI check.

## Proposed outcome

A legacy or from-issue task auto-merges only when TFactory returned a real
pass and host CI is green on the head commit, and its tier is never below
the floor its changed paths demand. A missing verdict or missing CI means
"not passed".

## Affected users and systems

- AIFactory web server, PR endgame (`server/services/pr_endgame.py`).
- MODE 0 `factory:low` from-issue tasks and any other task without a signed
  contract.
- Projects with no TFactory or no CI: these tasks stop auto-merging.

## Constraints

- Fail closed: unknown or unmeasured is never green.
- Signed (verified) tasks keep their current behaviour.
- Keep the change small, in the one module, reusing existing helpers.
- No deploys or cluster changes.

## Open questions

None. The user chose option B of the decision brief (2026-10-17).
