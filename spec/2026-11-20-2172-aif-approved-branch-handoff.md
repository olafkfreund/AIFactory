---
status: approved
issue: 2172
intent: intent/2026-11-20-2172-aif-approved-branch-handoff.md
---

# Spec: Adopt the approved branch/PR, carry one handoff identity, and close the TFactory return loop

## Design

### 1. Boundary with PFactory#1096 and hard-tier intake forwarding
PFactory#1096 owns the stage job (`intent` → `spec` → `plan`), reading `.factory.yml` from the trusted base branch, creating branch `factory/<n>-<slug>` and the draft PR, applying `factory:intent|spec|plan` labels, and verifying bound single-use `/approve <stage>` comments (`fetch_pr_comments` at `protocol.py:614`, immutable provider user ID, base-branch CODEOWNERS for the stage file else write access, exact user-ID lookup in `gitlab_provider.py:781`, bound to `PR id + stage + exact head SHA`).
In AIFactory, `apps/web-server/server/services/intake_poller.py:349-362` (`_route_hard`) adds `"project_id": cfg.project_id` and `"base_branch": cfg.base_branch` to the JSON body POSTed to `PFACTORY_INGEST_URL`, matching `_route_low_medium` (`:310-324`).

### 2. Canonical handoff identity and `from-plan` verification
The handoff identity across PFactory → AIFactory → TFactory is:
- `project_id`: resolved AIFactory project UUID (`execution.py:1422`)
- `repo`: provider-qualified repo ref (`[provider:]owner/name`, RFC-0020 3.5)
- `base_ref`: target integration branch (e.g. `main`, `dev`)
- `source_branch`: PFactory-created branch (`factory/<n>-<slug>`)
- `approved_head_sha`: exact 40-hex commit SHA on `source_branch` where `/approve plan` was bound
- `pr_number`: positive integer draft PR/MR number opened by PFactory

**Contract & request surface (`apps/backend/trusted_plan.py`, `apps/web-server/server/routes/execution.py`):**
- All handoff fields live inside `plan["provenance"]` (or `plan["handoff"]` normalized into `provenance` before signing) so they are covered by `_signing_bytes` (`trusted_plan.py:201-222`). `FromPlanRequest` (`execution.py:192-206`) and `TaskProvenance` (`:151-182`) accept optional `base_ref`, `source_branch`, `approved_head_sha`, `pr_number`, but when the signed contract carries them, any request-body override that disagrees is rejected with **422** before spec allocation (`execution.py:1440`).
- When `provenance.repo` is present, `create_from_trusted_plan` validates it against the project via `_check_declared_repo(project_id, project_path, repo_ref)` (`routes/from_issue.py:225-251`).
- `source_branch` and `base_ref` are validated with a strict ref validator (no leading `-`, no `..`, no control chars/spaces, not in `{"HEAD", ""}`, `source_branch != base_ref`); `approved_head_sha` must match `^[0-9a-fA-F]{40}$`; `pr_number` must be `int > 0`.
- **Adopted-branch baseline verification (`trusted_plan.py:671-750`):** When `source_branch` and `approved_head_sha` are present on the contract, `check_baseline_drift` fetches `origin` refs (or inspects fetched refs via `task_branch.py` helpers) and checks:
  1. `origin/<source_branch>` resolves to `approved_head_sha` on initial start, OR on a retry where the spec already has recorded build commits (`memory/build_commits.json`), `approved_head_sha` is an ancestor of `origin/<source_branch>`. If `origin/<source_branch>` is at a different unapproved SHA (e.g. a human pushed after approval), reject with **422** (`status="adopted_head_mismatch"`).
  2. `baseline_commit` (on `base_ref`) is checked for blast-radius drift against `origin/<base_ref>`, not the local checkout's `HEAD`.
- **Persistence (`trusted_plan.py:806-935`, `execution.py:1465-1590`):** `ingest_trusted_plan` and `create_from_trusted_plan` stamp `base_branch` (`base_ref`), `source_branch`, `approved_head_sha`, `pr_number`, and `repo` into `task_metadata.json` and `requirements.json["provenance"]`, and call `record_branch(project_path, spec_id, source_branch)` (`server/services/task_branch.py:105-107`) so `.task_branch` holds `factory/<n>-<slug>` from creation.

### 3. Branch adoption in worktree & kubejob execution
- **Branch resolution (`apps/backend/core/worktree.py:685-687`, `apps/backend/pfactory/tfactory_client.py:264-271`):**
  - `WorktreeManager.__init__` accepts `source_branch: str | None = None` and `approved_head_sha: str | None = None` (or reads them from `.aifactory/specs/<spec_name>/task_metadata.json`). `get_branch_name(spec_name)` returns `source_branch` when set, else `f"aifactory/{spec_name}"`.
  - In `create_worktree(spec_name)` (`core/worktree.py:802-914`), when adopting `source_branch`: fetch `origin <source_branch>` and `origin <base_branch>`. Verify the remote `source_branch` tip equals `approved_head_sha` (or descends from `approved_head_sha` on retry). Do **not** run `git branch -D` onto a fresh `start_point` from `base_branch`; instead create/reset the local branch to the verified `origin/<source_branch>` commit (`git worktree add -B <source_branch> <worktree_path> FETCH_HEAD`) so the committed `intent/`, `spec/`, and `plan/` files stay in the tree and retries resume existing work.
