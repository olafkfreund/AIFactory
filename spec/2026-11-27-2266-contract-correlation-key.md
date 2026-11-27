---
status: approved
issue: 2266
intent: intent/2026-11-27-2266-contract-correlation-key.md
---

# Spec: a trusted-plan task reports under its contract's correlation key

## Design

### One reader for the contract key

Add `read_contract_key(spec_dir: Path) -> str | None` to
`apps/web-server/server/services/completion.py`, next to `read_issue_number` (~190).

- It reads `<spec_dir>/context/task_contract.json` and returns `correlation_key` when that is a
  `str` matching `_CONTRACT_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}")`, tested with
  `fullmatch`.
- It returns `None` on a missing file, invalid JSON, a non-dict document, or a bad value.
- It never raises.
- A purely numeric key is returned as is. That is the legacy issue-number form, and the
  numeric-issue paths already handle it.

### `correlation_key()` prefers it

`correlation_key(task_id, issue_number, repo=None, contract_key=None)`:
- **Precedence:** a valid `contract_key` that is not all digits wins. Then `<repo>#<n>`, then
  `af-<task_id>`, as today.
- **Digits:** an all-digits contract key keeps today's behaviour. The issue number already
  carries it via `provenance.issue_number`, and a bare number must never be a key (#1960).

### Every emitter passes it

- **Event builders:** `build_completion_event` (~565, key at ~630), `build_worker_event` (~687,
  ~737) and `build_worker_progress_event` (~767, ~813) gain a keyword `contract_key: str | None =
  None`. It is forwarded to `correlation_key()`.
- **Callers:** every caller that has a `spec_dir` passes `contract_key=read_contract_key(spec_dir)`.
  These are the four sites next to `read_issue_number(spec_dir)` (~865, ~966, ~1231, ~1300).
- **Task list:** `routes/task_service.py:1287` passes `contract_key=read_contract_key(spec_dir)`.
- **Job-state row:** `services/agent_service.py:_read_correlation_key` (~295) returns
  `read_contract_key(spec_dir)` first when it is non-numeric. Otherwise it returns today's
  `provenance.issue_number`.

### Already correct (no change)

The TFactory handoff, `pfactory/tfactory_client._thread_issue`, already lets the contract's own
key win.

## Alternatives rejected

- **Store the key in `requirements.json` provenance at install-plan** (`routes/execution.py`
  ~1747, ~1796): this adds a second copy of the same value. `task_contract.json` already holds it
  for every trusted-plan task, and the handoff already reads it there.
- **Read the signed copy from `TrustedContractStore`:** it is async and DB-backed. The task list
  and event emitters are sync and file-based. The key is an observability join, not an
  authorization input, so a validated file read is enough. See Risks.
- **Fix it in CFactory by aliasing the keys:** CFactory can't know that `af-<task>` and `pf-…`
  are the same run without AIFactory saying so.

## Risks

- **An agent edits the key:** `task_contract.json` is writable by the build agent. A forged key
  can only re-file that task's events under another work item. It can't affect merge gates,
  which read the trusted store, or authorization. The regex blocks injection.
- **Work items in flight re-key:** a task that is already running switches from `af-…` to
  `pf-…` on its next event. CFactory's `prune_duplicate_stages` then moves the stage to the
  `pf-` row. That is the intended join.

## Verification

| Case | Expected |
|---|---|
| contract `pf-337-x` and no issue | completion, worker and progress events, the task list and the job-state row all use `pf-337-x` |
| contract `pf-337-x` plus issue `repo#5` | `pf-337-x` |
| contract key all digits (`42`) and repo `o/r` | `o/r#42` (unchanged) |
| no contract file, bad JSON, key too long, or a key with a space or newline | today's key |
| no contract and no issue | `af-<task_id>` (unchanged) |

Also:
- the existing `tests/test_completion_*.py`, the task-list tests and the agent-service tests
  pass;
- the ruff and mypy ratchets are clean, and CI is green;
- live, after the release: a P1 E2E run's CFactory work item `pf-…` carries AIFactory events and
  usage.
