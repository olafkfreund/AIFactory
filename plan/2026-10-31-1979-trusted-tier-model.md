---
status: approved
issue: 1979
spec: spec/2026-10-31-1979-trusted-tier-model.md
coders:
  1: codex   # lane brief: Codex is this lane's coder
  2: codex
  3: codex
---

# Plan: tier stamp and subtask model from the run-start snapshot

Approved decisions (from the spec):
- Reuse `RunGates` (`apps/backend/run_gates.py`, #1983) and its
  `approved_plan` (#1940). No new snapshot mechanism.
- Tier stamp: `record_turn` takes `routing_metadata` from the caller
  (`gates.metadata`), never reads `task_metadata.json` itself.
- Subtask model: `RunGates.subtask_model(subtask_id, live_model)`.
  `approved_plan is None` → `live_model`; else the snapshot subtask's `model`
  string, or `None` (phase default) when absent. Never the live file.

## Steps

1. `apps/backend/run_gates.py`: add method `subtask_model(self, subtask_id: object, live_model: object) -> str | None`
   per the decision above (walk `approved_plan.get("phases")` list → each
   dict's `subtasks` list → dict with `id == subtask_id`; return its `model`
   when it is a non-empty str). Add unit tests in
   `tests/test_run_gates_snapshot.py` (None snapshot returns live; present;
   absent/without model → None).
   → verify by `cd apps/backend && .venv/bin/python -m pytest ../../tests/test_run_gates_snapshot.py -q`
   Traps: keep the dataclass frozen; mypy ratchet (`scripts/ratchet_lint.py`).
2. `apps/backend/agents/token_attribution.py` `record_turn` (~463, ~505-517):
   add kw-only `routing_metadata: Mapping[str, Any] | None = None`; delete the
   lazy `load_task_metadata(spec_dir)` block; pass `dict(routing_metadata)` if
   given else None where `routing_metadata` is used today. Pass
   `routing_metadata=gates.metadata` in `agents/coder.py` (~827) and
   `agents/parallel_integration.py` (~653). Add a source test next to
   `test_both_producers_pass_a_duration` in `tests/test_worker_duration_ms.py`
   (or a new parametrized test in `tests/test_run_gates_snapshot.py`) that both
   producers pass `routing_metadata=`.
   → verify by `pytest tests/test_token_attribution.py tests/test_worker_duration_ms.py apps/backend/test_routing_policy.py tests/test_model_fallback_visible.py tests/test_live_usage_marker.py -q`
   Traps: token accounting must never raise.
3. `apps/backend/agents/coder.py` (~463) and
   `apps/backend/agents/parallel_integration.py` (~594): use
   `gates.subtask_model(...)` as in the spec. Extend
   `test_serial_coder_second_session_ignores_metadata_forged_by_the_first`
   style tests in `tests/test_run_gates_snapshot.py`: with
   `dataclasses.replace(gates, approved_plan=<plan without model>)` and a live
   subtask declaring `model: "haiku"` the client gets the phase model; with
   the snapshot declaring `haiku` it gets haiku; and `record_turn` was called
   with `routing_metadata == gates.metadata` after `task_metadata.json` was
   forged. Cover both serial and parallel.
   → verify by `pytest tests/test_run_gates_snapshot.py -q`
   Traps: #1988 local-only test-order failure in `test_git_metadata_baseline`
   is not ours.

## Tests

`ruff check` + `ruff format --check` on touched files,
`python scripts/ratchet_lint.py` if present, and the pytest commands above:
all green.

## Rollback

Revert the PR; behaviour returns to reading the live files.
