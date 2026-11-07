---
status: draft
issue: 2014
author: olafkfreund
---

# Intent: make the control mapping's branch-protection claim match what holds now

## Problem

The published autonomy matrix and control mapping (Factory#1962) put one
branch-protection `claim` on six controls: `policy.tier.low`, `.medium`,
`.hard`, `gate.merge.merge_policy`, `gate.server.services.pr_endgame` and
`gate.server.services.pr_review_service`. It says GitHub does not stop an admin
merging past a red required check "while enforce_admins is false (measured
2026-10-07, Factory#943)".

Factory#943 is closed. Since 2026-10-22 `enforce_admins` is on for `main` in
AIFactory, PFactory, TFactory, CFactory and Factory. It is still off on every
`dev` branch and on factory-gitops `main`, where the CD bot pushes. A read-only
`gh api …/protection/enforce_admins` check on 2026-11-07 shows the same.

So the page understates the control on `main`, the branch that reaches the
cluster, and does not say that the bypass is still open on `dev`. Factory#1958
needs the evidence trail to say what actually holds.

## Proposed outcome

The six controls carry a claim that says, with the date and the issue:
- on `main`, branch protection binds admins too, so a red required check blocks
  every merge;
- on `dev`, it binds non-admin merges only. The fleet's merge paths do not use
  admin bypass, but GitHub does not prevent it there.

The generated markdown page and JSON are regenerated from that, and
`autonomy matrix matches the policy` stays green.

## Affected users and systems

- `docs/compliance/control-objectives.toml` (the hand-kept input), and its
  generated outputs `docs/docs/compliance/autonomy-matrix.md` and
  `docs/static/compliance/autonomy-matrix.json`.
- Readers of the published page: auditors, and Factory#324 / #1958.
- No runtime code, cluster or branch protection changes.

## Constraints

- Claim only what was measured. Changing branch protection is live state and
  out of scope.
- Keep the generator and its `--check` unchanged.
- The hub row `Factory/docs/compliance/control-matrix.md:53` says the same stale
  thing. It is a one-line edit in another repo, done as a separate exempt PR
  that links this one.

## Open questions

None. Factory#943 records the owner's decision (option 2).
