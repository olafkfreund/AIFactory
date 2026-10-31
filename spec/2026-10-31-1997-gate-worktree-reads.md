---
status: approved
issue: 1997
intent: intent/2026-10-31-1997-gate-worktree-reads.md
---

# Spec: Remaining agent-writable reads in the run-start gate snapshot

## Design

All changes are in `RunGates.capture` (`apps/backend/run_gates.py`).

1. **Review-off flags with no source spec dir.**
   `fail_closed = source_spec_dir is None and bool(os.environ.get("AIFACTORY_TASK_ID"))`.
   - When `fail_closed` is true, `solo = False` and `requires_review = True`;
     the code doesn't read `requireReviewBeforeCoding`, `mode` or `soloMode`.
     It logs one warning.
   - When `source_spec_dir` is None and `AIFACTORY_TASK_ID` is unset (the
     CLI, including `--direct`), it reads the flags as today and logs one
     warning.
   - Why `AIFACTORY_TASK_ID`: the web server sets it unconditionally on both
     dispatch paths, `agent_service.py` (subprocess) and `build_backend.py`
     (kubejob extra_env), since #1972. The backend already reads it
     (`agents/parallel_integration.py`). It is set by the process that
     spawned `run.py`, so the agent can't change it for the current run.
   - Server paths that reach `source_spec_dir=None`: kubejob always passes
     `--auto-continue`, which means isolated mode. The subprocess backend
     passes `--auto-continue` unless the API request sets
     `auto_continue=false`. Then `choose_workspace` shows its menu on the PTY,
     and "direct" leaves `source_spec_dir` None (`cli/build_commands.py`). The
     web server never passes `--direct`.
2. **Approved-plan snapshot.** `_approved_plan_snapshot(gate_dir, ...)`
   replaces `_approved_plan_snapshot(spec_dir, ...)`. It is still read once,
   inside `capture`, before the first agent session. `gate_dir` is the source
   spec dir, the same dir the solo, review and approval state come from.
3. **Phase betas and fastMode:** no change; see Not changed.

## Not changed

- **CLI `--direct` (and any run without `AIFACTORY_TASK_ID`):** the flags are
  still read from the spec dir, with a warning. The user runs this mode
  locally, and by design it has no isolation: the agent writes the real
  project anyway.
- **`get_phase_model_betas` and `get_fast_mode`** (`phase_config.py`): neither
  has a production caller (`git grep` on dev finds only their definitions and
  tests), so no `RunGates` is in scope. Left as is. If a caller is ever
  added, it should read `RunGates.metadata`.
- **Model and thinking metadata on the fail-closed path** still come from the
  run's spec dir. With review required, grants come from the approved
  snapshot, but `pinnedModel` and similar settings are out of scope for this
  issue.

## Alternatives rejected

- **Fail closed whenever `source_spec_dir` is None:** this breaks local CLI
  `--direct` users who have review off. The lead decided against it.
- **Reading the flags from the control plane:** this needs a new
  control-plane read and a dispatch contract. It is larger than the risk on a
  path that needs `auto_continue=false` plus a menu choice.
- **Telling the paths apart by heuristic** (PTY, `CI=true`, path layout):
  the constraints rule it out.

## Risks

- A build started by a web server older than #1972 has no
  `AIFACTORY_TASK_ID`. It behaves like the CLI: today's reads plus a warning.
- An operator who sets `AIFACTORY_SOLO_MODE` on the server loses solo mode on
  the fail-closed path. That path is already off the default flow.
- In isolated mode the source plan is synced from the worktree after each
  agent session (`sync_plan_to_source`). At run start, the source copy is the
  one the approval was given against.

## Verification

Unit tests in `tests/test_run_gates_snapshot.py`:

- With `AIFACTORY_TASK_ID` set and no source dir, review-off and solo
  metadata in the spec dir give `requires_review=True` and `solo=False`.
- Without `AIFACTORY_TASK_ID` and with no source dir, the same metadata is
  honoured and one warning is logged.
- `approved_plan` comes from the source dir, not from a differing worktree
  plan. Editing either plan after `capture` changes nothing.
- The existing suite stays green. Ruff and the mypy ratchet pass on
  `run_gates.py`.
