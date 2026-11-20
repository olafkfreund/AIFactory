---
status: draft
issue: 2172
author: olafkfreund
---

# Intent: Adopt the approved branch/PR, carry one handoff identity, and close the TFactory return loop

Part of olafkfreund/Factory#3864 (W2: issue → intent → spec → plan → build → test, GitHub first; owner-approved plan 2026-11-20). Companion slice: olafkfreund/PFactory#1096.

## Problem

1. **Hard-tier intake drops project and integration branch.** `apps/web-server/server/services/intake_poller.py:327-362` (`_route_hard`) forwards `repo`, `provider`, `issue_number`, `title`, `body`, `labels`, `autonomy_tier`, and `change_mode` to PFactory, but omits `cfg.project_id` and `cfg.base_branch` (which `_route_low_medium` at `:306-324` sends). Repos integrating via `dev` lose their base branch when routed to PFactory.
2. **No shared handoff identity or branch/PR adoption on `from-plan`.** PFactory's W2 stage machine (PFactory#1096) opens a draft PR on `factory/<n>-<slug>` and commits `intent/<slug>.md`, `spec/<slug>.md`, and `plan/<slug>.md` as each stage is approved. When PFactory emits the signed contract to `POST /api/tasks/from-plan` (`apps/web-server/server/routes/execution.py:1402-1612`), `FromPlanRequest` (`:192-206`) and `start_task_execution` (`:1580-1589`) accept only `baseBranch`. Worktree setup (`apps/backend/core/worktree.py:685-687,802-914` and `apps/web-server/server/services/build_backend.py:1356-1449`) unconditionally creates a new `aifactory/<spec_id>` branch from `base_branch` (deleting any existing local branch at `:855`) instead of adopting the approved `factory/<n>-<slug>` branch at the exact approved head SHA, and `tfactory_client.py:264-271` hardcodes `aifactory/<spec_id>`.
3. **Baseline drift checks the wrong ref for an adopted branch, and PR endgame can open a duplicate PR or auto-merge.** `check_baseline_drift` (`apps/backend/trusted_plan.py:671-750`) compares `baseline_commit` against the project checkout's `HEAD` instead of verifying that the adopted `source_branch` is at `approved_head_sha` and descends from `base_ref`. At completion, `pr_endgame.py:2118-2328` creates a new PR and, when `AIFACTORY_AUTO_MERGE` is on, can auto-merge (`:1719-1727`). W2 requires reusing the existing draft PR, marking it ready for review only after TFactory verifies the exact head SHA, and never auto-merging from this flow.
4. **The TFactory return loop is open and accepts a caller-supplied callback URL.** `ApplyCorrectionRequest` (`execution.py:117-149`) accepts `tfactory_task_id` and `tfactory_callback_url` ("accepted, not yet acted on", `:144-148`), and `apply_task_correction` (`:1615-1665`) neither persists `tfactory_task_id` nor notifies TFactory when the QA Fixer finishes (`apps/backend/qa/correction.py:151-205`, `apps/web-server/server/services/agent_job_backend.py:437-500`). Calling `tfactory_callback_url` from the request body would be an SSRF / token-exfiltration vector.

## Proposed outcome

