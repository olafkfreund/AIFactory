---
status: approved
issue: 1796
author: olafkfreund
---

# Intent: forward the gate env (timeout, sandbox network) into the build Job

Not fixed on dev. Checked at `fb97ccd6`. Found while reviewing #1792 (#1736).

## Problem

Gates started inside the build Job read three env vars that
`_PASSTHROUGH_BUILD_ENV` (`apps/web-server/server/services/build_backend.py:253-330`)
does not forward:

- `AIFACTORY_GATE_TIMEOUT_SECONDS` (`apps/backend/agents/gate_runner.py:49`)
- `AIFACTORY_SANDBOX_NETWORK` (`gate_runner.py:360`)
- `AIFACTORY_DATA_ROOT` (`gate_runner.py:390`, `:481`)

So in-Job gates always use the defaults while control-plane gates use the
operator's values. factory-gitops does not set the first two today, so
nothing is visibly broken yet.

## Proposed outcome

An operator's gate timeout and sandbox network setting reach gates run
inside the build Job, as they reach gates run in the control plane.

## Affected users and systems

- `kubejob` build backend deployments that set either variable.
- No change where neither is set: `build_job_env` (`build_backend.py:342-346`)
  forwards only non-empty values.

## Constraints

- No credential joins the passthrough list (#1759 guard,
  `tests/test_env_passthrough_credentials.py`). None of the three names is a
  credential.
- `AIFACTORY_DATA_ROOT` names where the data PVC is mounted in the current
  pod. The build Job does not mount the PVC there (only a worktree subPath at
  `/work`, or an emptyDir on the packed path), so its value must be checked
  before forwarding.

## Open questions

- Forward `AIFACTORY_DATA_ROOT` or not. Answered in the spec.
