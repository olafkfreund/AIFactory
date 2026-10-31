---
status: approved
issue: 1984
spec: spec/2026-10-31-1984-job-pushback-trust.md
coders:
  1: agy  # wave brief: agy is this team's coder
  2: agy
  3: agy
  4: agy
---

# Plan: refuse gated tasks on subprocess, and never trust what a build Job pushes back

Approved decisions (from the spec; owner decision on #1932 is option B, agents
only in Jobs):

- Slice B: on the subprocess backend (raw `AIFACTORY_BUILD_BACKEND` unset or
  `subprocess`), refuse a task whose control-plane `task_metadata.json` has
  `requireReviewBeforeCoding: true`, `mode: "quick"` or `reviewTier:
  "blocking"`, or whose `mode` argument is `"quick"`. Unparseable metadata is
  refused; missing metadata is allowed. Raised as `GatedTaskRefusedError
  (InputRejectedError)` from `start_task_execution` (before admission) and
  `start_spec_creation` (next to the #1942 check). Message contains "this task
  needs the kubejob backend".
- #1984 rows (see spec table): 1 review pause recomputed (claim necessary,
  never sufficient); 2 memory archive extracts only `memory/` files and dirs;
  3 `load_task_contract` stops falling back to `implementation_plan.json`;
  4-5 merger PR body labels QA sign-off and gate evidence as build-reported;
  6-9 unchanged (existing tests cover usage bounds and task_logs keep-local).
- No HMAC.

## Steps

1. Slice B.
   - `apps/web-server/server/services/build_backend.py` after
     `BuildBackendUnavailableError` (~377): add `GatedTaskRefusedError
     (InputRejectedError)` (import from `server.error_ref`) and
     `subprocess_gate_refusal(spec_dir: Path | None, mode: str | None = None)
     -> str | None` using `_raw_backend()`.
   - `apps/web-server/server/services/agent_service.py`
     `start_task_execution` (~842, after the "already queued" guard):
     `if problem := subprocess_gate_refusal(spec_dir_for(project_path, spec_id), mode): raise GatedTaskRefusedError(problem)`.
   - `apps/web-server/server/services/agent_spec_creation.py`
     `start_spec_creation` (~86-97): same check once `spec_dir` is known.
   - New `apps/web-server/tests/test_subprocess_gate_refusal.py`: unit tests of
     the helper (three gates, quick arg, unparseable, missing, kubejob env) and
     one test per call site that the refusal raises before any spawn.
   → verify: `cd apps/web-server && ../backend/.venv/bin/python -m pytest tests/test_subprocess_gate_refusal.py -q`
   Traps: the routes catch `BuildBackendUnavailableError` and re-raise; the
   new error must NOT subclass it (that maps to a fixed 503 text).

2. Row 1, review pause. `apps/web-server/server/services/agent_kubejob.py`
   `_kubejob_review_reason` (~78-110): after reading the claim, return it only
   when `_pause_confirmed(spec_dir, project_path, reason)` (new sync helper,
   run in `asyncio.to_thread`) is true; else log at warning that the Job's
   claim was ignored and return None. `_pause_confirmed`:
   - `approved = ReviewState.load(spec_dir).is_approval_valid(spec_dir)`
     (`review.state`, lazy import);
   - `plan_review`: `requires_review_before_coding(spec_dir) and not
     is_solo_mode_enabled_for_spec(spec_dir) and not approved`;
   - `injection_scan`: `run_scan_gate(spec_dir, tree).blocks and not approved`,
     `tree` = `project_path/.aifactory/worktrees/builds/<spec_id>` if it is a
     dir, else `project_path`;
   - any exception → False.
   Tests in `tests/test_agent_service_kubejob_backend.py`: the existing pause
   tests (`test_kubejob_review_pause_is_not_completion`,
   `test_kubejob_preflight_skeleton_plan_pauses`, `_PAUSED` users) write
   control-plane metadata `{"requireReviewBeforeCoding": true}` and, for
   `injection_scan`, a `spec.md` matching a scan pattern; new tests: a forged
   `plan_review` / `injection_scan` claim without that state runs the
   completion (`rtc` awaited, no `kubejob_review_pause` control); a claim
   with a valid approval is ignored.
   → verify: `../backend/.venv/bin/python -m pytest tests/test_agent_service_kubejob_backend.py -q` from the repo root with `apps/backend/.venv/bin/python -m pytest`.
   Traps: `_kj` fixture monkeypatches `resolve_project_path`; the review
   state helpers live on the backend path.

3. Row 2, memory. `apps/backend/core/workspace_fetch.py` `maybe_fetch_memory`
   (~703-712): pass `members=` only those with `name == "memory"` or
   `name.startswith("memory/")` and `isfile() or isdir()`; keep
   `filter="data"` on the same `tar.extractall(` line (pinned by
   `test_the_extraction_filter_is_pinned_explicitly`).
   Test in `tests/test_memory_packed_path.py`: a forged archive with
   `task_control.json`, `review_state.json`, `context/task_contract.json` and
   a symlink `memory/x -> ../task_control.json` writes none of them while a
   real `memory/` file lands.
   → verify: `apps/backend/.venv/bin/python -m pytest tests/test_memory_packed_path.py -q`

4. Rows 3-5.
   - `apps/backend/pfactory/tfactory_client.py` `load_task_contract` (~74-103):
     read only `context/task_contract.json`; update the docstring.
   - `apps/web-server/server/services/merger.py` `honest_pr_title_and_body`
     (~277-290): lines become `- Gate evidence (reported by the build, not
     verified by the control plane): ...` and `- QA sign-off (reported by the
     build): ...`. Update `apps/web-server/tests/test_merger.py` asserts
     (~221, 251, 276, 286) to the new prefix.
   - Tests: forged `tfactory` block in the plan → `load_task_contract == {}`
     (`apps/backend/pfactory/test_tfactory_contract_source.py`, new);
     `test_merger.py` asserts the label.
   → verify: `apps/backend/.venv/bin/python -m pytest apps/backend/pfactory apps/web-server/tests/test_merger.py apps/web-server/tests/test_trusted_contract_merge.py -q`
   Traps: `test_trusted_contract_merge.py` legacy tests must still pass.

## Tests

- Each step's check above, then `ruff check` and `ruff format --check` on the
  touched files, `python scripts/ratchet_lint.py` if present, and the
  pre-commit hook on every commit (never skipped).
- Known local-only noise: `test_github_env` x2, `test_marker_nonce`.

## Rollback

Revert the PR. No data, schema, config or deploy change.
