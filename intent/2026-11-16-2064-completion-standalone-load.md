---
status: approved
issue: 2064
---

# Intent: completion.py loads standalone in parallel_integration

## Problem
`parallel_integration._load_completion_emitter` loads `server/services/completion.py` by file path; its `from .billing import ...` raises ImportError, the failure is cached, and parallel worker live/progress events never emit.

## Outcome
The emitter loads, with a regression test that loads it the standalone way.

## Constraints
Smallest change; the emitter stays optional and never breaks a build.
