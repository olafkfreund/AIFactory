---
status: approved
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

The spec is copied here so this plan stands alone, with the plan review folded in (see Review
below). Paths are under `apps/web-server/`.

- **Validator:** one validator, `_valid_contract_key(value: object) -> str | None`, in
  `server/services/completion.py`.
  - It returns `value` only when it is a `str`, `_CONTRACT_KEY.fullmatch`es and is not all
    digits. `_CONTRACT_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}")`.
  - Anything else gives `None`, including `""`, an over-length value and a bare number (#1960).
- **Reader:** `read_contract_key(spec_dir: Path) -> str | None`, next to `read_issue_number`
  (~190).
  - It reads `spec_dir / "context" / "task_contract.json"` and returns
    `_valid_contract_key(doc.get("correlation_key"))`.
  - It returns `None` on `OSError`, `ValueError` or a non-dict document. It never raises.
- **Precedence (changed from the spec after review):** `correlation_key(task_id, issue_number,
  repo=None, contract_key=None)` (~233).
  - `<repo>#<n>` comes first, as today.
  - Then `_valid_contract_key(contract_key)`.
  - Then `af-<task_id>`, keeping the `_MAX_KEY_LEN` guard.

  So a contract key replaces only the synthetic `af-` fallback, and issue-intake tasks stay
  byte-identical. The approved spec had the contract key winning over `<repo>#<n>`. Both
  reviewers showed that lets an agent-edited or stale contract re-key an issue task. A
  trusted-plan task from `factory submit` has no issue, so the outcome is the same.
- **Builders:** `build_completion_event` (~565, key ~630), `build_worker_event` (~687, key ~737)
  and `build_worker_progress_event` (~767, key ~813) gain the keyword `contract_key: str | None =
  None`, forwarded to `correlation_key()`.
  - All four `spec_dir` callers (~865, ~966, ~1231, ~1300) pass
    `contract_key=read_contract_key(spec_dir)`.
  - That covers the human-approval `done` event: `routes/tasks.py:492` → `emit_terminal_completion`
    → ~1227.
- **Diagnostic (~621–627):** the "synthetic key … (no cross-service join)" log fires only when the
  resolved key starts with `af-`. Otherwise it logs nothing.
- **Task list:** `server/routes/task_service.py:1287` passes
  `contract_key=read_contract_key(spec_dir)`.
- **Job-state row:** `server/services/agent_service.py:_read_correlation_key` (~295) returns the
  same key the events carry: `correlation_key(task_id, read_issue_number(d), read_issue_repo(d),
  read_contract_key(d))`. When that is `af-…` and `provenance.issue_number` is set, it keeps
  today's `str(issue_number)`.
- **TFactory handoff:** in `apps/backend/pfactory/tfactory_client.py`, `_thread_issue` (~700) must
  produce the event key.
  - Replace `contract.setdefault("correlation_key", key)` with: keep the contract's own key only
    when it passes the same validator, mirrored in the backend module because it is stdlib-only;
    otherwise set `key`.
  - `_event_correlation_key` gains the contract-key precedence above. It must equal
    `completion.correlation_key` for the same inputs, and the existing pin test extends to that.

## Review (plan draft, 2026-11-27)

Codex (gpt-6.2-sol) and agy (model not reported) reviewed the draft. Both were read-only, and the
worktree was unchanged after each.

| Finding | Verdict | Change |
|---|---|---|
| A contract key can override an issue task's `<repo>#<n>` (Codex, high) | confirmed (spec precedence) | precedence: issue key, then contract key, then `af-` |
| `correlation_key()` doesn't validate, so `""` or a malformed key leaks (agy, high) | confirmed | a single `_valid_contract_key`, used inside `correlation_key` |
| `_thread_issue` `setdefault` keeps a bad, digits-only or empty contract key (agy, high) | confirmed (`tfactory_client.py:719-723`) | handoff validates and overwrites; the pin test is extended |
| The human-approval `done` emitter isn't traced (Codex) | refuted: `routes/tasks.py:492` → `emit_terminal_completion` → `completion.py:1227` | none |
| The misleading "no cross-service join" log (both) | confirmed (`completion.py:621-627`) | the log follows the resolved key |
| A first poll before the contract exists, or a queued old-key event, leaves a second row (Codex) | plausible, not verified | accepted; the live check asserts one `pf-` item with usage |
| The job-state test should assert the key passed to `admit()` (Codex) | confirmed | test extended |

## Steps

1. **Codex:** `server/services/completion.py` (validator, reader, precedence, builders, diagnostic)
   and `apps/backend/pfactory/tfactory_client.py` (`_thread_issue`, `_event_correlation_key`).

   **Tests** in `tests/test_completion_emitter.py`:
   - **Reader:** `read_contract_key`
     - returns a valid `pf-337-x`, `o/r#5` and an all-digits `42` (returned; `correlation_key`
       ignores it);
     - returns `None` for a missing file, invalid JSON, a list document, a non-str value, an empty
       string, 129 characters, an embedded space, a newline, and a leading `-`.
   - **Precedence:** the issue key wins over `contract_key`. `contract_key` wins over `af-`. A
     digits-only, empty or malformed key, or `None`, falls through.
   - **Builders:** each of the three event builders, given `contract_key`, emits it.
   - **Callers:** each of the four `spec_dir` callers puts the file's key into the event. Use the
     module's existing fixtures and the `_spec_with` helper.
   - **Handoff pin:** `_thread_issue` keeps a valid `pf-337-x`, and replaces `"42"`, `""`,
     `None` and `"bad key"` with the event key. It is checked with and without an issue in `req`,
     and the result equals `completion.correlation_key` for the same inputs.
   - **Diagnostic log:** silent for a `pf-` key, and still logged for an `af-` key.

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
     contract key is digits and `provenance.issue_number` is 5. The key handed to
     `_store().admit(...)` is asserted too.

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

## Deviations (implementation)

- **Step 1 (Codex): the handoff key.** `_event_correlation_key` for a task with no issue now
  returns the valid contract key, or `af-<task_id>`. Before, it returned `None`. The handoff then
  carries the same key as AIFactory's events, so TFactory joins the AIFactory work item instead of
  minting `tf-…`. An unknown `task_id` still returns `None` (#1972).
- **Step 1 test list:** the item "the reader returns an all-digits `42`" contradicted the decision
  that the validator rejects digits. The decision was followed: the reader returns `None`.
- **Job-state row decision dropped (fresh Opus diff review, critical).** `_read_correlation_key`
  feeds `admit()`, then `scope_job_s3` and the artifact paths, not CFactory.
  - With the event key, an issue task gets `o/r#5`.
  - `job_s3_scope._BAD_ID` rejects `/`, so the build would not dispatch, and `#` truncates
    `s3://` URIs.
  - `agent_service.py` is reverted to `origin/dev`. A test pins the issue task's job-state key to
    `"5"`. CFactory joins on the events and the task list, which carry the contract key.
