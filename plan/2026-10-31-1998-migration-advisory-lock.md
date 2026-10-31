---
status: draft
issue: 1998
spec: spec/2026-10-31-1998-migration-advisory-lock.md
coders:
  1: codex   # lead assigned Codex to this lane
  2: codex
---

# Plan: Postgres advisory lock around boot migrations

Approved decisions (from the spec; a port of PFactory PR #982 plus its review):
- `_upgrade_head_with_pg_lock()` on a dedicated `NullPool` async engine, one
  connection set to `isolation_level="AUTOCOMMIT"`; `SET lock_timeout = '300s'`,
  `SELECT pg_advisory_lock(:k)`, the upgrade via `asyncio.to_thread`, then
  `SELECT pg_advisory_unlock(:k)` in `finally`; engine disposed in an outer
  `finally`.
- An unlock failure is logged with `logger.warning(..., exc_info=True)`, never
  raised (it must not mask the upgrade's error; closing the session frees the lock).
- `_MIGRATION_LOCK_KEY = 0x4149464143545259` ("AIFACTRY"), distinct from
  PFactory's key and AIFactory's other advisory keys.
- Lock only when `engine.dialect.name == "postgresql"`; SQLite unchanged;
  `MIGRATIONS_AUTO_APPLY=false` unchanged.
- No redundant local imports: `asyncio` imported once at module top.

## Steps

1. `apps/web-server/server/database/engine.py`: add top-level `import asyncio`
   and `from sqlalchemy.pool import NullPool`; add `_MIGRATION_LOCK_KEY` with a
   one-line why comment below `_ALEMBIC_INI_PATH` (~line 105); add
   `_upgrade_head_with_pg_lock()` after `_alembic_upgrade_head_sync` (~line 132)
   as decided above; in `init_db()` (~lines 187-193) drop the local
   `import asyncio` and branch on `engine.dialect.name == "postgresql"`.
   → verify by `apps/backend/.venv/bin/ruff check apps/web-server/server/database/engine.py`
   Traps: `engine` is built at import from `DATABASE_URL`; tests needing
   another URL run `init_db()` in a subprocess.
2. Tests:
   a. `tests/postgres/test_p1_alembic.py`: give `_init_db_subprocess` an optional
      `barrier_dir` (env `INIT_DB_BARRIER`; after imports each process touches
      a file named by its pid and waits up to 30 s for 2 files), and add
      `test_concurrent_init_db_applies_migrations_once` (`postgres`, `slow`):
      `_drop_schema`, run two subprocesses through a 2-worker thread pool, assert
      both exit 0, assert `alembic_version` rows == [script head] (head read from
      a local alembic `Config` built from `WEB_SERVER_ROOT`).
   b. New `tests/test_migration_lock.py`: (i) SQLite `init_db()` in a subprocess
      with a `before_cursor_execute` listener prints no `pg_advisory`;
      (ii) the order test with a fake engine/connection, monkeypatching
      `create_async_engine` and `_alembic_upgrade_head_sync`: asserts
      `poolclass=NullPool`, `isolation_level="AUTOCOMMIT"` requested, and
      calls == SET lock_timeout, lock, upgrade, unlock, dispose;
      (iii) upgrade raises and unlock raises: the upgrade error propagates, a
      warning is logged, dispose still runs.
   → verify by `apps/backend/.venv/bin/pytest tests/test_migration_lock.py -q` and,
   with `TEST_POSTGRES_URL` set, `pytest tests/postgres/test_p1_alembic.py -m postgres -q`.
   Traps: Postgres tests skip without `TEST_POSTGRES_URL`. Never `git add -A`
   (venv symlink in the worktree).

## Tests

- ruff check + format --check on touched files: clean.
- `pytest tests/test_migration_lock.py -q`: pass.
- `pytest tests/postgres/test_p1_alembic.py -m postgres` against Postgres: pass;
  the concurrency test fails on `origin/dev` (red output in the PR body).

## Rollback

Revert the PR; boot migrations go back to unlocked. No schema or data change.
