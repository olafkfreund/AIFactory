---
status: draft
issue: 1998
author: olafkfreund
---

# Intent: serialise `alembic upgrade head` at boot across pods

## Problem

`init_db()` (`apps/web-server/server/database/engine.py`) runs `alembic upgrade
head` at every boot when `MIGRATIONS_AUTO_APPLY` is true, the default. Nothing
serialises the upgrade across pods. A KEDA scale-up, a node restart or a crash
loop can start two AIFactory pods at once; both run the same upgrade, and the
loser fails with a `UniqueViolationError` on `alembic_version` or runs data
steps twice. PFactory had the same gap (PFactory #981, fixed by PR #982).

## Proposed outcome

Two pods booting at the same time against one Postgres database both start,
and each pending migration is applied exactly once. SQLite (local dev)
behaves as today.

## Affected users and systems

AIFactory web-server pods in the cluster (Postgres database `aifactory` on the
shared `postgres.factory.svc` server); local dev (SQLite, unchanged).

## Constraints

- Port PFactory #982 faithfully, plus the minor points from its review.
- No gitops change, no new dependency, `MIGRATIONS_AUTO_APPLY=false` unchanged.

## Open questions

None. The issue states the fix: a Postgres session advisory lock around the
upgrade, as in PFactory #982.
