---
status: draft
issue: 1979
intent: intent/2026-10-31-1979-trusted-tier-model.md
---

# Spec: tier stamp and subtask model from the run-start snapshot

## Design

Chosen (the user's decision, the issue's Fix): read both values from what
#1983 `RunGates` (`apps/backend/run_gates.py`) froze at run start.

1. **Tier stamp.** `record_turn` (`agents/token_attribution.py`) gains a
   keyword `routing_metadata: Mapping[str, Any] | None = None` and no longer
   calls `load_task_metadata(spec_dir)`. Both producers pass
   `routing_metadata=gates.metadata`, the task metadata `RunGates.capture`
   read from the source spec dir:
   - `agents/coder.py` serial `record_turn(...)` (~827)
   - `agents/parallel_integration.py` wave `record_turn(...)` (~653)
   A caller that passes nothing gets no contract routing block, which is the
   same as a task with no `task_metadata.json`: env policy still stamps.
2. **Subtask model.** New method
   `RunGates.subtask_model(subtask_id, live_model) -> str | None`:
   - `approved_plan is None` (plan review off; no snapshot, same rule as
     grants): return `live_model` (unchanged behaviour).
   - otherwise: return the `model` string of the subtask with that id in
     `approved_plan["phases"][*]["subtasks"][*]`, or `None` when the subtask
     is absent or has no model. `None` means the phase default.
   Callers:
   - `agents/coder.py` ~463: `override = gates.subtask_model(next_subtask.get("id"), next_subtask.get("model"))`; `if override: phase_model = override` (planning still never overridden).
   - `agents/parallel_integration.py` ~594: `sub_model = gates.subtask_model(subtask.id, getattr(subtask, "model", None)) or phase_model`.

## Alternatives rejected

- New snapshot of `task_metadata.json` / plan inside `token_attribution`:
  the user ruled out a new snapshot mechanism; `RunGates` already holds both.
- Keep `load_task_metadata(spec_dir)` as a fallback when the kwarg is
  omitted: leaves the agent-writable read in place for any future caller.
- Fall back to the live plan's model for subtasks missing from the snapshot:
  that is exactly the hole; the user chose the phase default.
- Snapshot the plan even when review is off: no approval exists to trust, the
  plan itself is agent-authored in that mode; out of scope, grants behave the
  same way.

## Risks

- A subtask added to the plan after approval (re-planning) runs on the phase
  default model instead of its declared cheaper model: cost, not correctness.
- Tier stamp now depends on callers passing metadata; both producers are
  covered by a source-level test.

## Verification

- `tests/test_run_gates_snapshot.py`: serial coder and parallel wave each
  run with a live plan subtask `model: "haiku"` not in the approved snapshot →
  client created with the phase model; with the model in the snapshot → that
  model; `record_turn` receives `routing_metadata == gates.metadata` while the
  worktree `task_metadata.json` was forged.
- Unit test of `RunGates.subtask_model` (None snapshot, present, absent).
- Source test: both producers pass `routing_metadata=` to `record_turn`.
- `ruff`, `scripts/ratchet_lint.py`, targeted pytest. Known local-only
  failure #1988 (`test_git_metadata_baseline` QA tests) is not ours.