- **Kubejob & packed workspace (`apps/web-server/server/services/build_backend.py:1356-1449`, `apps/backend/core/workspace_fetch.py:106-195`):**
  - `_populate_self_contained_worktree` reads `base_branch`, `source_branch`, and `approved_head_sha` from `source_spec_dir / "task_metadata.json"`. It clones at `base_branch`, sets `origin`, refreshes `base_branch` (`_refresh_to_remote_base`), and when `source_branch` is set, fetches `origin <source_branch>:refs/remotes/origin/<source_branch>` into the build clone so the in-Job `WorktreeManager` can adopt it offline or online, and records `source_branch` via `record_branch(project_path, spec_id, branch)`.
  - `maybe_push_workspace_branch` (`core/workspace_fetch.py:149,177`) already reads `HEAD`'s branch name in the task worktree (`git rev-parse --abbrev-ref HEAD`) and pushes `HEAD:<branch>`; because the worktree checks out `source_branch`, it pushes to `factory/<n>-<slug>` without force-overwriting unrelated history.
  - In `tfactory_client.py:264-271,321-428,431-561`, `_build_branch(spec_id, spec_dir=None)` reads `.task_branch` / `task_metadata.json["source_branch"]` first, falling back to `f"aifactory/{spec_id}"`. In `_git_info_and_push` (`:400-407`), when `source_branch` is an adopted branch, use `git push --force-with-lease` instead of `--force` so a concurrent push on the PR is never clobbered.

### 4. TFactory handoff, ready-for-review endgame, and issue labels
- **TFactory ingest payload (`apps/backend/pfactory/tfactory_client.py:664-793`):**
  - `build_ingest_payload` includes `source_branch` (the adopted branch), `source_sha` (the built commit SHA from `_locate_build_commit` / remote tip), `base_ref` (`_task_base_branch(spec_dir)`), `pr_number` (from `task_metadata.json` / `provenance`), and `repo` (from `provenance.repo` / `githubIssue.repo`).
  - When `pr_number` is already known at handoff time, TFactory receives it in the initial ingest payload (and `send_pr_attach` at `:890-929` remains a safe idempotent backfill).
- **Adopted-PR endgame (`apps/web-server/server/services/pr_endgame.py`, `completion_orchestration.py`):**
  - `gather_pr_context` (`pr_endgame.py:1858-1975`) reads `pr_number` and `source_branch` from `task_metadata.json` / `requirements.json["provenance"]` and returns `"adopted_pr": pr_number` in the context dict.
  - In `run_pr_endgame` (`:2118-2328`), when `adopted_pr` is present:
    1. Force `auto_merge = False` unconditionally (an adopted W2 PR is never auto-merged, regardless of `AIFACTORY_AUTO_MERGE` or `reviewTier`).
    2. Push the branch (`push_with_lfs`) and reuse `pr = adopted_pr` instead of calling `create_pr` (or if `create_pr` is called, it updates the existing PR title/evidence block without opening a second PR).
    3. Call `send_pr_attach(spec_dir, spec_id, pr, repo)` so TFactory's `source.json` has `pr_number`.
    4. Poll the verification gate (`_build_merge_gate`) for `tfactory_verdict(spec_dir, spec_id, head=current_head)`. When `tfactory_verdict == "pass"` on the exact current PR head SHA, mark the draft PR ready for review via `mark_pr_ready(owner, name, pr, runner=runner)` (`gh pr ready <pr> --repo <owner>/<name>`), swap issue label `factory:verifying` → `factory:done`, and return `{"ok": True, "pr": pr, "merged": False, "ready_for_review": True}`. On terminal reject/timeout after handback cycles, apply `factory:needs-human`.
  - **Lifecycle labels on the originating issue:** Helper `transition_factory_label(project_path, spec_dir, to_label)` swaps the active `factory:(intent|spec|plan|building|verifying|done|needs-human)` label on `provenance.issue_number` via the project's `GitProvider`: set `factory:building` when `from-plan` starts execution, `factory:verifying` when `maybe_auto_handoff_tfactory` sends the handoff, `factory:done` when TFactory passes and the PR is marked ready, and `factory:needs-human` when a build fails or the return loop marks it stuck.

### 5. TFactory return loop (configured origin, SHA-bound dedup, branch push)
- **Request handling & persistence (`apps/web-server/server/routes/execution.py:117-149,1615-1665`, `apps/backend/qa/correction.py:63-108,226-327`):**
  - `ApplyCorrectionRequest.tfactory_callback_url` is kept as an optional schema field for backward compatibility with existing TFactory callers (`send.py:145`), but its docstring and handler explicitly ignore it. Instead, `record_handback` (`qa/correction.py:63-108`) records `tfactory_task_id` (from `request.tfactory_task_id` or `triage["tfactory_task_id"]`), `pre_fix_sha` (current tip of the task branch before the fixer runs), and `correlation_key` into `handback_received.json`.
  - **Stale/duplicate inbound handback guard:** If `handback_received.json` already exists with the same `(manifest_hash, pre_fix_sha)` and a fix is already in progress or completed for that SHA, `apply_correction` returns `{"success": True, "deduplicated": True, "started": False}` without re-running the QA Fixer or burning a cycle.
