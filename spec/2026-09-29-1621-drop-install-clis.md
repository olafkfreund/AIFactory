---
status: draft
issue: 1621
intent: intent/2026-09-29-1621-drop-install-clis.md
---

# Spec: delete the initContainer, keep the PATH, assert at image build

## Design

### 1. Delete the initContainer — do not make it conditional

The intent's open question 1, answered: delete.

A conditional "install only if missing" preserves the silent-rescue path, and
silent rescue is the actual defect here. The `/clis` install has been redundant
since the Dockerfile started baking all three CLIs, and nothing complained for
months because the rescue worked. A fallback would reproduce that exactly, one
layer down.

This also matches what the other path already does: `factory-gitops`
`apps/aifactory/manifests/manifests.yaml:84-87` removed the same initContainer
from the control-plane Deployment under #791 —

> provider CLIs (claude-code, codex, gemini-cli + the `antigravity` alias, and
> copilot) are now BAKED into the image at `/home/nonroot/.npm-global/bin`
> (Dockerfile), so the old install-clis init container + its `/clis` emptyDir
> are gone — no per-pod npm fetch.

Confirmed live: the control-plane pod (`aifactory:sha-cc8ea52`, the non-`-nix`
`runtime` stage) resolves `claude`, `codex`, `gemini`, `antigravity` **and**
`copilot` from `/home/nonroot/.npm-global/bin`. This change finishes #791 on the
build path.

### 2. `_inject_install_clis` does two jobs — only one is being removed

This is the detail that makes the diff non-obvious. The function both injects the
initContainer *and* sets `PATH`:

```python
container.setdefault("env", []).append({"name": "PATH", "value": _BUILD_PATH_ENV})
```

`_BUILD_PATH_ENV` is load-bearing and must stay. Its own comment
(`build_backend.py:500-504`) explains why: the build image's PATH additionally
carries `/nix/var/nix/profiles/default/bin` (where `nix` lives, for
`nix develop`) and `/home/nonroot/.npm-global/bin`, and *"dropping either breaks
the packed build"*.

So:

- **Remove**: the `/clis` emptyDir volume, the `install-clis` initContainer, both
  `volumeMounts`, and `_INSTALL_CLIS_IMAGE` / `_INSTALL_CLIS_SCRIPT` /
  `_CLIS_VOLUME_NAME` / `_CLIS_MOUNT_PATH`.
- **Keep**: the `PATH` env injection, with `/clis/bin:` dropped from the front of
  `_BUILD_PATH_ENV` so the baked `/home/nonroot/.npm-global/bin` is no longer
  shadowed.
- **Rename** the function to what it now does — it is no longer about installing
  anything.

Deleting the function wholesale would silently break the packed build, which is
precisely the class of mistake this issue is about.

### 3. The loud check belongs at image build, not at runtime

The intent's open question 2, answered: neither dispatch-time nor in-Job — the
Dockerfile, where the idiom already exists.

`Dockerfile:272` states the reasoning for the existing assertion:

> `claude --version` is the point of the fix, not decoration: this shipped broken

Present today: `claude --version` (`:280`), `agy --version` (`:314`),
`test -x …/copilot` (`:343`, deliberately `test -x` rather than execution,
because the Copilot CLI self-downloads a platform bundle on first run). Missing:
`codex` and `gemini` — the two the initContainer was rescuing.

Adding those assertions is strictly better than a runtime check:

- It fails when the image is built, not when a user's build runs.
- It fails once, in CI, rather than once per affected build.
- A dispatch-time check cannot work anyway: the control plane cannot inspect the
  build image's filesystem before creating the Job.

So a missing CLI becomes an image that does not build, instead of a build that
quietly downloads a replacement.

### 4. Scope: AIFactory only

The intent's open question 3, answered by search: `/clis` appears **only** in
`apps/web-server/server/services/build_backend.py`. The shared, vendored
`core/job_dispatch.py` builder has zero references, so TFactory and PFactory —
which build their manifests from it — never injected this. No sibling-repo work,
and no vendored-copy drift to manage.

## Alternatives rejected

**Keep it as a fallback ("install only if missing").** Preserves the silent
rescue that hid the drift. The defect is not the 790 MB; it is that nothing ever
complained.

**Delete `_inject_install_clis` entirely.** Would drop the `PATH` injection with
it and break the packed build — `nix` would leave PATH. Tempting because the
function's name suggests it only installs CLIs, which is exactly why the spec
says it out loud.

**Pin the initContainer's npm versions to match the Dockerfile.** Fixes
reproducibility but keeps 790 MB, a network dependency, and two places that must
be edited together — the pattern that produced this.

**A runtime check in the Job.** Later, more often, and it cannot prevent the
image being published broken.

**Leave it alone because it works.** It works by downloading 790 MB per build to
shadow pinned binaries with unpinned ones. The disk is the least of it.

## Risks

- **A CLI turns out not to be baked after all**, on some image variant, and
  builds that silently relied on the rescue start failing. Mitigated by the
  build-time assertions, which would catch it before any image ships, and by the
  live confirmation that all five resolve from the baked path today. This is the
  risk that matters.
- **Dropping `/clis/bin` changes which binary runs.** Intended: from "whatever
  npm served that minute" to "what the image pins". Stated because it is a real
  behaviour change, not a refactor — a build could behave differently if a newer
  CLI had been silently in use.
- **`_BUILD_PATH_ENV` is edited by hand**, and dropping the wrong element breaks
  the packed build with a confusing failure. Covered by a test asserting the
  remaining entries.
- **`agy` and `copilot` were never in `/clis`** and must be unaffected.
- **Blast radius:** build Jobs only. No control-plane change (already done), no
  gate Job change, no gitops change, no schema, no deployment.

## Verification

1. **The manifest no longer contains it:** a dispatched Job manifest has no
   `install-clis` initContainer, no `/clis` volume and no `/clis` volumeMount.
   Pure-function test; `_inject_*` is deliberately I/O-free.
2. **`PATH` is still injected**, still carries
   `/nix/var/nix/profiles/default/bin` and `/home/nonroot/.npm-global/bin`, and
   no longer starts with `/clis/bin`. This is the regression test for the failure
   mode described in §2.
3. **The image asserts all of them:** `docker build` fails if `codex` or `gemini`
   is absent. Provable by building the `runtime` stage with one install removed.
4. **The `-nix` build image carries all three** — verified directly rather than
   inferred from `FROM runtime`, since that inference is what the stale comment
   got wrong.
5. **Existing suite green**, particularly the build-manifest tests, and
   cq-ratchet "0 regressed" on both halves.
6. **Post-merge, on the cluster:** a real build Job starts with no `install-clis`
   initContainer, `which claude` inside it resolves to
   `/home/nonroot/.npm-global/bin/claude`, and the pod's
   `ephemeral-storage.usedBytes` from `stats/summary` is ~192 MB rather than
   ~852 MB. One build proves the disk claim, the PATH claim and the startup-time
   claim together.
7. **Not verified here:** that every provider still functions end to end under
   the pinned versions. The pinned versions are what the control plane has been
   running since #791, which is the strongest evidence available short of a paid
   run per provider.
