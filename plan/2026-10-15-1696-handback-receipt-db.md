---
status: approved
issue: 1696
spec: spec/2026-10-15-1696-handback-receipt-db.md
---

# Plan: store the handback receipt where the QA Fixer cannot delete it

Self-contained summary of the approved decisions. You do not need to open the
intent or the spec.

**Problem.** A TFactory handback writes only `handback_received.json` in the
spec dir (`apps/backend/qa/correction.py:96`). The merge gate
(`apps/web-server/server/services/pr_endgame.py` `merge_gate_signals`,
lines 442 and 469) holds while that file exists. The QA Fixer the handback
starts runs in the web-server process on the control-plane spec dir, so it can
delete the file and lift the HOLD.

**Decisions.**

- D1. New table `handback_receipts`: `spec_key` String(64) PK (from
  `trusted_contract_store.spec_key_for_dir`), `spec_id` String(255) not null,
  `received_at` DateTime not null, server default now. Alembic revision after
  `0400a6b429b0`. Never cleared.
- D2. New module `apps/web-server/server/services/handback_receipt_store.py`
  with `async record(spec_dir)` and `async on_record(spec_dir) -> bool`. Both
  do nothing / return `False` unless `job_state_store.store_enabled()`.
  `record` inserts if absent, logs and swallows any error. `on_record` returns
  `True` on a row or on any error (fail closed).
- D3. Write only in the TFactory route `apply_task_correction`
  (`apps/web-server/server/routes/execution.py:1560-1588`), when the result has
  `success` and `confirm`. `qa/correction.py` and the internal fixer calls in
  `completion_orchestration.py` are unchanged; the file is still written.
- D4. Read in `run_pr_endgame`: a veto before the `merge_disposition` block
  (`auto_merge = False` when `on_record`), and a wrapper around the merge-time
  gate that runs `on_record` on the event loop from the worker thread. The file
  check in `merge_gate_signals` stays, so a pre-upgrade receipt still holds.
- D5. `apps/web-server/tests/conftest.py` gets an autouse stub making
  `on_record` return `False`, because many tests patch `store_enabled` to True
  and would otherwise read the developer's default DB.

## Steps

1. Model, export and migration.
   - `apps/web-server/server/database/models.py` (append after
     `TrustedContract`, end of file, line 1251): class `HandbackReceipt(Base)`,
     `__tablename__ = "handback_receipts"`, columns per D1, a short docstring
     and `__repr__`, in the style of `TrustedContract`.
   - `apps/web-server/server/database/__init__.py` lines 21 and 41: import and
     export `HandbackReceipt` next to `TrustedContract`.
   - New `apps/web-server/server/database/alembic/versions/20261015_5d1c9e3a7b20_handback_receipts.py`:
     revision `5d1c9e3a7b20`, down_revision `0400a6b429b0`; `create_table` /
     `drop_table`, copying the shape of
     `20261009_0400a6b429b0_trusted_contracts.py`.
   → verify: `cd apps/web-server && DATABASE_URL=sqlite+aiosqlite:///$PWD/../../x.db alembic upgrade head && sqlite3 ../../x.db 'pragma table_info(handback_receipts)'; rm -f ../../x.db`
   Traps: one Alembic head only; do not reformat the rest of models.py.

2. Store module `apps/web-server/server/services/handback_receipt_store.py`
   (new), per D2. Lazy-import `async_session_factory` from
   `server.database.engine` inside the functions (as `TrustedContractStore`
   does) and `store_enabled` from `server.services.job_state_store`. Log with
   constant messages (no request data).
   → verify: `ruff check` and `mypy` on the file.
   Traps: never raise from either function.

