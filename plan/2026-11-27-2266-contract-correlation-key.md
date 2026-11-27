---
status: draft
issue: 2266
spec: spec/2026-11-27-2266-contract-correlation-key.md
coders:
  1: codex   # small, validation-sensitive key resolution in the emitter core (owner: Codex codes)
  2: agy     # mechanical wiring of the same reader into two call sites plus tests (owner: agy in the team)
reviewers:
  - codex    # independent read of the precedence and validation rules
  - agy      # second model on the same diff (agent-team)
---

# Plan: a trusted-plan task reports under its contract's correlation key

## Approved decisions

The spec, copied here so this plan stands alone. Paths are under `apps/web-server/`.

- **Reader:** `server/services/completion.py` gets `read_contract_key(spec_dir: Path) -> str |
  None`, next to `read_issue_number` (~190).
  - It reads `spec_dir / "context" / "task_contract.json"` and returns `correlation_key` only when
    that is a `str` that `_CONTRACT_KEY.fullmatch`es, where
    `_CONTRACT_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}")`.
  - It returns `None` for a missing file, `OSError`, `ValueError`, a non-dict document, or a
    non-matching value. It never raises.
- **Precedence:** `correlation_key(task_id, issue_number, repo=None, contract_key=None)` (~233).
  - A `contract_key` that is not `None` and not `isdigit()` is returned first.
  - Otherwise the existing logic runs unchanged: `<repo>#<n>`, then `af-<task_id>`, keeping the
  `_MAX_KEY_LEN` guard. An all-digits contract key is ignored here (#1960).
- **Builders:** `build_completion_event` (~565, key ~630), `build_worker_event` (~687, key ~737)
  and `build_worker_progress_event` (~767, key ~813) each gain a keyword
  `contract_key: str | None = None`, forwarded to `correlation_key()`.
  - The callers that pass `issue_number=read_issue_number(spec_dir)` (~865, ~966, ~1231, ~1300)
    also pass `contract_key=read_contract_key(spec_dir)`.
  - A grep for `issue_number=read_issue_number(` must leave no caller without the new argument.
- **Task list:** `server/routes/task_service.py:1287` passes `contract_key=read_contract_key(spec_dir)`.
- **Job-state row:** `server/services/agent_service.py:_read_correlation_key` (~295) returns
  `read_contract_key(spec_dir)` first when it is not `None` and not all digits. Otherwise it falls
  back to today's `provenance.issue_number`. Use the existing `spec_dir_for(project_path, spec_id)`.
- **No change:**
  - `routes/execution.py`;
  - `pfactory/tfactory_client.py`, whose `_thread_issue` already lets the contract key win;
  - CFactory.

## Steps

1. **Codex:** `server/services/completion.py`, per the reader, precedence and builder decisions
   above.

   **Tests** in `tests/test_completion_emitter.py`:
   - **Reader:** `read_contract_key`
     - returns a valid `pf-337-x`, `o/r#5` and an all-digits `42` (returned; `correlation_key`
       ignores it);
     - returns `None` for a missing file, invalid JSON, a list document, a non-str value, an empty
       string, 129 characters, an embedded space, a newline, and a leading `-`.
   - **Precedence:** `correlation_key(..., contract_key="pf-337-x")` wins over the issue and repo.
     A digits-only key, or `None`, falls through unchanged.
   - **Builders:** each of the three event builders, given `contract_key`, emits it.
   - **Callers:** each of the four `spec_dir` callers puts the file's key into the event. Use the
     module's existing fixtures and the `_spec_with` helper.
   - **Handoff pin:** `_thread_issue(contract={"correlation_key": "pf-337-x"}, req={}, task_id="p:s")`
     keeps `pf-337-x`, which equals the event key for the same spec dir.

   **Verify:**
   ```bash
   W=/mnt/code/Source-home/GitHub/AIFactory/apps/web-server/.venv/bin
   (cd apps/web-server && PYTHONPATH=../backend $W/pytest -q -o asyncio_mode=auto tests/test_completion_emitter.py tests/test_completion_outbox.py tests/test_completion_attribution.py)
   ```

2. **agy:** `server/routes/task_service.py:1287` and `server/services/agent_service.py:295`, per
   the task-list and job-state decisions above.

   **Tests** in a new `tests/test_contract_correlation_key.py`:
   - **Task list:** the task-list builder returns `correlationKey == "pf-337-x"` for a spec dir
     with that contract and no issue. Follow how `tests/test_string_issue_identifier.py` builds a
     task and spec dir.
   - **Job-state row:** `_read_correlation_key` returns `pf-337-x`. It returns `"5"` when the
     contract key is digits and `provenance.issue_number` is 5, and `None` when neither exists.

   **Verify:**
   ```bash
   (cd apps/web-server && PYTHONPATH=../backend $W/pytest -q -o asyncio_mode=auto tests/test_contract_correlation_key.py tests/test_string_issue_identifier.py tests/test_completion_emitter.py)
   ```

**Every step:**
- Run `ruff check` and `ruff format --check` on the changed files.
- After the lead commits, run the ratchets from the repo root:
  ```bash
  apps/backend/.venv/bin/python scripts/cq_ratchet.py --base origin/dev --ruff $B/ruff --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'
  ```
  Then run the same with `--tool mypy --mypy $B/mypy --config standards/mypy.ini`.

**Traps:**
- The worktree has no venv. Use the main clone's web-server venv, with `PYTHONPATH=../backend`
  and `-o asyncio_mode=auto`.
- The ratchet only sees committed files.
- Don't change `_MAX_KEY_LEN`, or the `af-` form for keyless tasks.

## Tests

- The verify commands above.
- The web-server suite, compared with an `origin/dev` baseline, with no new failures.
- CI green.
- Live, after the AIFactory release: in a P1 E2E run, CFactory's `pf-…` work item carries the
  AIFactory events and usage.

## Rollback

Revert the squash commit. Events go back to `af-…` keys. No stored state changes.
