---
status: draft
issue: 1750
author: olafkfreund
---

# Intent: the TFactory test profile must come from the trusted contract

## Problem

This is still open on dev at `e176db99`. No commit mentions #1750. PR #1751,
which fixes #1708, leaves this reader alone and lists #1750 as a follow-up.

When `start_task` (`apps/web-server/server/routes/execution.py:366`) routes a
spec to TFactory (`handoff:tfactory`, or `type:testing` with no handoff label),
it calls `load_tfactory_block`
(`apps/backend/pfactory/tfactory_client.py:69-85`). That function does a raw
`json.loads` of `implementation_plan.json` and returns its `tfactory` block
as-is. That block is the test profile: lanes, frameworks, endpoints, coverage,
mutation, security and `ac_to_code_map`. `build_handoff_payload` copies it into
the payload unchanged, and `send_handoff` posts it to TFactory.

No trust check runs on this path. Anything that can write the spec's
`implementation_plan.json` before a start or restart decides what TFactory is
told to test. That includes an agent from an earlier run. It can drop the
security or mutation lanes, set thresholds to zero, or point `ac_to_code_map`
at harmless files. Nothing fails or logs, and TFactory can come back green on
a weaker test set, which then feeds auto-PR and merge. A held contract does not
stop it either, because this path never asks for the verdict. A read-only probe
at `e176db99` confirmed it: the payload carried the edited block word for word
and ignored the signed `context/task_contract.json` beside it.

The sibling route `handoff_to_tfactory` (`execution.py:936`) and
`completion_orchestration.py` already gate their contract through
`handoff_contract(spec_dir, await lookup(spec_dir))`. This is the last
TFactory-bound reader with no gate.

Rated MEDIUM in the #1708 review. It is an integrity bypass of the test gate,
not code execution or a secret leak.

## Proposed outcome

- On the `start_task` TFactory path, the test profile sent to TFactory comes
  only from a contract the web server has verified.
- When the contract is on hold, no tampered profile is sent.
- Legacy tasks (no trusted-plan trace) still hand off to TFactory with their
  declared profile.
- A test covers the route, so a regression fails CI.

## Affected users and systems

- `apps/web-server/server/routes/execution.py` (`start_task`, TFactory branch)
- `apps/backend/pfactory/tfactory_client.py` (`load_tfactory_block`) and its
  exports in `pfactory/__init__.py`
- `tests/test_pfactory_tfactory_client.py:69-87`, which pins the current
  unchecked read
- TFactory, and anyone who relies on its verdict to gate the PARR endgame

## Constraints

- The issue's suggested reader, `core.contract_trust.trusted_contract`, cannot
  work here. It runs in the pod and reads `AIFACTORY_TRUSTED_CONTRACT`, which
  the web server never has, so in the web server it can never return verified.
  The web server's own verdict comes from `lookup` and `resolve_contract`
  (`server/services/trusted_contract.py`).
- When in doubt, hold. A lookup failure, an exception, or a trusted trace with
  no record must not fall back to the plan file.
- Only isolated hosts (kubejob plus `DATABASE_URL`, `host_isolated`) can
  verify. On other hosts, trusted-plan tasks would lose their declared profile.
  That is a visible change. #1680 landed (`1c2df772`), but `host_isolated`
  still says "revisit when #1680 lands" and was never widened; no open issue
  tracks that.
- `implementation_plan.json` is rewritten during the build and can lose its
  `tfactory` block. The signed copy is `context/task_contract.json`.
- Overlap: PR #1751 edits `tfactory_client.py` (other functions) and the
  autonomy matrix, which may need regenerating. PR #1752 edits `execution.py`
  away from line 366. Do not touch the Factory#943 cells in the autonomy
  matrix.
- Target `dev`, add a CHANGELOG entry, follow the intent, spec and plan gates.

## Open questions

1. On hold: send the handoff with an empty `tfactory` block so TFactory works
   out its own plan, or skip the handoff entirely? The issue says "send
   nothing", which could mean either.
2. Legacy tasks: keep the raw `implementation_plan.json` read, or read
   `context/task_contract.json` through `load_task_contract`?
3. `load_tfactory_block`: keep it for the legacy path, or delete it with its
   exports and the tests that pin it?
4. Is it acceptable that trusted-plan tasks on hosts that cannot verify lose
   their declared TFactory profile? If not, widening `host_isolated` now that
   #1680 has landed is a separate issue to file first.
5. Order: wait for PR #1751 to merge and branch from the new dev, or start now
   and rebase?
