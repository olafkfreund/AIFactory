---
status: draft
issue: 1998
intent: intent/2026-10-31-1998-migration-advisory-lock.md
---

# Spec: Postgres advisory lock around boot migrations

## Design

A port of PFactory PR #982, with the minor points from its review applied.

In `apps/web-server/server/database/engine.py`:

- New `_upgrade_head_with_pg_lock()`: create a dedicated
  `create_async_engine(DATABASE_URL, poolclass=NullPool)`, open one connection,
  set `isolation_level="AUTOCOMMIT"` (never "idle in transaction" while it
  waits), run `SET lock_timeout = '300s'`, then
  `SELECT pg_advisory_lock(:k)` on `_MIGRATION_LOCK_KEY`, then run
  `_alembic_upgrade_head_sync` via `asyncio.to_thread`, then
  `SELECT pg_advisory_unlock(:k)` in a `finally`. The engine is disposed in an
  outer `finally`. The dedicated NullPool engine keeps the lock connection out
  of the app pool, which PFactory found can be used from another event loop
  ("Future attached to a different loop" in its nested Postgres suite).
- An unlock failure is logged (`logger.warning`, with traceback) and not
  raised, so it never hides the upgrade's own error. The lock is session-level,
  so closing the connection on dispose releases it anyway.
- `init_db()`: when `MIGRATIONS_AUTO_APPLY` is true and
  `engine.dialect.name == "postgresql"`, call `_upgrade_head_with_pg_lock()`;
  otherwise (SQLite) the bare `asyncio.to_thread(_alembic_upgrade_head_sync)`
  as today. `MIGRATIONS_AUTO_APPLY=false` is unchanged.
- `asyncio` is imported once at module top; the local `import asyncio` in
  `init_db()` goes.
- Key: `_MIGRATION_LOCK_KEY = 0x4149464143545259` ("AIFACTRY" in ASCII),
  distinct from PFactory's `0x50464143544F5259` ("PFACTORY") and from
  AIFactory's existing keys (`0x0A1FAC700668`, `0x0A1FAC701919`). AIFactory
  and PFactory share one Postgres server (`postgres.factory.svc`) but use
  separate databases (`aifactory`, `pfactory`). Advisory locks are scoped to
  the database, so they could not clash anyway; the distinct key costs nothing.

## Alternatives rejected

- **Pre-sync Argo hook Job** running migrations once: needs a gitops change and
  DDL rights in the Job. PFactory's lead chose the lock; this is a port.
- **Lock on a connection from the app engine** (PFactory's first version):
  breaks the nested Postgres suite with a cross-loop pool connection.
- **`pg_advisory_xact_lock` in `env.py`**: touches every CLI run.
- **`pg_try_advisory_lock` and skip**: a pod that skips could serve before
  the schema is at head.

## Risks

- A hung migration blocks other booting pods; after 300 s they fail boot and
  the kubelet restarts them.
- If `init_db()` is cancelled while the upgrade thread runs, `finally` releases
  the lock while the thread keeps migrating. Accepted in PFactory #982's review.
- One extra Postgres connection during the boot upgrade only.

## Verification

- `tests/postgres/test_p1_alembic.py::test_concurrent_init_db_applies_migrations_once`
  (markers `postgres`, `slow`, CI Postgres job): drop the schema, start two
  `init_db()` subprocesses held at a file barrier, assert both exit 0 and
  `alembic_version` has one row at head. It must fail on `origin/dev`.
- `tests/test_migration_lock.py` (no DB server): the order test asserts
  `AUTOCOMMIT` was requested and the statements run SET lock_timeout, lock,
  upgrade, unlock, dispose; an unlock failure is logged and the upgrade's error
  is raised; a SQLite `init_db()` issues no `pg_advisory` SQL.
- ruff on touched files.
