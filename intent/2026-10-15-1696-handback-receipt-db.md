---
status: approved
issue: 1696
author: olafkfreund
---

# Intent: store the handback receipt where the QA Fixer cannot delete it

## Problem

Still open on `dev`. When TFactory hands a build back, `qa/correction.py`
(`record_handback`, line 96) writes `handback_received.json` into the spec dir.
The merge gate treats that file as a failing verdict:
`server/services/pr_endgame.py` `merge_gate_signals` (lines 390, 442, 469)
returns `tfactory_verdict="handback"` while `(spec_dir / _HANDBACK_RECEIPT).is_file()`.
Nothing is stored in the database.

The QA Fixer that the handback starts (`_default_fixer` → `_run_fixer_bg`)
runs inside the web-server process with the control-plane spec dir as its
working tree, even on the `kubejob` backend. An agent that deletes the file
removes the HOLD, and the next merge decision auto-merges a build TFactory
rejected. Follow-up from #1672 (Q3).

## Proposed outcome

A TFactory handback holds the auto-merge even when `handback_received.json` is
deleted afterwards. The receipt is also kept in the web server's database,
which the agent cannot write.

## Affected users and systems

- `apps/web-server`: the correction route, the PR endgame merge gate, the DB
  models and an Alembic migration.
- Hosts that can auto-merge at all: `host_isolated()` needs the `kubejob`
  backend and `DATABASE_URL` (`trusted_contract.py:25-34`). Other hosts already
  hold every merge.

## Constraints

- An existing on-disk receipt must still count, so an in-flight HOLD survives
  the upgrade. Keep writing the file: the assertion guard
  (`check_fix_cycle_assertions`) reads its baseline from it.
- Use the existing Alembic migration chain (head `0400a6b429b0`).
- A failed DB read must hold the merge, never allow it (same rule as the
  trusted-contract lookup, `trusted_contract_store.lookup`).
- Existing tests that write the file directly
  (`test_pr_endgame_merge_gate.py`) keep passing.

## Open questions

None. The internal `apply_correction` calls (PR review fix, conflict fixer in
`completion_orchestration.py`) also write the file, but they are not TFactory
handbacks; this task records only the inbound TFactory handback in the DB and
leaves their file behaviour unchanged.
