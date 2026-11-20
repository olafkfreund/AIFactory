---
status: approved
issue: 2172
spec: spec/2026-11-20-2172-aif-approved-branch-handoff.md
coders:
  1: codex   # small pure routing + contract validation logic in intake_poller and trusted_plan
  2: agy     # mechanical threading of handoff identity across execution.py, task_branch.py, and worktree setup
  3: codex   # tricky git ref adoption and fast-forward retry logic in core/worktree.py and build_backend.py
  4: codex   # security-sensitive PR endgame ready-for-review gate and auto-merge suppression
  5: codex   # security-sensitive TFactory fix-completed callback (configured origin, SHA dedup)
  6: agy     # comprehensive test suite additions across tests/ and apps/web-server/tests/
reviewers:
  - codex
  - agy
---

# Plan: Adopt the approved branch/PR, carry one handoff identity, and close the TFactory return loop (#2172)

Approved decisions (copied from spec, self-contained):
1. **Boundary with PFactory#1096:** PFactory owns the durable stage job (`intent` → `spec` → `plan`), reading `.factory.yml` from the trusted base branch, creating branch `factory/<n>-<slug>` and the draft PR, applying `factory:intent|spec|plan` labels, and verifying bound single-use `/approve <stage>` comments (`fetch_pr_comments` in `protocol.py:614`, immutable provider user ID, base-branch CODEOWNERS for the stage file else write access, exact user-ID lookup in `gitlab_provider.py:781`, bound to `PR id + stage + exact head SHA`). AIFactory owns forwarding `project_id` and `base_branch` on hard intake, adopting the approved branch/PR and handoff identity on `from-plan`, coding on that branch, handing off to TFactory, marking the draft PR ready for review (never auto-merging), transitioning `factory:building|verifying|done|needs-human` labels, and posting fix-completed to the configured TFactory origin.
2. **One handoff identity (`{project_id, repo, base_ref, source_branch, approved_head_sha, pr_number}`):** Carried inside the HMAC-signed contract (`provenance`), verified by `verify_plan_signature`, checked against the project repo (`_check_declared_repo`), persisted into `requirements.json`, `task_metadata.json`, and `.task_branch` (`record_branch`), and forwarded to TFactory (`build_ingest_payload`).
3. **Adopt existing branch & retry resume:** Validate `source_branch`/`base_ref` as safe git branch names (`source_branch != base_ref`) and `approved_head_sha` as 40-hex. Fetch `origin/<source_branch>` and verify its tip equals `approved_head_sha` (or descends from `approved_head_sha` on retry with recorded build commits); reject mismatch with 422. Check out `source_branch` in `WorktreeManager.create_worktree` and `_populate_self_contained_worktree` without `branch -D` so `intent/`, `spec/`, and `plan/` files stay intact. Push with `--force-with-lease`.
4. **Ready-for-review endgame (never auto-merge):** When `pr_number` is adopted, `run_pr_endgame` forces `auto_merge=False`, skips `create_pr`, attaches the PR to TFactory, waits for `tfactory_verdict(..., head=current_head) == "pass"`, runs `gh pr ready <pr>`, and transitions issue labels (`factory:building` → `factory:verifying` → `factory:done`, or `factory:needs-human`).
5. **TFactory return loop:** Ignore caller-supplied `tfactory_callback_url`. Persist `tfactory_task_id` and `pre_fix_sha` in `handback_received.json`. Run the QA Fixer on the task branch, commit and push to `origin/<task_branch>`, and call `send_fix_completed` to POST `{tfactory_task_id, project_id, spec_id, correlation_key, source_branch, head_sha, pr_number, status}` to `<TFACTORY_BASE_URL>/api/handback/aifactory-complete` with `X-TFactory-Handback-Token`. Suppress duplicate or unchanged `head_sha` via `handback_sent.json`.

Common repo traps: never use `as` aliases inside parenthesised imports (ruff sort disagreement); use absolute `server.*` imports in `apps/web-server/server/` (TID252 ratchet); never read a worktree's `HEAD` as the task branch without calling `resolve_task_branch` / `resolve_work_ref` in a sanctioned position (`apps/web-server/tests/test_no_worktree_head_as_branch.py`); never log tokens or unvalidated disk strings (py/log-injection).

## Steps

