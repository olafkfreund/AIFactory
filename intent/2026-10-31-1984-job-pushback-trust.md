---
status: draft
issue: 1984
author: olafkfreund
---

# Intent: refuse gated tasks on subprocess, and never trust what a build Job pushes back

Covers slice B of #1932 and all of #1984, in one PR. Owner decision for #1932
(2026-10-31): option B, agents run only in Jobs; the subprocess backend becomes
local-dev only.

## Problem

Re-verified on `origin/dev` 8497484b (code reading, not a live run).

1. **Slice B.** The subprocess backend runs agents in the control-plane pod,
   with write access to the task's own gate state (`task_metadata.json`,
   `review_state.json`, `task_control.json`). A task that is gated on human
   review (`requireReviewBeforeCoding`, quick mode) or on a blocking
   `reviewTier` is still started there. `start_task_execution`
   (`services/agent_service.py`) and `start_spec_creation`
   (`services/agent_spec_creation.py`) refuse only a broken backend config
   (#1942), never a gated task.
2. **#1984.** A build Job writes files under its own `/work/.aifactory`, which
   the agent can write, and pushes them to object storage. The control plane
   fetches them and acts on them:
   - `_kubejob_review_reason` (`services/agent_kubejob.py`) takes a
     `human_review` + `plan_review`/`injection_scan` status in the pushed plan
     as a review pause. That skips the completion, the TFactory handoff and the
     PR endgame, and writing `plan_review` to `task_control.json` drops an
     existing plan approval.
   - `maybe_fetch_memory` (`core/workspace_fetch.py`) extracts the pushed
     `memory.tar.gz` into the control-plane spec dir with `filter="data"`. That
     filter allows any relative path, so a forged archive can overwrite
     `task_control.json`, `review_state.json`, `task_metadata.json` or
     `context/task_contract.json`. The Job's object-store credential can write
     this key for any spec.
   - `load_task_contract` (`pfactory/tfactory_client.py`) falls back to the
     pushed `implementation_plan.json` when it carries `tfactory` /
     `contract_version` / `approval`. On a legacy task a forged plan then
     chooses what TFactory tests.
   - The merger PR body shows the pushed gate marker and the plan's
     `qa_signoff` as "Verification status" without saying where they came
     from.

## Proposed outcome

- On the subprocess backend, a review-gated or tier-gated task (build or spec
  creation) is refused before any agent runs, with a client-visible message
  that the task needs the kubejob backend. Local dev without those gates is
  unchanged.
- Every artifact a build Job hands back is listed in the spec with a decision:
  recomputed by the control plane from its own state, or ignored for every
  decision and derived otherwise. None is used as authority. No HMAC with a key
  the agent can read.
- For each artifact a test shows that a forged value has no effect.

## Affected users and systems

- `apps/web-server/server/services/{build_backend,agent_service,agent_spec_creation,agent_kubejob,merger}.py`
- `apps/backend/core/workspace_fetch.py`, `apps/backend/pfactory/tfactory_client.py`
- Operators running subprocess in a pod with gated tasks: those tasks are now
  refused (intended). Local dev is unchanged for ungated tasks.

## Constraints

- No deploy, no gitops or cluster change, no credential change.
- No HMAC or signature whose key the agent can read.
- Read-side paths (stop, reconcile, reap, board display) keep working.

## Remaining #1932 slices, in order (after this PR)

1. A1. Spec-creation Job substrate (`KubeJobSpecBackend`, flag off).
2. A2. Allowlisted spec fetch-back.
3. A3. Logs (#1929 nonce) and reconcile, then the gitops flag flip.
4. A4. PR-review and clarification sessions as Jobs.
5. D. Separate uid for anything still in-pod, plus the `core/client.py` grant
   narrowing (#1930), then #555 to close #1931.

## Open questions

None. The owner decision fixes the direction.
