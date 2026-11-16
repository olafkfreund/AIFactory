---
status: draft
issue: 2047
author: olafkfreund
---

# Intent: seed failover provider logins

## Problem
Build Jobs seed only the logins of the five phase models (#2025). QA provider failover and subtask provider overrides can switch to an operator-enabled provider whose login was never seeded, so the session dies on an auth error.

## Proposed outcome
A Job also gets the logins of every operator-enabled provider in the failover chain. With the default allowlist (claude only) nothing changes.

## Affected users and systems
Operators who enable codex or antigravity via AIFACTORY_RUNTIMES; build and spec Jobs.

## Constraints
Never seed a runtime the operator has not enabled.

## Open questions
None.
