---
status: approved
issue: olafkfreund/Factory#1959
author: olafkfreund
---

# Intent: Pin the provider credentials a dispatched AIFactory Job receives

First slice of the model access gateway epic (olafkfreund/Factory#1959).

## Problem

The epic's correction 2: provider credentials do not stop at the Deployment.
`_PASSTHROUGH_BUILD_ENV` in `apps/web-server/server/services/build_backend.py` copies provider keys into every dispatched
build Job. Nothing pins *which* credentials cross that boundary. A new key can be
added to the Job env without anyone deciding to, and the later slices that move
each credential behind the gateway have no test that tells them when a key has
really stopped reaching the Job.

## Proposed outcome

One test fails whenever the set of model-provider credential env names that
reach a dispatched build Job changes, in either direction. Today the set is:
ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, OPENAI_API_KEY, OPENAI_COMPATIBLE_API_KEY, GEMINI_API_KEY, GOOGLE_API_KEY, OLLAMA_API_KEY. Adding one fails the test; so does
removing one, which turns each later removal into a deliberate, reviewed edit
of the pinned set.

## Affected users and systems

AIFactory tests only. No runtime change, no Deployment, Job, gitops or credential
change. Running builds keep every credential they get today.

## Constraints

- Must not stop copying any credential into Jobs. Running builds depend on them.
- Must not change gitops, cluster config or any live credential, and must not deploy.
- The test reads the real env the Job gets (the builder's output), not the
  constant alone, so a credential added by another route is caught too.

## Open questions

None for this slice. The epic's own open questions (Plane B bench test, Plane C
decision, OAuth token pool) belong to later slices.

## Remaining slices, in order

1. This slice: pin the Job credential set (AIFactory build Job, TFactory verify Job).
2. Gateway exists, nothing uses it: new first-party repo + image, signed, Kyverno
   glob extended, PASS result plus a deliberate unsigned-tag deny (gitops; needs the owner).
3. CFactory through the gateway; then remove its `OLLAMA_API_KEY` and prove it still works.
4. TFactory verify path (Plane A): code before gitops; drop keys from the pinned set as they move.
5. Routes in the contract: additive `route`/`phase_routes` from `model-catalog.json` classes,
   route resolution from a hot-reloadable ConfigMap, flag default off.
6. AIFactory + PFactory (Plane A), including `_PASSTHROUGH_BUILD_ENV`, after one full PARR run.
7. Plane B (Claude OAuth) only after the bench test on `ANTHROPIC_BASE_URL` + OAuth, and the token pool (#670).
8. Revoke and rotate every credential at the provider.
