---
status: approved
issue: 1621
spec: spec/2026-09-29-1621-drop-install-clis.md
---

# Plan: delete the initContainer, keep the PATH, assert at image build

Self-contained summary of the approved decisions.

## Approved decisions (carried from the spec)

- **Delete, do not make conditional.** An "install only if missing" fallback
  preserves the silent rescue that hid this drift for months. The defect is that
  nothing complained, not the 790 MB.
- **`_inject_install_clis` does two jobs; only one goes.** It injects the
  initContainer *and* sets `PATH`. `_BUILD_PATH_ENV` is load-bearing — its own
  comment (`build_backend.py:500-504`) says dropping
  `/nix/var/nix/profiles/default/bin` (where `nix` lives for `nix develop`) or
  `/home/nonroot/.npm-global/bin` *"breaks the packed build"*. Keep the PATH
  injection; drop only `/clis/bin:` from the front of it.
- **The loud check goes in the Dockerfile**, extending the existing idiom —
  `claude --version` (`:280`), `agy --version` (`:314`), `test -x …/copilot`
  (`:343`). `codex` and `gemini` lack one, and are exactly the two the
  initContainer was rescuing. Fails once in CI rather than once per build; a
  dispatch-time check cannot work, since the control plane cannot inspect the
  build image's filesystem.
- **AIFactory-only.** `/clis` appears solely in `build_backend.py`; the shared
  vendored `core/job_dispatch.py` has zero references, so TFactory/PFactory
  never injected it.
- **Precedent:** `factory-gitops apps/aifactory/manifests/manifests.yaml:84-87`
  already removed this initContainer from the control-plane Deployment under
  #791. This finishes that on the build path.

## Verified facts this plan relies on

- Baked, pinned, in the image: `Dockerfile:275-282` installs
  `@anthropic-ai/claude-code@2.1.238`, `@openai/codex@0.149.0`,
  `@google/gemini-cli@0.56.0` and creates the `antigravity` symlink.
  `build-runtime` is `FROM runtime` (`:467`), so the `-nix` image inherits them.
- Live control-plane pod (`aifactory:sha-cc8ea52`) resolves `claude`, `codex`,
  `gemini`, `antigravity` and `copilot` from `/home/nonroot/.npm-global/bin`.
- Live build pod: `/clis` emptyDir = **790.1 MB** of an 852.4 MB pod total
  (kubelet `stats/summary`), and `which claude` → `/clis/bin/claude`.
- Symbols to remove: `_INSTALL_CLIS_IMAGE` (`:489`), `_INSTALL_CLIS_SCRIPT`
  (`:490`), `_CLIS_VOLUME_NAME` (`:497`), `_CLIS_MOUNT_PATH` (`:498`),
  `_inject_install_clis` (`:574-602`).
- Call site: `:776` — `return _inject_install_clis(_inject_seed_creds(build_job_manifest(spec)))`.
- Stale module docstring: `:86-90` describes the initContainer as the reason
  non-claude runtimes have their CLI on PATH, citing #777.
- Tests to update: `tests/test_build_backend_kubejob.py` — `:177`
  (`mount_paths == {"/work", "/nix/store", "/clis"}`), `:175-176`, `:286`, `:940`.
  No other test file references `/clis`.

## Steps

1. `tests/test_build_backend_kubejob.py` — **update the assertions first, so they
   fail for the right reason before the code changes**: `:177` becomes
   `{"/work", "/nix/store"}`, and the comments at `:175-176`, `:286`, `:940` stop
   describing an "always-on install-clis" that will not exist. Add an assertion
   that the manifest has **no** initContainer named `install-clis` and no `clis`
   volume. → verify by these failing on `dev` and passing after step 2.

2. `apps/web-server/server/services/build_backend.py` — rename
   `_inject_install_clis` to `_inject_build_path` and reduce it to the one thing
   it still does: append the `PATH` env to the first container. Delete the
   `/clis` volume, the `install-clis` initContainer, both `volumeMounts`, and the
   four now-unused constants (`:489`, `:490`, `:497`, `:498`). Update the call
   site at `:776`. Docstring says why the initContainer is gone and points at
   #791. → verify by step 1's tests.

3. `apps/web-server/server/services/build_backend.py:506` — drop the leading
   `/clis/bin:` from `_BUILD_PATH_ENV`, keeping every other entry byte-identical,
   and update the comment at `:500-504` which currently ends "only prepend
   /clis/bin for the provisioned provider CLIs". → verify by a test asserting the
   exact remaining PATH string.

4. `apps/web-server/server/services/build_backend.py:86-90` — rewrite the stale
   module docstring paragraph: the build Job gets its provider CLIs from the
   image, not from an initContainer. Keep the #777 reference as history and add
   #1621. → verify by reading it back.

5. `Dockerfile:275-282` — extend the existing build-time assertions to the two
   CLIs that lack one: `codex --version` and `gemini --version`, in the same
   `RUN` as the install, matching the `claude --version` idiom at `:280`. Comment
   cites `:272`'s reasoning and #1621. → verify by step 6.

6. Build the `runtime` stage locally and confirm it succeeds; then confirm the
   assertion bites by building with one package removed from the install list and
   observing the failure. → verify by both build outcomes.

7. Run the affected suites and the full suite; commit; run both halves of
   cq-ratchet (it diffs committed history, so after the commit). → verify by
   "0 regressed" from each.

8. Open the PR against `dev` linking all three artifacts. → verify by the PR body
   and `Closes #1621`.

## Tests

```bash
# the build-manifest suite (the one that asserts /clis today)
apps/backend/.venv/bin/pytest tests/test_build_backend_kubejob.py -v

# nothing else references /clis, but prove it
grep -rn "/clis\|install_clis\|install-clis" apps/ tests/   # expect: no output

# full suite
apps/backend/.venv/bin/pytest tests/ -m "not slow" -q

# the image assertion actually builds
docker build --target runtime -t aif-runtime-probe .
```

Expected: manifest tests pass with `{"/work", "/nix/store"}`; no `/clis`
references remain anywhere; full suite green; `runtime` stage builds; cq-ratchet
"0 regressed" on both halves.

Ratchet note: it counts TID252, untyped defs, PLC0415 and DTZ006. Type the
renamed function and any new test helper; keep imports module-level and absolute.

## Post-merge verification (needs the cluster, one real build)

Recorded here so it is not forgotten once the PR is green — the PR cannot prove
these:

- a dispatched build Job has **no** `install-clis` initContainer
- `which claude` inside the build pod → `/home/nonroot/.npm-global/bin/claude`
- the pod's `ephemeral-storage.usedBytes` from
  `/api/v1/nodes/<node>/proxy/stats/summary` is ~192 MB, not ~852 MB

One build proves the disk claim, the PATH claim and the startup-time claim
together.

## Rollback

`git revert <sha>`. The change is manifest construction plus two Dockerfile
assertions — no schema, no migration, no gitops, no deployment. A revert restores
the initContainer and the `/clis` PATH prefix exactly.

Note the one-way door: builds running between merge and revert would have used
the image-pinned CLI versions rather than npm-latest. That is the intended
behaviour change, and it is why the spec names it as a behaviour change rather
than a refactor.

## Out of scope

- The control-plane Deployment: already fixed under #791.
- The gate Job, which installs no CLIs.
- `agy` and `copilot`, which were never in `/clis` and must be left untouched.
- Raising `APP_MAX_CONCURRENT_TASKS` (#1425) on the strength of the freed disk.
  That issue has its own spec and its own arithmetic; this one only makes it
  cheaper.
