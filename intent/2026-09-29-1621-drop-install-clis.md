---
status: approved
issue: 1621
author: Olaf Krasicki-Freund
---

# Intent: stop re-downloading the CLIs the image already pins

## Problem

Every build Job runs an `install-clis` initContainer that npm-installs three
provider CLIs into a `/clis` emptyDir (`build_backend.py:_INSTALL_CLIS_SCRIPT`):

```
export npm_config_prefix=/clis
npm install -g @anthropic-ai/claude-code @openai/codex @google/gemini-cli
ln -sf /clis/bin/gemini /clis/bin/antigravity
```

Those three are already in the image it runs, at pinned versions
(`Dockerfile:275-282`):

```dockerfile
RUN npm install -g \
        @anthropic-ai/claude-code@2.1.238 \
        @openai/codex@0.149.0 \
        @google/gemini-cli@0.56.0 \
 && ... && ln -sf .../bin/gemini .../bin/antigravity
```

`build-runtime` is `FROM runtime` (`Dockerfile:467`), so the `-nix` build image
inherits them, and `/home/nonroot/.npm-global/bin` is already on
`_BUILD_PATH_ENV` (`build_backend.py:506`). But `/clis/bin` is prepended *ahead*
of it, so in the live build pod `which claude` returns `/clis/bin/claude`. The
pinned binaries are shadowed by unpinned ones.

The justification in the code is stale (`build_backend.py:485-488`):

> The -nix build image bakes `claude` already (a claude build works), but not
> `codex`/`gemini` — this closes that gap for every runtime.

The Dockerfile bakes all three. Whatever was true when #777 landed stopped being
true, and nothing failed loudly when it did — the initContainer simply kept
working, more slowly.

**This has already been fixed once, on the other path.** `factory-gitops`
`apps/aifactory/manifests/manifests.yaml:84-87` removed the same initContainer
from the control-plane Deployment under #791:

> provider CLIs (claude-code, codex, gemini-cli + the `antigravity` alias, and
> copilot) are now BAKED into the image at `/home/nonroot/.npm-global/bin`
> (Dockerfile), so the old install-clis init container + its `/clis` emptyDir
> are gone — no per-pod npm fetch.

Confirmed live: the control-plane pod runs `aifactory:sha-cc8ea52` and resolves
`claude`, `codex`, `gemini`, `antigravity` and `copilot` from
`/home/nonroot/.npm-global/bin`. The build Job is the straggler.

### Two costs

**Disk.** Measured from the kubelet on a live build Job
(`factory-aifactory-shared-core-remediat-8kzqf`, 28 minutes in): the `clis`
emptyDir is **790.1 MB** of the pod's 852.4 MB total — 93%. Removing it takes the
build-pod floor from ~982 MB to ~192 MB.

**Reproducibility and supply chain, which matters more.** The initContainer runs
`npm install -g` with no version pins, over the network, on the critical path of
every build, and the result shadows the pinned copies. So two builds an hour
apart can run different CLI versions; the Dockerfile's pinning is decorative,
overridden at runtime by whatever the registry serves; and every build takes a
live dependency on npm being reachable and honest — the exposure a pinned image
presumably exists to avoid.

## Proposed outcome

A build Job uses the CLIs its image pins, and fetches nothing at start-up.

Observable:

- No `install-clis` initContainer and no `/clis` emptyDir in a dispatched Job.
- `which claude` inside a build pod resolves to the baked path.
- The CLI versions a build uses are the versions the image pins, and are
  identical across runs of the same image.
- Build pods start faster and use ~790 MB less ephemeral storage.
- If an expected CLI is ever missing from the image, the build fails loudly
  rather than being silently rescued by a network install.

## Affected users and systems

- `apps/web-server/server/services/build_backend.py` — `_inject_install_clis`,
  `_INSTALL_CLIS_*`, and the `/clis/bin:` prefix in `_BUILD_PATH_ENV`.
- Every dispatched build Job. `AIFACTORY_BUILD_BACKEND=kubejob` is live, so this
  is the normal path, not an edge case.
- Not the control plane: gitops already dropped it there under #791.
- Not the gate Job, which does not install CLIs.

## Constraints

- **A missing CLI must fail loudly.** The present design's real defect is that
  drift was absorbed silently for months. Removing the safety net without adding
  a check would repeat that, one layer down.
- Must not change which CLI versions a build gets *by accident* — it will change
  them by design, from "whatever npm served" to "what the image pins". That is
  the point, and it needs saying rather than sliding through.
- `copilot` and `agy` are baked but never were in `/clis`; whatever the change
  does must not disturb them.
- No change to the control-plane path, which is already correct.
- The `-nix` build image must be confirmed to carry all three, not assumed from
  `FROM runtime`.

## Open questions

1. **Delete, or make it a fallback?** Deleting is the honest fix and matches what
   gitops already did. A conditional "install only if missing" would preserve a
   silent-rescue path, which is the behaviour that hid this. I lean strongly to
   deleting, with a startup assertion in its place.
2. **Where should the loud check live** — in the dispatch path before the Job is
   created, or in the Job itself at start-up? Failing before creating a Job gives
   a better message; failing inside it is closer to the truth of what is missing.
3. **Do any other images rely on `/clis`?** TFactory/PFactory build their own
   manifests from the same shared `job_dispatch` builder. Whether they inject the
   same initContainer, and whether their images bake the CLIs, needs checking
   before this is assumed to be AIFactory-local.
