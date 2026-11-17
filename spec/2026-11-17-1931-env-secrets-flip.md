---
status: draft
issue: 1931
intent: intent/2026-11-17-1931-env-secrets-flip.md
---

# Spec: APP_REQUIRE_ENV_SECRETS=true on the aifactory Deployment (#1931)

## Design

In factory-gitops `apps/aifactory/manifests/manifests.yaml`, add `{ name: APP_REQUIRE_ENV_SECRETS, value: "true" }` to the aifactory container env, next to `APP_API_TOKEN` / `APP_JWT_SECRET`. Pre-check done: image `sha-1d03c52` is v3.12.0 (contains `config.py` `REQUIRE_ENV_SECRETS`); both secrets are set from `factory-secrets`. Argo CD syncs it. Tier: auto (owner pre-approved).

## Alternatives rejected

- Leave default off: a missing Secret key would silently regenerate a readable token/JWT file.
- Enable on tfactory/pfactory too: out of scope for #1931 (aifactory only); their images may not carry the flag.

## Risks

If either Secret key were empty the pod would fail at startup (that is the intent). Both exist and are mounted today. Rollback: delete the one line.

## Verification

`kubectl get deploy aifactory -o jsonpath` shows image `sha-1d03c52` (read-only, done); after merge Argo reports Synced/Healthy and the pod is Ready; yaml lint of the manifest.
