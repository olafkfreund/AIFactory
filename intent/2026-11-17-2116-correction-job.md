---
status: approved
issue: 2116
author: olafkfreund
---

# Intent: run QA apply-correction as a Job (#2116)

## Problem

With `AIFACTORY_FORBID_INPOD_AGENTS=true` (live since factory-gitops#372) `POST /tasks/{id}/apply-correction` is refused with 409 (#2095, #2104), because the QA fixer is an agent session that would run in the web-server pod. That closes the in-pod escape, but the TFactory to AIFactory fix loop (#317) is down in production.

## Proposed outcome

A second backend flag, `AIFACTORY_CORRECTION_BACKEND=kubejob` (default off), runs the QA fixer for a confirmed correction as a Kubernetes Job on the same substrate as PR review and clarification (#1932, #2089): validated, size-capped fetch-back, and no in-pod fallback on any failure. With the flag unset, the 409 refusal under FORBID is unchanged, and so is the in-pod path when FORBID is off.

## Affected users and systems

AIFactory web-server (`routes/execution.py`, new Job dispatch in `agent_job_backend.py`), the backend (`qa/correction.py` plus a small Job entrypoint, `core/workspace_fetch.py`). TFactory callers of the hand-back. Production needs the env set by the owner or coordinator in factory-gitops (not edited here).

## Constraints

No factory-gitops edits. No in-pod fallback when the flag is on. The Job never gets the control plane's token, JWT or DATABASE_URL, and no GitHub credential. Whatever comes back from the Job is untrusted: it is applied only after path, symlink and size checks. Defaults unchanged when the flag is off. No live-cluster actions.

## Open questions

None. Owner delegation 2026-11-17: take the least risky recommended option. Recorded decision: the fix is returned as a validated file set (not a git push), because the Job has no GitHub write credential and a push would bypass control-plane validation.
