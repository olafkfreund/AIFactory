---
status: draft
issue: 1736
intent: intent/2026-10-15-1736-nix-store-job-env.md
---

# Spec: forward both nix-store vars into the build Job, drop its unseeded store mount

Line numbers are at `e87de74b`.

## Design

### 1. Forward both variables together

Add `AIFACTORY_PACKED_NIX_IN_IMAGE` and `AIFACTORY_NIX_STORE_PVC`, adjacent,
to `_PASSTHROUGH_BUILD_ENV` (`apps/web-server/server/services/build_backend.py:259`)
with one comment: the gate inside the Job reads both (`gate_runner.py:496`),
and they must move together or the #253 node pin returns (#1736).
`build_job_env` (`:345`) already forwards only non-empty values, so a
deployment that sets neither sees no change.

Neither name is credential-shaped, so the #1759 guard
(`tests/test_env_passthrough_credentials.py`) is unaffected.

### 2. The build Job never mounts the store PVC

In `build_run_py_job_manifest` delete the store read and its packed-path
drop (`build_backend.py:659-665`) and the `nix_store_pvc=` argument
(`:756`); `JobSpec.nix_store_pvc` defaults to `None`
(`core/job_dispatch.py:160`). Then `_ENV_NIX_STORE_PVC` (`:153`),
`_packed_nix_in_image` (`:884-900`) and the half-sentence comment at
`:172-176` have no reader; delete them.

`core/job_dispatch.py` is **not** edited (vendored byte-exact from the hub).
Its optional mount and self-check (`:554-562`, `:816`, `:912`) stay as a
capability of the shared builder that AIFactory no longer uses.

### 3. Tests (`tests/test_build_backend_kubejob.py`)

- `:123-180`: keep `AIFACTORY_NIX_STORE_PVC` set (it now proves the build
  ignores it); mounts become `{"/work"}`; fix the comments.
- Section 10 (`:1503-1594`, three tests): replace with one test
  parametrized over packed/non-packed × flag on/off, PVC set: no
  `nix-store` volume and no `/nix/store` mount in any case.
- New: `build_job_env` forwards both variables when set.
- New: both names are on `_PASSTHROUGH_BUILD_ENV` together (a tuple
  membership check; removing one fails it).

### 4. Comments and docs

- `gate_runner.py:489-495` and `nix_env.py:47-50`: say both are now
  forwarded into the build Job (#1736), so the gate honours the flip.
- `tests/test_gate_runner_nix.py:344` docstring: drop the reference to
  `_packed_nix_in_image`.
- `docs/docs/environment-reference.md:241,248`: the PVC is mounted by gate
  Jobs only, never by the build Job; both are forwarded into the build Job.

## Alternatives rejected

- **Edit `job_dispatch.py` here (as the issue says).** Breaks the byte-exact
  drift gate until the hub canonical changes, the pin is bumped, and TFactory
  re-vendors. A cross-repo change for no extra effect on AIFactory, since
  only `build_backend` passes the field.
- **Seed the build store like the gate does.** Larger, and the build image
  already carries `/nix`; #1620 chose removal.
- **Forward only the flag.** The gate would then never get a PVC, silently
  disabling the warm cache for gates on deployments that rely on it.

## Risks

- **Gate behaviour inside the build Job changes.** With the PVC set and the
  flag off, gate Jobs started from a build Job now mount the seeded store
  PVC (as gates started on the control plane already do), which pins them to
  the PVC's node (#253). With the flag on they drop it. This is the intended
  "one flip, both paths" behaviour; operators who see unschedulable gate
  pods set `AIFACTORY_PACKED_NIX_IN_IMAGE`.
- **Build Jobs lose a cold, unseeded store mount.** No closures persisted
  through it (no db), so nothing is lost; the Job is no longer PVC-pinned.
- **Not verified on a live cluster.** Verification is unit tests on the
  manifest and env; a live Job run is needed to confirm scheduling.

## Verification

- `pytest tests/test_build_backend_kubejob.py tests/test_gate_runner_nix.py
  tests/test_env_passthrough_credentials.py` passes.
- Regression proof: the new "forwards both" test fails against `dev`'s
  `build_backend.py`, and the parametrized no-mount test fails there too.
- `ruff check` / `ruff format --check` on touched Python files; ratchet CI.
