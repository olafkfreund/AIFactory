---
status: draft
issue: 1454
author: Olaf Krasicki-Freund
---

# Intent: The security pre-merge gate has never scanned a diff

## Problem

`core/worktree.py:986-1005` presents a pre-merge scan for secrets and injection that
refuses a high-severity finding, and `docs/environment-reference.md` documents it as a
control. In production it has never run, and it could not refuse a merge if it did:

1. It is gated on `AIFACTORY_SELF_HEAL`, which defaults to off and is set nowhere in any
   deployment. Only the test suite turns it on.
2. If the scanner crashes, `agents/self_heal_integration.py:130-132` returns
   `gate_decision([])`, which reads as a clean verdict.
3. The call site catches everything with `except Exception: pass` (`worktree.py:1004`).

A disabled feature is honest. This one is documented as present, so readers
believe merges are scanned when they are not.

## Proposed outcome

What the docs say about the gate matches what it does. When the gate is enabled, a
scanner failure is reported as "not scanned", never as "clean", and a merge log
shows which of the two happened.

## Affected users and systems

- `apps/backend/core/worktree.py` (the merge path) and `agents/self_heal_integration.py`
- `docs/environment-reference.md`, `.env.example`
- Possibly `factory-gitops` (the AIFactory deployment env), if the gate is turned on
- Anyone who relies on merges being security-scanned

## Constraints

- Must not start blocking merges fleet-wide without an explicit decision.
- A scanner outage must not be reported as a pass.
- The gitops change, if any, restarts pods, so it goes in a quiet window (see #1425/#1465).

## Open questions

1. Should the gate be **enabled** in production, or **left off** with the docs
   saying plainly that it is off?
2. When enabled and the scanner fails: **fail closed** (refuse the merge) or **fail open
   loudly** (merge, but log and mark it "unscanned")?