3. Write site, `apps/web-server/server/routes/execution.py`.
   Import `record as record_handback_receipt` from the new module (near line 33).
   In `apply_task_correction`, inside the existing
   `if result.get("success") and result.get("confirm"):` block, first
   `await record_handback_receipt(spec_dir)`, then the audit call.
   Test: in `tests/audit/test_task_action_audit.py`
   `test_apply_correction_audits_only_a_confirmed_success`, patch
   `execution.record_handback_receipt` with a recorder and assert it was called
   `expected` times.
   → verify: `apps/backend/.venv/bin/pytest tests/audit/test_task_action_audit.py -q`

4. Read sites, `apps/web-server/server/services/pr_endgame.py`.
   - Add helper near `_build_merge_gate` (line 1584):
     `_with_receipt_veto(gate, spec_dir, loop)` returning a sync callable that
     runs `asyncio.run_coroutine_threadsafe(on_record(spec_dir), loop).result()`
     and returns `False` if held, else `gate(head)`. Comment: it runs in a worker
     thread (`_gated_head` uses `asyncio.to_thread`).
   - In `run_pr_endgame`, just before `human_approval_required = False`
     (line 1706): `if auto_merge and await on_record(spec_dir):` log
     `"[pr-endgame] %s: TFactory handback on record; auto-merge withheld"` with
     `sanitize_log(spec_id)` and set `auto_merge = False`.
   - After `merge_gate = (...)` (lines 1738-1750): if not None, wrap it with
     `_with_receipt_veto(merge_gate, spec_dir, asyncio.get_running_loop())`.
   - Lazy-import `on_record` (`from .handback_receipt_store import on_record  # noqa: PLC0415`)
     where used, so the conftest stub applies.
   → verify: `apps/backend/.venv/bin/pytest apps/web-server/tests/test_pr_endgame_merge_gate.py -q`
   Traps: `merge_gate` is called only from `_gated_head` in a worker thread;
   calling the wrapper on the loop thread would deadlock.

5. Tests.
   - `apps/web-server/tests/conftest.py`: autouse fixture setting
     `server.services.handback_receipt_store.on_record` to an async function
     returning `False` (D5).
   - `apps/web-server/tests/test_pr_endgame_merge_gate.py`: import the real
     `on_record` at module import; regression test: SQLite DB in `tmp_path`,
     `Base.metadata.create_all`, patch `server.database.engine.async_session_factory`,
     restore the real `on_record`, `await record(spec)`, no file on disk →
     `_endgame_auto_merge(spec, "auto", ...)` is False. Control: same DB, no row
     → True. Plus a wrapper test: `on_record` stubbed True,
     `await asyncio.to_thread(pe._with_receipt_veto(lambda _h: True, spec, loop), "h")`
     is False; stubbed False → True. Plus `on_record` fails closed when the
     session factory raises.
   - `tests/test_trusted_contract_store.py` style: assert the table exists at
     Alembic head on SQLite (add to that file or a new `tests/test_handback_receipt_store.py`).
   → verify: the pytest commands under Tests.
   Traps: the conftest stub also reaches root `tests/` in a combined run, so
   `tests/test_handback_receipt_store.py` restores the real `on_record` captured
   at import (deviation found while implementing).
   Also: prove the regression test fails on unfixed code (scratch worktree at
   `origin/dev` with only the test file copied in).

## Tests

```
apps/backend/.venv/bin/pytest apps/web-server/tests/test_pr_endgame_merge_gate.py \
  apps/web-server/tests/test_trusted_contract_merge.py \
  apps/web-server/tests/test_merge_decision_reaches_the_merge.py \
  apps/web-server/tests/test_pr_endgame_review_tier.py \
  apps/web-server/tests/test_pr_endgame.py \
  apps/web-server/tests/test_fleet_provider_propagation.py \
  tests/audit/test_task_action_audit.py tests/test_handback_receipt_store.py \
  tests/test_handback_contract.py tests/test_completion_lands_via_merger.py -q
ruff check <touched> && ruff format --check <touched>
```
All pass.

## Rollback

Revert the PR. The table is additive; nothing reads it after a revert.
`alembic downgrade 0400a6b429b0` drops it.
