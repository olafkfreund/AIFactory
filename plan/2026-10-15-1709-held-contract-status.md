---
status: approved
issue: 1709
spec: spec/2026-10-15-1709-held-contract-status.md
---

# Plan: show a held task contract on the task status

Self-contained. You do not need to open the intent or the spec.

**Problem.** A held task contract (#1673) is visible only as a
`[trusted-contract]` log line: `apps/backend/cli/build_commands.py:203-221`
(`_resolve_build_contract`: held migration logs then `sys.exit(1)`, other holds
log and run a normal build) and `apps/backend/agents/deploy_scaffold.py:91-95`
(deploy scaffold withheld). Nothing reaches the task status the UI/API show.

## Decisions (carried from the approved spec)

- **D1** New plan key `contractHold` (string) on the spec's
  `implementation_plan.json`; value = the existing log text without the
  `[trusted-contract] ` prefix. Only `reason_label` constants go in it. The plan
  file is used because packed Jobs push it back on every exit
  (`cli/main.py:526-532`).
- **D2** Never touch `status`, `reviewReason`, `planStatus` or subtasks, so a
  hold never renders or counts as a failure (UI, CFactory sync, metrics read
  only those).
- **D3** Writer `core.contract_trust.record_hold(spec_dir, note)`: never
  creates a plan; `None` removes the key; no write when unchanged; swallows
  `OSError`; never raises.
- **D4** Calls: `_resolve_build_contract` clears on non-hold, records on hold
  (before `sys.exit(1)` / `return None`); `scaffold_deploy_for_spec` records
  `"deploy scaffold withheld: <label>"` on hold.
- **D5** `task_service.task_to_dict` copies a string `contractHold` from the
  plan into the row; frontend `Task.contractHold?: string`; `TaskHeader` shows
  an `outline` badge "Contract held" with the note as tooltip, at any status.
- **D6** A held migration still exits 1 (approved #1673 decision, unchanged).

## Steps

### Step 1 — `record_hold` in `core/contract_trust.py`

File `apps/backend/core/contract_trust.py`. After `ENV`/`LEGACY` (line 16-17)
add `HOLD_KEY = "contractHold"`. After `_read_json` (line 49-53) add:

```python
def record_hold(spec_dir: Path, note: str | None) -> None:
    """Put a hold note on the task's plan for the task status (#1709); None clears it.

    Never creates a plan, never raises: the note is display-only.
    """
    plan_file = Path(spec_dir) / "implementation_plan.json"
    plan = _read_json(plan_file)
    if not isinstance(plan, dict) or plan.get(HOLD_KEY) == note:
        return
    if note is None:
        plan.pop(HOLD_KEY)
    else:
        plan[HOLD_KEY] = note
    with contextlib.suppress(OSError):  # strict-ruff SIM105
        plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")
```

Tests in `tests/test_contract_trust.py` (append): sets the key and keeps other
keys (`status`, `reviewReason`) untouched; `None` removes it; no plan file → no
file created; a non-object plan (`[]`) left as is; unchanged note → file mtime
not rewritten is not required, just no error.

Check: `cd <worktree> && apps/backend/.venv/bin/python -m pytest tests/test_contract_trust.py -q`
(use the main clone's venv on PATH).
Traps: `pop(HOLD_KEY)` is safe because `plan.get(HOLD_KEY) == None` returned
early when the key is absent. Keep `json` import (already there).

### Step 2 — call sites

File `apps/backend/cli/build_commands.py`, `_resolve_build_contract`
(lines 203-221). Import `record_hold` with the others
(`from core.contract_trust import reason_label, record_hold, trusted_contract`).
- `if state != "hold": record_hold(spec_dir, None); return held`
- migration branch: after building `msg`, `record_hold(spec_dir, msg.removeprefix("[trusted-contract] "))`
  before `sys.exit(1)`.
- normal-build branch: same, before `return None`.

File `apps/backend/agents/deploy_scaffold.py`, `scaffold_deploy_for_spec`
(lines 84-99): import `record_hold` in the line-24 import; in the hold branch
add `record_hold(spec_dir, msg.removeprefix("[trusted-contract] "))` before
`return []`.

Tests:
- `tests/test_contract_trust.py`: import `_resolve_build_contract` from
  `cli.build_commands`; write a contract with
  `{"migration": {...}}` as `is_migration` needs (check
  `core/migration_mapper.is_migration`) plus a plan
  `{"status": "in_progress", "reviewReason": None-free}`:
  (a) ENV=`hold`, migration contract → `SystemExit(1)` and the plan has
  `contractHold == "migration contract held (held by server); build stopped"`,
  `status` unchanged, no `reviewReason` added;
  (b) ENV=`hold`, non-migration contract → returns None, note
  `"contract held (held by server); normal build"`;
  (c) a plan carrying an old note, ENV = `contract_digest(C)` → contract
  returned and `contractHold` gone.
- `tests/test_deploy_scaffold.py::test_scaffold_for_spec_withheld`: write a
  plan `{"phases": []}` into the spec dir first and assert
  `contractHold` starts with `"deploy scaffold withheld: "`.

Check: `python -m pytest tests/test_contract_trust.py tests/test_deploy_scaffold.py tests/test_trusted_contract_isolation_stamp.py -q`.
Traps: `cli.build_commands` imports may need `apps/backend` on `sys.path`
(test file already inserts it). Do not change the exit code or messages.

### Step 3 — `task_to_dict`

File `apps/web-server/server/routes/task_service.py`, `task_to_dict`
(lines 1264-1345). Inside the existing `with contextlib.suppress(...)` block
that reads the plan (lines 1289-1296) add:

```python
                        if isinstance(plan.get("contractHold"), str):
                            hold_note = plan["contractHold"]
```

with `hold_note = None` initialised next to `specs_path = None`, and after the
`result = {...}` dict: `if hold_note: result["contractHold"] = hold_note`.

Test in `tests/test_task_service_module.py` (pattern of
`test_task_rows_declare_the_correlation_key`): a spec with a plan carrying
`contractHold` → row has it; a spec without → key absent.

Check: `python -m pytest tests/test_task_service_module.py -q`.
Traps: `plan` may be a non-dict JSON value; guard with `isinstance(plan, dict)`
if the existing code does not (it calls `"archivedAt" in plan`).

### Step 4 — frontend

- `apps/frontend-web/src/shared/types/task.ts`, `interface Task` (line ~298):
  `contractHold?: string;  // Why the task contract was held (#1709); display only`.
- `apps/frontend-web/src/components/task-detail/TaskHeader.tsx`: in the badge
  row (after the status/stuck block, inside `<div className="mt-2 flex ...">`)
  add, using the already-imported `Tooltip`/`Badge`:

```tsx
{task.contractHold && (
  <Tooltip>
    <TooltipTrigger asChild>
      <Badge variant="outline" className="text-xs">Contract held</Badge>
    </TooltipTrigger>
    <TooltipContent side="bottom" className="max-w-xs">
      <p className="text-sm">{task.contractHold}</p>
    </TooltipContent>
  </Tooltip>
)}
```

Check: `cd apps/frontend-web && npx tsc --noEmit -p .` and
`npx vitest run src/components/task-detail`.
Traps: neutral `outline` variant only, never `destructive`/`warning`; do not
touch `reviewReasonBadge` or `REVIEW_REASON_BADGES`.

### Step 5 — CHANGELOG

`CHANGELOG.md` under `## [Unreleased]`, a `### Added` (or existing section)
bullet: held task contract now shows on the task (#1709).

## Tests (summary)

Steps 1-3 add pytest cases; the regression proof is step 2 (a), which fails on
`dev` (no `contractHold` written). Run also `ruff check` and
`ruff format --check` on the touched Python files, and the ratchet if the repo
has one for these paths.

## Rollback

Revert the PR. The key is additive; old plans with a `contractHold` key are
ignored by every reader.
