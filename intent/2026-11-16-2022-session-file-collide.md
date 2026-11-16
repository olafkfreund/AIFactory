---
status: approved
issue: 2022
author: olafkfreund
---

# Intent: session_NNN.json collisions and novelty-gate edge cases (#2022, #2035)

## Problem

`agents/coder.py` restarts `iteration = 0` each run, and `qa/reviewer.py` and
`qa/fixer.py` pass their own counters to `save_session_memory`. All write
`session_NNN.json` into one insights directory, so a later writer overwrites an
earlier file. With the RFC-0021 Phase 1 exact-repeat gate, session 2 drops an
item session 1 stored; if session 1's file is then overwritten, the item exists
nowhere (#2022). The gate also does not run when Graphiti is the primary store.

Two smaller defects (#2035): a gotcha whose text contains a
`## [YYYY-MM-DD HH:MM]` line is split into two entries by the `gotchas.md`
dedupe, so a later call can be wrongly rejected; and `repeat_key` raises
`TypeError` on a dict with mixed-type keys, failing the whole save.

## Proposed outcome

- A save never overwrites an existing `session_NNN.json`; no stored item is lost.
- A gotcha with an embedded header is stored and deduped as one entry.
- `repeat_key` never raises on a dict entry.
- PFactory and TFactory (same vendored memory code) get the same fixes.

## Affected users and systems

`apps/backend/memory/{sessions,novelty}.py`, `agents/tools_pkg/tools/memory.py`
in AIFactory, PFactory, TFactory.

## Constraints

- No change to the file format or to `load_all_insights` ordering.
- Graphiti is not configured in the pod; no live-cluster action.

## Open questions

Graphiti-primary path skips the gate. Proposed: leave as is, record it in the
spec as a known, unconfigured-path limitation (see spec).