1. (codex) `apps/web-server/server/services/intake_poller.py:327-363` and `apps/backend/trusted_plan.py:597-760,773-877`:
   - In `intake_poller.py:349-362` (`_route_hard`), add `"project_id": cfg.project_id` and `"base_branch": cfg.base_branch` to the payload sent to `PFACTORY_INGEST_URL`.
   - In `trusted_plan.py:597-608`, add helpers `validate_handoff_identity(plan)` (validates optional `provenance.source_branch`, `provenance.base_ref`, `provenance.approved_head_sha` as `^[0-9a-fA-F]{40}$`, `provenance.pr_number` as positive `int`, and folds errors into `verify_trusted_plan` at `:472-493`) and extend `check_baseline_drift` (`:671-750`) to accept `spec_dir: Path | None = None`, verify `origin/<source_branch>` against `approved_head_sha` (exact match on initial run, ancestor match on retry when `memory/build_commits.json` is non-empty; else return `BaselineDrift(status="adopted_head_mismatch", ...)` which `baseline_drift_rejects` at `:752-760` rejects unless policy is `"off"`), and compare `baseline_commit` against `origin/<base_ref>` when `base_ref` is given.
   - In `trusted_plan.py:773-877` (`apply_execution_profile`), copy validated `provenance` handoff keys (`base_ref` → `base_branch`, `source_branch`, `approved_head_sha`, `pr_number`, `repo` → `github_repo`) into `task_metadata.json`.
   → verify by `apps/backend/.venv/bin/pytest tests/test_trusted_plan.py apps/web-server/tests/test_intake_poller.py -q`
   Traps: `verify_trusted_plan` must stay pure (no filesystem/git calls); git checks belong only in `check_baseline_drift`.

2. (agy) `apps/web-server/server/routes/execution.py:117-206,1362-1613`:
   - Add `base_ref: str | None = None`, `source_branch: str | None = None`, `approved_head_sha: str | None = None`, `pr_number: int | None = None` to `TaskProvenance` (`:151-182`) and `FromPlanRequest` (`:192-206`); update `ApplyCorrectionRequest.tfactory_callback_url` description (`:146-148`) to note it is ignored in favor of `TFACTORY_BASE_URL`.
   - In `create_from_trusted_plan` (`:1402-1613`), check that any request-level handoff field matches `request.plan["provenance"]` when both are present (422 on mismatch); validate `provenance.repo` via `_check_declared_repo` from `server.routes.from_issue` (`:225-251`); pass `effective_base = provenance.base_ref or request.baseBranch` to `start_task_execution` (`:1585`); when `source_branch` is present, call `record_branch(project_path, spec_id, source_branch)` (`server.services.task_branch:105`) before starting execution and transition issue label to `factory:building` (best-effort).
   → verify by `apps/backend/.venv/bin/pytest apps/web-server/tests/test_from_plan_route.py -q -o asyncio_mode=auto`
   Traps: `create_from_trusted_plan` is near ruff's `PLR0912` branch limit (`execution.py:1365`); put the handoff extraction/validation in a helper `_extract_plan_handoff(request, project_id, project_path)`.

3. (codex) `apps/backend/core/worktree.py:301-311,685-688,802-915`, `apps/backend/core/workspace/setup.py:226-305`, and `apps/web-server/server/services/build_backend.py:1356-1450`:
   - In `core/worktree.py`, read `source_branch` and `approved_head_sha` from `.aifactory/specs/<spec_name>/task_metadata.json` (or constructor args); `get_branch_name(spec_name)` (`:685-687`) returns `source_branch` when valid, else `f"aifactory/{spec_name}"`.
   - In `create_worktree(spec_name)` (`:802-915`), when adopting `source_branch`: fetch `origin <source_branch>` with `github_env(_git_env())`, verify `FETCH_HEAD` equals `approved_head_sha` (or descends from `approved_head_sha`), and run `git worktree add -B <source_branch> <worktree_path> FETCH_HEAD` under `_base_repo_git_lock()` without deleting the branch or resetting to `base_branch`.
   - In `build_backend.py:1356-1450` (`_populate_self_contained_worktree`), read `base_branch`, `source_branch`, and `approved_head_sha` from `source_spec_dir / "task_metadata.json"`; when `source_branch` is set, fetch `origin <source_branch>:refs/remotes/origin/<source_branch>` into `wt_path` and pass `source_branch` to `record_branch(project_path, spec_id, source_branch)`.
   → verify by `apps/backend/.venv/bin/pytest tests/test_worktree.py apps/web-server/tests/test_build_backend.py apps/web-server/tests/test_no_worktree_head_as_branch.py -q -o asyncio_mode=auto`
   Traps: `test_no_worktree_head_as_branch.py` scans `apps/web-server/server/` for `rev-parse --abbrev-ref HEAD` on worktree paths; use `recorded_branch` / `resolve_task_branch` in `server/`.