- **Pushing the fix and notifying TFactory (`apps/backend/qa/correction.py`, `apps/backend/qa/correction_job.py`, `apps/web-server/server/services/agent_job_backend.py`, `apps/backend/pfactory/tfactory_client.py`):**
  - The QA Fixer must run against the task's build worktree / branch (`resolve_task_branch`), not the base branch. In `_prepare_correction_workspace` (`agent_job_backend.py:363-410`), check out the task branch (`recorded_branch` / `resolve_task_branch_fetching`) in the correction clone before packing, and in `fetch_correction` (`core/workspace_fetch.py:1226-1371` / `_await_correction` at `agent_job_backend.py:437-461`), commit and push the applied correction to `origin/<task_branch>` (using `authed_push_url` + `lfs_upload`), recording the new `post_fix_sha`. On the in-pod path (`_run_fixer_bg` in `qa/correction.py:151-205`), run the fixer in the task worktree and push `HEAD:refs/heads/<task_branch>` to origin.
  - New helper `send_fix_completed(spec_dir, spec_id, *, tfactory_task_id, head_sha, status)` in `apps/backend/pfactory/tfactory_client.py`:
    1. Resolves base URL exclusively from `tfactory_config()["base_url"]` (`TFACTORY_BASE_URL`) + `"/api/handback/aifactory-complete"`. Never reads `tfactory_callback_url`.
    2. Authenticates with header `X-TFactory-Handback-Token: <TFACTORY_HANDBACK_SECRET or APP_INBOUND_HANDBACK_SECRET or TFACTORY_TOKEN or APP_API_TOKEN>` (plus `Authorization: Bearer` when token is set).
    3. **Deduplication & stale-SHA gate:** Reads `handback_sent.json` in `spec_dir`. If `head_sha` is empty, equals `pre_fix_sha` (fixer produced no new commit), or was already sent in `handback_sent.json["sent_shas"]`, it does **not** POST `status="complete"` to TFactory (avoiding burning a TFactory cycle on a duplicate or no-op SHA) and instead logs/records `"skipped_stale_or_duplicate_sha"`.
    4. Otherwise POSTs `{tfactory_task_id, project_id, spec_id, correlation_key, source_branch, head_sha, pr_number, status}` to `<TFACTORY_BASE_URL>/api/handback/aifactory-complete` and records `head_sha` in `handback_sent.json`.

## Alternatives rejected

- **Calling `request.tfactory_callback_url` with `assert_safe_target_url`:** Even with RFC-1918/loopback allowed (as TFactory's `send.py:137` does), any caller with `member` access on `apply-correction` could point AIFactory at an internal cluster service and exfiltrate `X-TFactory-Handback-Token`. Resolving the origin exclusively from `TFACTORY_BASE_URL` eliminates the SSRF class completely.
- **Letting `pr_endgame` open a second PR on `aifactory/<spec_id>` and closing PFactory's draft PR:** Breaks the audit trail on the draft PR where `/approve intent|spec|plan` comments live.
- **Using `git push --force` on adopted branches:** Can overwrite commits pushed between approval and build; `--force-with-lease` or fast-forward push protects the branch.

## Risks

- **Correction Job currently packs `project` (base branch) instead of the task branch (`agent_job_backend.py:385-388`):** If not switched to the task branch before running the fixer, the fix applies against `main`/`dev` and fails to push onto `factory/<n>-<slug>`. Addressed in Design §5.
- **Shallow or stale local project clone during `check_baseline_drift`:** `check_baseline_drift` (`trusted_plan.py:671`) currently never fetches. For adopted branches, a targeted `git fetch origin <base_ref> <source_branch>` before the drift check ensures both `baseline_commit` and `approved_head_sha` are present locally.

## Verification

- Unit tests for `_route_hard` forwarding `project_id` and `base_branch`.
- Unit tests for `from-plan` with `{base_ref, source_branch, approved_head_sha, pr_number}`: signature tampering rejected (422), mismatched `approved_head_sha` on remote rejected (422), retry when remote is a descendant of `approved_head_sha` accepted, `.task_branch` and `task_metadata.json` populated.
- Unit tests for `WorktreeManager.create_worktree` and `_populate_self_contained_worktree` adopting `factory/2172-slug` without `branch -D`, preserving existing files (`intent/`, `spec/`, `plan/`).
- Unit tests for `run_pr_endgame` with `adopted_pr`: `auto_merge` forced off even when `AIFACTORY_AUTO_MERGE=true`, no second PR created, `gh pr ready` called only after `tfactory_verdict(..., head=sha) == "pass"`.
- Unit tests for `apply_correction` + `send_fix_completed`: ignores `tfactory_callback_url`, posts to `TFACTORY_BASE_URL/api/handback/aifactory-complete`, suppresses duplicate or unchanged `head_sha`.
