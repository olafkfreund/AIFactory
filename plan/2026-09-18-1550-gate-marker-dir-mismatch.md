---
status: approved
issue: 1550
spec: spec/2026-09-18-1550-gate-marker-dir-mismatch.md
---

# Plan: One gate marker, resolvable from anywhere

## Decisions (carried from the approved spec)

- The marker stays at `<spec_dir>/.trailing_gates_done`, first line = the gated commit sha.
- **Readers bind to the task branch**, not a working copy. The current sha is
  `git rev-parse aifactory/<spec>` in `project_dir` (branch naming from
  `core/worktree.py:687`). If that ref cannot be resolved, fall back to today's
  `gate_dir_for` HEAD check, so the local path is unchanged.
- **The marker leaves the Job** through the same object-store channel as the plan.
  - Clarification of "same channels as the plan": the marker is pushed once at build end
    beside `maybe_push_usage` (`cli/main.py:556`) and fetched in `completion.py:975`,
    rather than on every `publish_plan` tick. The marker is written once per build, so a
    per-tick push buys nothing.
  - It is also added to `agent_worktree_sync.files_to_sync` for in-pod builds.
- #1496's guarantee holds: any "cannot find / cannot read" path returns **no evidence**.
  "Cannot verify → current" remains only for a sha of `-` (non-git checkout), as documented.

## Steps

1. `apps/backend/agents/gate_runner.py`: add
   `_task_branch_sha(project_dir: Path, spec_name: str) -> str | None` that runs
   `git rev-parse --verify --quiet refs/heads/aifactory/<spec_name>` with the same
   error/timeout handling as `_current_head_sha`.
   → verify: unit test with a temp git repo (branch present → its sha; absent → `None`).
2. Same file: give `trailing_gate_marker_is_current(spec_dir, gate_dir, project_dir=None)`
   a keyword-only `project_dir`. When it is given and `_task_branch_sha` resolves, compare
   the recorded sha with that. Otherwise keep the existing HEAD-of-`gate_dir` logic.
   `trailing_gate_evidence` passes its `project_dir` through.
   → verify: existing `tests/test_gate_runner.py` passes unchanged. New tests: a marker at
   commit X with no worktree directory is current when the branch is at X, and stale when
   the branch has advanced.
3. `apps/backend/agents/tools_pkg/tools/qa.py:253-254`: pass `project_dir=project_dir` into
   `trailing_gate_marker_is_current`. `agents/coder.py:1279` is the same.
   → verify: `tests/test_qa_approval_needs_gate_evidence.py` and
   `tests/test_coder_trailing_gates.py` pass.
4. `apps/backend/core/workspace_fetch.py`: add `_GATE_MARKER_FILE = ".trailing_gates_done"`,
   `_gate_marker_key(spec_id)` (same derivation as `_usage_key`), and
   `maybe_push_gate_marker(spec_dir, spec_id)` / `maybe_fetch_gate_marker(spec_dir, spec_id)`,
   copied from the `maybe_push_usage` / `maybe_fetch_usage` pair. They are no-ops off the
   packed path, never raise, and use content type `text/plain`. Fetch **overwrites**, like
   `maybe_fetch_plan`, because a stale control-plane copy must not win.
   → verify: unit tests mirroring the usage push/fetch tests (a no-op without `WORKSPACE_URI`;
   a round-trip with a fake `ArtifactStore`).
5. `apps/backend/cli/main.py:556`: call `maybe_push_gate_marker(spec_dir, spec_dir.name)`
   beside `maybe_push_usage`. `apps/web-server/server/services/completion.py:975`: call
   `maybe_fetch_gate_marker(spec_dir, spec_id)` beside `maybe_fetch_usage`.
   → verify: existing completion tests pass. New assertion: the fetch is called.
6. `apps/web-server/server/services/agent_worktree_sync.py:86`: add `".trailing_gates_done"`
   to `files_to_sync`.
   → verify: the sync test (or a new one) copies the marker.
7. `apps/web-server/server/services/merger.py`: no code change needed, because it already
   calls `trailing_gate_evidence(spec_dir, project_path)`, which now binds via the branch.
   Add a test: a marker for the branch tip with no worktree directory → the PR body carries
   the gate line.
   → verify: `tests/test_merge_auto_merger.py` plus the new test pass.
8. **During implementation, check** whether any build path besides packed and in-pod
   still runs (a PVC co-mount Job). If one does and has no copy-back, record it in this
   plan in the same commit, per the workflow.

## Tests

```bash
apps/backend/.venv/bin/pytest tests/test_gate_runner.py tests/test_coder_trailing_gates.py tests/test_qa_approval_needs_gate_evidence.py tests/test_merge_auto_merger.py -v
apps/backend/.venv/bin/pytest tests/ -k "workspace_fetch or completion or worktree_sync" -q
```
Live check after deploy: one packed-path build on the cluster. The merger's PR body shows
`Gate evidence: <summary>` instead of "no verification gates recorded".

## Rollback

Revert the implementation commit. Object-store keys written for the marker are inert
without the fetch. Markers on disk keep their format (first line = sha), so older code
still reads them.
