---
status: draft
issue: 1932
intent: intent/2026-11-17-1932-inpod-agents-off.md
---

# Spec: Job-only agents flags

## Design

Add two env entries next to `AIFACTORY_SPEC_BACKEND` in the aifactory container of
`apps/aifactory/manifests/manifests.yaml`:
`AIFACTORY_REVIEW_BACKEND=kubejob` and `AIFACTORY_FORBID_INPOD_AGENTS=true`, each commented with the
issue, minimum version (>= 3.13.0) and the revert ("delete this line").

Code audit (AIFactory dev, read-only). `inpod_agent_refusal()` guards exactly four spawns: spec
creation (`agent_spec_creation.py`), builds (`build_backend.py`, `agent_kubejob.py`), PR review
(`pr_review_service.py`) and clarification (`clarification_service.py`). With BUILD, SPEC and REVIEW
backends all on kubejob, none takes the in-pod path, so FORBID refuses nothing in normal use. The build QA loop runs
inside the build Job. Codex review (confirmed): \`POST /{task_id}/apply-correction\` (\`qa/correction.py\`) still runs a QA fixer in-pod without the guard; it stays in-pod and is added to slice D. Conflict resolution, insights, changelog and the settings probe are not guarded
and keep working (slice D, tracked in #1932).

## Alternatives rejected

- Flip only REVIEW and wait on FORBID: leaves a silent in-pod fallback path available; FORBID is a no-op when everything is Jobs, so there is nothing to soak.
- Two PRs: both flags must ship together or review breaks.

## Risks

- Image older than 3.13.0: the vars are ignored (harmless), but nothing is protected. PR is merged only after the Deployment image contains #2089.
- Review/clarification Job fails (S3, netpol): reviews error with no in-pod fallback. Rollback: delete both lines.
- Pod restart during a release: wait for releases to finish.

## Verification

Pod Ready; both env vars present; a docs-only task on `aifactory-demo` via task_create_and_run shows
spec, build and review Jobs in `kubectl get jobs -n factory` and no in-pod agent process.

Codex review (read-only, 2026-11-17): guard claims for build, spec, review and clarification confirmed; no flow is refused by the flag; Job prerequisites and live behaviour unverified until the smoke task.
