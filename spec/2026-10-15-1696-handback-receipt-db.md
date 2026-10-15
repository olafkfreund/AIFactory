---
status: draft
issue: 1696
intent: intent/2026-10-15-1696-handback-receipt-db.md
---

# Spec: store the handback receipt where the QA Fixer cannot delete it

## Design

Keep the file. Add a database row written by the web server, and make either
one hold the merge.

1. **Table `handback_receipts`** (`server/database/models.py`, new Alembic
   revision after `0400a6b429b0`): `spec_key` (sha256 of the spec dir, String 64,
   primary key, same key as `trusted_contracts` via
   `trusted_contract_store.spec_key_for_dir`), `spec_id` (String 255),
   `received_at` (DateTime, server default now). One row per spec dir, never
   cleared, like the file today.

2. **Store `server/services/handback_receipt_store.py`**, two functions:
   - `record(spec_dir)`: insert the row if absent. Runs only when
     `job_state_store.store_enabled()`; a failure is logged and never raised
     (the file is still written, as today).
   - `on_record(spec_dir) -> bool`: `False` when the store is disabled; `True`
     when the row exists; `True` when the read fails (fail closed, as
     `trusted_contract_store.lookup` does).

   The store is gated on `store_enabled()` because auto-merge already needs it:
   `host_isolated()` (`trusted_contract.py:25-34`) holds every merge without
   `DATABASE_URL`. So a host that can merge always has the table.

3. **Write site: the TFactory route** `apply_task_correction`
   (`server/routes/execution.py:1560`). After `apply_correction` returns
   `success` and `confirm` (the same condition as the audit row), `await
   record(spec_dir)`. The DB is reachable here: `apply_correction` runs in the
   web-server process. The backend module `qa/correction.py` stays unchanged and
   keeps writing the file, which the assertion guard needs.
   The internal `apply_correction` calls in `completion_orchestration.py` (PR
   review fixer, conflict fixer) are not TFactory handbacks and run under
   `asyncio.run` in worker threads, so they are not changed.

4. **Read sites: the PR endgame** (`server/services/pr_endgame.py`).
   A handback verdict can never auto-merge (`decide_merge` returns
   `AUTO_MERGE` only when `_verdict_pass`), and human approval does not clear
   it, so the DB receipt is applied as a veto on top of the existing decision:
   - In `run_pr_endgame` (async, the one entry every route passes through),
     before the `merge_disposition` block: `if auto_merge and await
     on_record(spec_dir)`, log and set `auto_merge = False`.
   - At merge time, the gate built by `_build_merge_gate` runs in a worker
     thread (`_gated_head` → `asyncio.to_thread`). `run_pr_endgame` wraps it so
     it first runs `on_record` on the event loop
     (`asyncio.run_coroutine_threadsafe(...).result()`) and returns `False` when
     a receipt is on record. This covers a handback that lands while the PR
     waits for review, which is when TFactory verifies.
   - `merge_gate_signals` keeps its file check, so an on-disk receipt from
     before the upgrade still holds.

## Alternatives rejected

- **A column on `job_states`.** The row is keyed by task id and rewritten per
  admission; a re-run could drop the receipt. A separate table keeps the file's
  "never cleared" meaning.
- **A column on `trusted_contracts`.** Rows exist only for signed tasks; legacy
  tasks get handbacks too.
- **Pass the receipt into `merge_gate_signals`.** It is sync and called from
  four sync functions; threading a flag through all of them changes more code
  than the veto and still needs an async read somewhere.
- **Stop writing the file.** The assertion guard reads its baseline from it,
  and the internal fixer paths rely on it.
- **Fail open on a DB error.** A failed read is not "no handback"; the
  trusted-contract lookup already holds on failure.

## Risks

- A DB outage on a kubejob host holds every auto-merge. That matches the
  trusted-contract lookup, which already holds on the same failure.
- The row is written just after `apply_correction` schedules the background
  fixer. The fixer needs a model round-trip before it can touch files, and the
  file is written first, so the window is not reachable in practice.
- Tests that patch `store_enabled` to True would reach the real default DB.
  A `conftest.py` autouse stub returns "no receipt" for them.

## Verification

- Regression test: record a receipt in a SQLite DB, delete the file, run
  `run_pr_endgame` with an `auto` tier: auto-merge is withheld. It fails on
  current `dev` (no store, the deleted file lets it merge).
- The merge-time wrapper returns `False` when a receipt is on record.
- The route records only a confirmed, successful correction
  (`tests/audit/test_task_action_audit.py`).
- Alembic upgrade on SQLite creates the table.
- Existing merge-gate tests, including the file-based handback test, pass.
- `ruff check`, `ruff format --check`, mypy on touched files.
