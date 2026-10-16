---
status: draft
issue: 1796
spec: spec/2026-10-16-1796-forward-gate-env.md
---

# Plan: forward the gate env (timeout, sandbox network) into the build Job

Paths relative to the repo root; line numbers at `fb97ccd6`.
`V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin`; put it
first on `PATH` (no `.venv` in the worktree).

## Approved decisions (carried from the spec)

1. Forward `AIFACTORY_GATE_TIMEOUT_SECONDS` and `AIFACTORY_SANDBOX_NETWORK`.
2. Do NOT forward `AIFACTORY_DATA_ROOT`: it names the data PVC mount in the
   current pod, and the build Job does not mount the PVC there. The PR says so.
3. One forward test; it also asserts `AIFACTORY_DATA_ROOT` is not forwarded.

## Steps

Small change (two files, two edits): the lane agent implements it itself
rather than handing it to codex.

### Step 1: `apps/web-server/server/services/build_backend.py`

In `_PASSTHROUGH_BUILD_ENV`, after `"AIFACTORY_NIX_STORE_PVC",` (`:276`) add:

    # #1796: gates in this Job read these (gate_runner.py). Not
    # AIFACTORY_DATA_ROOT: the Job does not mount the data PVC there.
    "AIFACTORY_GATE_TIMEOUT_SECONDS",
    "AIFACTORY_SANDBOX_NETWORK",

Check: `ruff check` and `ruff format --check` on the file pass.

### Step 2: `tests/test_build_backend_kubejob.py`

After `test_build_job_env_forwards_both_nix_store_vars` (ends `:362`) add
`test_build_job_env_forwards_gate_env`: delenv every `_PASSTHROUGH_BUILD_ENV`
var, setenv `AIFACTORY_GATE_TIMEOUT_SECONDS=900`,
`AIFACTORY_SANDBOX_NETWORK=bridge`, `AIFACTORY_DATA_ROOT=/data`; call
`bb.build_job_env("t")`; assert the first two carry their values and
`AIFACTORY_DATA_ROOT` is absent.

Check: `PATH=$V:$PATH pytest tests/test_build_backend_kubejob.py tests/test_env_passthrough_credentials.py -q`
passes; the new test fails against the step-1-reverted file.

Traps: `bb` is the module alias already imported in the test file.

## Tests

Step 2's test, plus the existing #1759 guard
(`tests/test_env_passthrough_credentials.py`).

## Rollback

Revert the two commits; no data or config migration.
