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

So the page understates the control on `main` in those five repos, and does
not say that the bypass is still open on `dev` and on factory-gitops `main`. Factory#1958
needs the evidence trail to say what actually holds.

## Proposed outcome

The six controls carry a claim that states the measured setting and its
scope, with the dates (applied 2026-10-22, re-read 2026-11-07) and Factory#943:
- `enforce_admins` is on for `main` in AIFactory, PFactory, TFactory, CFactory
  and Factory, so there the required checks bind admins too;
- it is off on every `dev` branch and on factory-gitops `main`. There the
  checks bind non-admin merges only: the fleet's merge paths do not use admin
  bypass (the code paths listed in Factory#943's 2026-10-07 comment), but
  GitHub does not prevent it.

The claim states the setting, not a stronger guarantee than it gives. The
TOML comment that calls the claim "verbatim from the step 0 measurement" is
updated to name the new source too.

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