- **Explicit repo boundary with PFactory#1096:** PFactory owns the durable stage job, reading `.factory.yml` from the trusted base branch, draft PR creation on `factory/<n>-<slug>`, stage commits (`intent/`, `spec/`, `plan/`), stage issue labels (`factory:intent|spec|plan`), and bound single-use `/approve <stage>` verification (`fetch_pr_comments` in `protocol.py:614`, immutable provider user ID resolution, base-branch CODEOWNERS / write-access policy, exact GitLab user-ID lookup replacing `gitlab_provider.py:781`). AIFactory owns forwarding `project_id` and `base_branch` on hard intake, adopting the approved branch/PR and handoff identity on `from-plan`, coding on that branch, forwarding the identity to TFactory, marking the draft PR ready for review (never auto-merging) with labels `factory:building|verifying|done|needs-human`, and closing the TFactory fix-completed return loop.
- **One handoff identity (`PFactory → AIFactory → TFactory`):** `{project_id, repo, base_ref, source_branch, approved_head_sha, pr_number}` (where `repo` is provider-qualified per RFC-0020 3.5). Carried in the signed contract (`provenance` / `handoff`), persisted into `requirements.json`, `task_metadata.json`, and `.task_branch`, and forwarded in `build_ingest_payload` (`tfactory_client.py:664-793`) including `source_sha`.
- **Adopt existing branch & retry resume:** When `source_branch` and `approved_head_sha` are present, AIFactory fetches `origin/<source_branch>`, verifies its tip matches `approved_head_sha` (or on retry is a fast-forward of `approved_head_sha` carrying only prior build commits for this spec), checks out `source_branch` in both subprocess (`core/worktree.py`) and kubejob (`build_backend.py`, `core/workspace_fetch.py`) modes without `branch -D`, and pushes new commits back to `source_branch`.
- **Ready-for-review endgame (never auto-merge):** For an adopted draft PR (`pr_number`), `pr_endgame.py` skips `create_pr`, forces `auto_merge=False`, waits for `tfactory_verdict(..., head=current_head) == "pass"` (`tfactory_client.py:945-1007`), marks the draft PR ready for review (`gh pr ready`), and transitions issue labels (`factory:building` → `factory:verifying` → `factory:done`, or `factory:needs-human` on failure/stuck).
- **Configured-origin TFactory return loop:** When a correction cycle finishes and commits a new SHA on the task branch, AIFactory POSTs fix-completed to `<TFACTORY_BASE_URL>/api/handback/aifactory-complete` (never `request.tfactory_callback_url`) with `X-TFactory-Handback-Token`, `{tfactory_task_id, project_id, spec_id, correlation_key, source_branch, head_sha, pr_number, status}`. Duplicate or stale SHAs (or no new commit) are suppressed before sending so they never consume a TFactory handback cycle.

## Affected users and systems

- **AIFactory (`apps/web-server`, `apps/backend`):** `intake_poller.py`, `routes/execution.py`, `trusted_plan.py`, `services/build_backend.py`, `services/task_branch.py`, `services/pr_endgame.py`, `services/completion_orchestration.py`, `services/agent_job_backend.py`, `core/worktree.py`, `core/workspace/setup.py`, `core/workspace_fetch.py`, `cli/main.py`, `cli/build_commands.py`, `pfactory/tfactory_client.py`, `qa/correction.py`, `qa/correction_job.py`.
- **Shared / sibling boundaries:** PFactory#1096 (producer of the branch, draft PR, and signed contract); TFactory (`routes/handback.py:138-223`, `routes/specs.py:245-341`).

## Constraints

- **Security rules (mandatory from Factory#3864):**
  - Never auto-merge a PR on the adopted W2 flow; only mark ready for review after TFactory passes on the exact head SHA.
  - Outbound fix-completed callbacks go ONLY to the configured `TFACTORY_BASE_URL` origin (`tfactory_config()["base_url"]`); `request.tfactory_callback_url` is ignored/deprecated so caller input can never redirect callbacks or leak `X-TFactory-Handback-Token`.
  - Handoff fields (`source_branch`, `approved_head_sha`, `base_ref`, `pr_number`) ride inside the HMAC-signed contract bytes verified by `verify_plan_signature` (`trusted_plan.py:307-366`), and `repo` is checked against the project's own repository (`_check_declared_repo` in `routes/from_issue.py:225-251`).
  - Branch adoption validates ref names (`git check-ref-format --branch`) and 40-hex commit SHAs (`^[0-9a-f]{40}$`), rejecting traversal or option-injection strings before any `git` call.
  - Stage approvals in PFactory#1096 are resolved by immutable provider user ID against base-branch policy (CODEOWNERS for the stage file, else write access), bound to `PR id + stage + exact head SHA`, single-use, invalidated on any new push, with exact-ID GitLab member lookup (`gitlab_provider.py:781`).
- Backward compatibility: existing `from-plan` contracts without `source_branch` / `approved_head_sha` keep cutting `aifactory/<spec_id>` from `base_branch` as today.

## Open questions

None.
