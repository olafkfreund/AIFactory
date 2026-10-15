---
status: draft
issue: 1736
spec: spec/2026-10-15-1736-nix-store-job-env.md
---

# Plan: forward both nix-store vars into the build Job, drop its unseeded store mount

Paths are relative to the repo root; line numbers at `e87de74b`.
`V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin`; put it
first on `PATH` (no `.venv` in the worktree).

## Approved decisions (carried from the spec)

1. Add `AIFACTORY_PACKED_NIX_IN_IMAGE` and `AIFACTORY_NIX_STORE_PVC`
   together to `_PASSTHROUGH_BUILD_ENV`.
2. The build Job never mounts the store PVC: `build_backend` stops passing
   `nix_store_pvc`. `apps/backend/core/job_dispatch.py` is NOT edited
   (vendored byte-exact from the Factory hub; deviation from the issue text,
   recorded in the spec).
3. Dead code after (2) is deleted: `_ENV_NIX_STORE_PVC`,
   `_packed_nix_in_image`, the half comment at `build_backend.py:172-176`.
4. Tests and docs as in spec sections 3 and 4.

## Steps

### Step 1: `apps/web-server/server/services/build_backend.py`

- `:259-327` `_PASSTHROUGH_BUILD_ENV`: after `"AIFACTORY_BUILD_IMAGE",`
  (`:281`) add a short comment and the two names, adjacent:
  `# #1736: the gate in this Job reads both (gate_runner.py, nix_env.py).`
  `# Forward them together: the PVC alone brings back the #253 node pin.`
  `"AIFACTORY_PACKED_NIX_IN_IMAGE",` `"AIFACTORY_NIX_STORE_PVC",`
- `:659-665` in `build_run_py_job_manifest`: delete the `nix_store_pvc = ...`
  line, the 4-line comment and the `if workspace_uri and _packed_nix_in_image():`
  block. Replace with one comment line:
  `# The build Job mounts no Nix store: the image carries /nix (#1736).`
- `:756`: delete `nix_store_pvc=nix_store_pvc,`.
- `:153`: delete `_ENV_NIX_STORE_PVC = ...`.
- `:172-176`: delete the five comment lines starting
  `# RFC-0017 #190: when the build image ships its own` through
  `# The nix-source flag itself now lives in core.nix_env ...`.
- `:884-900`: delete `_packed_nix_in_image` entirely.

Check: `grep -n "_packed_nix_in_image\|_ENV_NIX_STORE_PVC\|nix_store_pvc" apps/web-server/server/services/build_backend.py`
prints nothing; `ruff check` and `ruff format --check` on the file pass.

Traps: do not touch `apps/backend/core/job_dispatch.py`. Keep
`_pack_workspace_enabled` and `workspace_uri` handling; only the store
lines go.

### Step 2: `tests/test_build_backend_kubejob.py`

- `test_manifest_runs_run_py_on_build_image_with_mounts` (`:123`): keep
  `setenv("AIFACTORY_NIX_STORE_PVC", ...)` at `:130`; edit the comment at
  `:126` to say the store PVC is set to prove the build ignores it (#1736);
  `:177-180`: comment says only the worktree remains, assertion
  `mount_paths == {"/work"}`.
- Replace section 10 (the banner comment from `:1502` through the end of
  `test_manifest_nonpacked_keeps_nix_store_pvc_even_with_flag`, `:1594`):
  keep `_nix_store_present`; one new test
  `test_build_job_never_mounts_nix_store(monkeypatch, packed, flag)`
  parametrized `packed in (True, False)`, `flag in (True, False)`; set
  `AIFACTORY_SANDBOX_REPO_PVC`, `AIFACTORY_NIX_STORE_PVC=aifactory-nix-store`,
  `AIFACTORY_DATA_ROOT`, `AIFACTORY_IMAGE`; flag → setenv
  `AIFACTORY_PACKED_NIX_IN_IMAGE=1` else delenv; packed → pass the same
  `workspace_uri` the old tests used, else omit it. Assert
  `_nix_store_present(m) == (False, False)`. New banner comment: "10. #1736:
  the build Job never mounts the nix-store PVC (it was unseeded: no
  /nix/var db). Gates mount it, seeded, via kube_sandbox."
- After `test_build_job_env_propagates_present_provider_env` (`:313`) add
  `test_build_job_env_forwards_both_nix_store_vars`: delenv every
  `_PASSTHROUGH_BUILD_ENV` var, setenv both, call `bb.build_job_env("t")`,
  assert both present with their values. Docstring: #1736, forward together
  or the #253 pin returns.

Check: `PATH=$V:$PATH pytest tests/test_build_backend_kubejob.py tests/test_env_passthrough_credentials.py -q`
passes; ruff check/format on the file pass.

Traps: `bb` is `server.services.build_backend` (`:36`). Do not remove the
`delenv("AIFACTORY_NIX_STORE_PVC")` lines at `:281`, `:1452`, `:1484`
(harmless; out of scope).

### Step 3: comments and docs

- `apps/backend/agents/gate_runner.py:489-495`: replace the sentences from
  "Both gate paths (packed and co-mount) drop the PVC" through "brings back
  the #253 node pin." with: "Both gate paths drop the PVC when
  nix_in_image() is true. The build Job forwards both variables
  (_PASSTHROUGH_BUILD_ENV, #1736), so this guard sees the same values here
  as on the control plane; the build Job itself mounts no store."
- `apps/backend/core/nix_env.py:47-50`: replace the last docstring
  paragraph with: "The build Job forwards both AIFACTORY_PACKED_NIX_IN_IMAGE
  and AIFACTORY_NIX_STORE_PVC (#1736), so the gate path inside it reads
  the same values; the build Job itself mounts no store."
  Also `:42`: "(build: ``build_backend``; gate/verify: ``gate_runner``)"
  becomes "(gate/verify: ``gate_runner``, including gates inside the build Job)".
- `tests/test_gate_runner_nix.py:342-344` docstring: drop "and the build
  packed path (_packed_nix_in_image in build_backend.py)".
- `docs/docs/environment-reference.md`: `:241` purpose → "Gate Jobs resolve
  `/nix` from the image instead of the Nix-store PVC (removes the last
  node-pin). Forwarded into the build Job (#1736)."; `:248` purpose →
  "Warm Nix-store PVC that gate Jobs mount (seeded from the image). Forwarded
  into the build Job; the build Job itself never mounts it (#1736)."

Check: `grep -rn "_packed_nix_in_image" apps tests` prints nothing;
`PATH=$V:$PATH pytest tests/test_gate_runner_nix.py -q` passes; ruff on
the two Python files.

Traps: comment and doc edits only; no logic in `gate_runner.py` or
`nix_env.py` changes.

## Tests

`PATH=$V:$PATH pytest tests/test_build_backend_kubejob.py tests/test_gate_runner_nix.py tests/test_env_passthrough_credentials.py tests/test_child_process_env.py -q`.
Regression proof: copy `dev`'s `build_backend.py` over the branch's
temporarily; the new forward test and the no-mount test fail; restore.

## Rollback

Revert the PR. Operators can also unset `AIFACTORY_NIX_STORE_PVC` to stop
gate Jobs mounting the store.
