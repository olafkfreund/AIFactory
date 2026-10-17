---
status: approved
issue: 1737
spec: spec/2026-10-17-1737-gate-pod-diagnostics.md
---

# Plan: name the pod phase when a gate Job fails or times out

Self-contained. You do not need to open the intent or the spec.

**Problem.** `KubeJobSandbox._run_async` (`apps/backend/core/kube_sandbox.py:402-474`)
deletes the gate Job in its `finally`, so when a gate times out nobody can
see whether the pod was still pulling its image, initialising or running.

**Decisions.** Read the pod state from the pod object the code already
lists (`core.list_namespaced_pod`, `:441`) before the delete; no new API
call, no RBAC change. Only on a non-success result. Diagnostics never
change `succeeded`/`exit_code`; any error is logged and skipped. PR uses
`Refs #1737`, not `Closes`.

## Steps

1. `apps/backend/core/kube_sandbox.py`, after `_exit_code_from_pod` (`:349`):
   add `_pod_state_summary(pod: Any) -> str`.
   (Deviation: `Any`, not `object`, so mypy accepts the attribute reads;
   `Any` is already imported.)
   - `parts = [f"pod {pod.status.phase}"]` (skip when phase is None).
   - For each status in `(pod.status.init_container_statuses or []) +
     (pod.status.container_statuses or [])`: take `state.waiting` else
     `state.terminated`; if it has a `reason`, add
     `f"{cs.name}: waiting {reason}"` / `f"{cs.name}: terminated {reason}"`,
     plus ` ({message[:200]})` when there is a message.
   - Return `"; ".join(parts)`. Wrap the body in `try/except Exception`:
     `logger.warning("[kube-sandbox] pod state unavailable: %s", exc)`, return `""`.

   In `_run_async` (`:444-465`): set `diag = "no pod found"` before
   `if pods.items:`; inside it, after `_exit_code_from_pod`, set
   `diag = _pod_state_summary(pod)`. After building `text` (including the
   `[job ...]` prefix), `if not succeeded and diag: text = f"{text} [{diag}]"`.
   → verify by `apps/backend/.venv/bin/pytest tests/test_kube_sandbox.py -q`
   Traps: `getattr` style is not needed, the try/except covers missing
   attributes. Do not reformat other code. Keep the `# noqa: BLE001` style
   on the broad except.

2. `tests/test_kube_sandbox.py`: add tests.
   - `_pod_state_summary` on a SimpleNamespace pod with phase `Pending`, an
     init container terminated `Completed`, a container waiting
     `ErrImagePull` with a message: string contains all three; a broken
     object (`SimpleNamespace()`) returns `""`.
   - `_run_async` via `_run_seeded`: make `_FakeCoreApi` take optional
     `items` (default `[]`); a failed DeadlineExceeded Job with a pending pod
     whose container waits `ErrImagePull` gives `ok is False` and output
     containing `ErrImagePull`; a succeeded Job with no pod has no
     `no pod found` in the output.
   → verify by `apps/backend/.venv/bin/pytest tests/test_kube_sandbox.py -q`,
   and the run test fails against the unfixed file.
   Traps: `_run_seeded` patches `asyncio.sleep`; pod log read must be on
   the fake core (`read_namespaced_pod_log`), raise or return "".

## Tests

- `apps/backend/.venv/bin/pytest tests/test_kube_sandbox.py tests/test_gate_runner_nix.py -q`: all pass.
- `ruff check` and `ruff format --check` on the two files: clean.

## Rollback

Revert the commit; no config or data changes.