4. (codex) `apps/backend/pfactory/tfactory_client.py:264-272,342-429,664-794` and `apps/web-server/server/services/pr_endgame.py:801-864,1858-1976,2118-2328`:
   - In `tfactory_client.py:264-272`, update `_build_branch(spec_id, spec_dir=None)` to check `spec_dir / ".task_branch"` and `task_metadata.json["source_branch"]` before `f"aifactory/{spec_id}"`; pass `spec_dir` at all call sites (`:333,368,456,553,777`). In `_git_info_and_push` (`:400-407`), use `--force-with-lease` when pushing an adopted `source_branch`. In `build_ingest_payload` (`:664-794`), include `source_sha` (from `_locate_build_commit`), `base_ref` (`_task_base_branch(spec_dir)`), `pr_number` (from `task_metadata.json`), and `repo`.
   - In `pr_endgame.py`, add `mark_pr_ready(owner, repo, pr, *, runner=_default_runner) -> bool` (`gh pr ready <pr> --repo <owner>/<repo>`). In `gather_pr_context` (`:1858-1976`), read `pr_number` from `task_metadata.json` / `requirements.json["provenance"]` as `adopted_pr`. In `run_pr_endgame` (`:2118-2328`), when `adopted_pr` is set: force `auto_merge = False`, skip `create_pr` (push branch + update PR body evidence on `pr = adopted_pr`), attach PR to TFactory, poll `_build_merge_gate` for `tfactory_verdict == "pass"` at the exact PR head SHA, call `mark_pr_ready`, and transition issue labels (`factory:verifying` → `factory:done`, or `factory:needs-human` on failure).
   → verify by `apps/backend/.venv/bin/pytest apps/backend/pfactory/ apps/web-server/tests/test_pr_endgame.py apps/web-server/tests/test_pr_endgame_merge_gate.py -q -o asyncio_mode=auto`
   Traps: keep `run_pr_endgame` signature backward-compatible (`adopted_pr: int | None = None` optional keyword or read from `gather_pr_context` / `spec_dir`) so `test_fleet_provider_propagation.py` and `test_pr_endgame_review_tier.py` pass unchanged.

5. (codex) `apps/backend/qa/correction.py:63-108,151-327`, `apps/backend/pfactory/tfactory_client.py:930-945`, `apps/web-server/server/routes/execution.py:1615-1666`, `apps/web-server/server/services/agent_job_backend.py:363-500`, and `apps/backend/qa/correction_job.py:22-48`:
   - In `execution.py:1646-1654` and `qa/correction.py:63-108,226-327`, pass `tfactory_task_id=request.tfactory_task_id` into `apply_correction` and persist `tfactory_task_id` and `pre_fix_sha` in `handback_received.json` (never store or call `request.tfactory_callback_url`). Deduplicate if `(manifest_hash, pre_fix_sha)` matches an active/completed receipt.
   - In `agent_job_backend.py:363-410` (`_prepare_correction_workspace`), check out the task branch (`resolve_task_branch_fetching`) in the correction clone before packing; in `_await_correction` (`:437-461`) and `_run_fixer_bg` (`qa/correction.py:151-205`), commit and push the fix to `origin/<task_branch>` and invoke `send_fix_completed`.
   - In `tfactory_client.py`, add `send_fix_completed(spec_dir, spec_id, *, status="complete", poster=None) -> dict` which reads `TFACTORY_BASE_URL` from `tfactory_config()`, checks `handback_sent.json` to ensure `head_sha` is new (`head_sha != pre_fix_sha` and not in `sent_shas`), POSTs `{tfactory_task_id, project_id, spec_id, correlation_key, source_branch, head_sha, pr_number, status}` to `<TFACTORY_BASE_URL>/api/handback/aifactory-complete` with `X-TFactory-Handback-Token` (from `TFACTORY_HANDBACK_SECRET` or `APP_INBOUND_HANDBACK_SECRET` or `tfactory_config()["token"]`), and records `head_sha` in `handback_sent.json`.
   → verify by `apps/backend/.venv/bin/pytest tests/test_handback_contract.py apps/web-server/tests/test_inpod_slice_d.py -q -o asyncio_mode=auto`
   Traps: `agent_job_backend._correction_env` (`:420-422`) strips `GITHUB_*`/`GH_*` from the correction Job container so the Job itself stays untrusted; the control plane performs the git commit + push in `_await_correction` after `fetch_correction` validates and applies the file set.

6. (agy) Add focused regression tests in `apps/web-server/tests/test_w2_approved_branch_handoff.py` covering:
   - `_route_hard` includes `project_id` and `base_branch`;
   - `from-plan` adopts `factory/2172-slug` at `approved_head_sha`, rejects tampered or drifted remote head with 422, allows retry when remote head descends from `approved_head_sha`;
   - `run_pr_endgame` with `adopted_pr` never auto-merges even when `AIFACTORY_AUTO_MERGE=true` and marks PR ready for review on TFactory pass at exact head SHA;
   - `apply-correction` → `send_fix_completed` posts only to `TFACTORY_BASE_URL/api/handback/aifactory-complete` (ignoring `tfactory_callback_url`) and drops duplicate/stale SHAs.
   → verify by `apps/backend/.venv/bin/pytest apps/web-server/tests/test_w2_approved_branch_handoff.py -q -o asyncio_mode=auto`
   Traps: use the conftest temp registry and injectable `runner`/`poster` fakes (no network or live `gh` calls).

## Tests

- `apps/backend/.venv/bin/pytest tests/ -m "not slow" -q`
- `apps/backend/.venv/bin/pytest apps/web-server/tests -q -o asyncio_mode=auto`
- `apps/backend/.venv/bin/ruff check` and `apps/backend/.venv/bin/ruff format --check` on all touched files.

## Rollback

Revert the PR. All new contract and metadata fields (`source_branch`, `approved_head_sha`, `base_ref`, `pr_number`, `handback_sent.json`) are additive; reverting returns `from-plan` to cutting `aifactory/<spec_id>` branches and leaves `apply-correction` without the outbound completion callback.
