---
status: draft
issue: 1550
intent: intent/2026-09-18-1550-gate-marker-dir-mismatch.md
---

# Spec: One gate marker, resolvable from anywhere

## Findings (why the marker is read from the wrong tree)

1. **The marker is bound to a working copy, not a commit.** `write_trailing_gate_marker`
   records `git rev-parse HEAD` of `gate_dir_for(spec_dir, project_dir)`
   (`agents/gate_runner.py:686-736`). A reader recomputes `gate_dir_for` on *its* side.
   In the web-server, the task worktree often does not exist, so `gate_dir_for` falls back
   to `project_dir` (main's HEAD). The sha then never matches, and the evidence reads as
   stale.
2. **On the packed Job path the marker never leaves the Job.** The Job's `/work` is an
   ephemeral emptyDir (`agents/utils.py:229-253`, #1228). `.trailing_gates_done` is not
   published like `implementation_plan.json` is (`publish_plan`), and it is not in
   `agent_worktree_sync.files_to_sync` either. The merger (`services/merger.py:123-145`)
   therefore reads a source spec dir that has no marker.

## Design

Recommended answer to the intent's open question: **both halves**. The layout-aware
`gate_dir_for` alone cannot help on the packed path, because the Job's tree is gone by
the time the merger runs.

1. **Bind evidence to the task branch, not a directory.** The marker keeps its first line
   (the gated commit sha). The reader stops comparing it with a working copy's HEAD and
   instead resolves `git rev-parse <task branch>` in the project repo (the branch is
   `aifactory/<spec>`, from `core/worktree.py:687`). This is correct for local, linked-worktree
   and Job builds, because all of them push or merge their commits into that branch.
   Fallback when the branch ref cannot be resolved: keep today's `gate_dir_for` HEAD check,
   so the local path is unchanged.
2. **Get the marker out of the Job.** Add `.trailing_gates_done` to the same two channels
   the plan uses:
   - `sync_plan_to_source` / `publish_plan` (`agents/utils.py`), called right after
     `write_trailing_gate_marker` in `agents/coder.py:1404`;
   - `files_to_sync` in `services/agent_worktree_sync.py:86`.
3. **Keep #1496's guarantee.** Every "cannot find / cannot resolve" path still returns
   *no evidence*. The existing "cannot verify → True" branch in
   `trailing_gate_marker_is_current` stays scoped to non-git checkouts only, as documented.

## Alternatives rejected

- **Teach `gate_dir_for` the Job's nested layout only.** The Job's filesystem is gone
  before the merger runs, so there is nothing to point at.
- **Copy the marker back but keep HEAD-of-directory binding.** The copied marker would
  then be compared with the control plane's `project_dir` HEAD, which is the same false
  negative.
- **Store gate evidence in the DB.** It is a new schema and a new transport for a file
  that already has a publish path.

## Risks

- A branch that is later rebased or amended changes its tip. The evidence then correctly
  reads as stale, the same semantics as today's HEAD binding.
- `publish_plan` is best-effort, so a failed publish means the merger says "no gate
  evidence". That is the honest direction (a false negative), consistent with the intent.
- The QA guard (`tools_pkg/tools/qa.py:250-254`) uses the same reader and gains the same
  behaviour. Its existing tests must still pass unchanged.

## Verification

- Unit: a marker written against commit X is current when `aifactory/<spec>` points at X,
  even when no worktree directory exists. It is stale when the branch has moved on.
- Unit: the marker is included by `sync_plan_to_source` and `files_to_sync`.
- Existing: `tests/test_gate_runner*.py`, the #1496 QA-guard tests and `merger` tests pass.
- Live: one packed-path build on the cluster, and the merger's PR body shows the gate line.
