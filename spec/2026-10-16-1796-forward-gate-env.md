---
status: draft
issue: 1796
intent: intent/2026-10-16-1796-forward-gate-env.md
---

# Spec: forward the gate env (timeout, sandbox network) into the build Job

Line numbers are at `fb97ccd6`.

## Design

1. Add `AIFACTORY_GATE_TIMEOUT_SECONDS` and `AIFACTORY_SANDBOX_NETWORK` to
   `_PASSTHROUGH_BUILD_ENV` (`apps/web-server/server/services/build_backend.py:253`),
   next to the other gate vars, with a one-line `#1796` comment.
   `build_job_env` (`:342-346`) already skips unset/empty values.
2. Do **not** forward `AIFACTORY_DATA_ROOT` (deviation from the issue text).
3. One test in `tests/test_build_backend_kubejob.py`, modelled on
   `test_build_job_env_forwards_both_nix_store_vars` (`:349`): both names are
   forwarded with their values, and `AIFACTORY_DATA_ROOT` is not.

## Why not `AIFACTORY_DATA_ROOT`

In every reader it means "the path where the data PVC is mounted in this
pod": the subPath prefix `KubeJobSandbox` strips (`gate_runner.py:390-395`)
and the `repo_is_mountable` test (`gate_runner.py:481`, `:507`;
`apps/backend/core/kube_sandbox.py:305`). The build Job does not mount the
PVC at that path: `build_run_py_job_manifest` (`build_backend.py:650-700`)
mounts only the worktree subPath at `/work`, or an emptyDir on the packed
path. So the control-plane value would name a path that does not exist in
the Job pod, and the in-Job default is already the correct answer for that
pod: `/work` is not under it, so `repo_is_mountable` is False and the gate
takes the packed path (#1491/#1524). Forwarding it would at best change
nothing and at worst (a data root that prefixes `/work`) make a nested gate
Job mount the wrong subPath.

## Alternatives rejected

- Forward all three as the issue says: see above.
- Set `AIFACTORY_DATA_ROOT` explicitly in the Job to a Job-specific value:
  no reader needs it; the default is already correct.

## Risks

- A deployment that sets `AIFACTORY_SANDBOX_NETWORK` to something other than
  `none` now applies it to in-Job gates too. That is the intended outcome.
- Credential guard (#1759): neither name is a credential; the guard test
  keeps passing.

## Verification

- `pytest tests/test_build_backend_kubejob.py tests/test_env_passthrough_credentials.py -q`
- The new test fails on `fb97ccd6` and passes after the change.
- `ruff check` / `ruff format --check` on the touched files.
