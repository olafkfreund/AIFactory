---
status: approved
issue: 1931
author: olafkfreund
---

# Intent: turn on APP_REQUIRE_ENV_SECRETS in prod (#1931)

## Problem

`APP_REQUIRE_ENV_SECRETS` (AIFactory#2080, released in 3.12.0, default off) makes the server refuse to start when `APP_API_TOKEN` or `APP_JWT_SECRET` is empty, so the server never falls back to an auto-generated token or JWT file. Existing files are not deleted by the flag (the JWT file was already removed from the PVCs on 2026-10-27, #1931). Prod sets both from env but does not set the flag, so a missing Secret key would silently regenerate the files.

## Proposed outcome

The aifactory Deployment sets `APP_REQUIRE_ENV_SECRETS=true`; a missing Secret fails the pod at startup instead of falling back to files.

## Affected users and systems

factory-gitops `apps/aifactory/manifests/manifests.yaml` (Argo CD syncs it). Deployed image is `sha-1d03c52` = v3.12.0, which contains the flag.

## Constraints

Both secrets are already set from `factory-secrets`: env `APP_API_TOKEN` from key `APP_API_TOKEN`, env `APP_JWT_SECRET` from key `AIFACTORY_JWT_SECRET` (manifests.yaml lines 402-405), so the pod must start. Revert is one line.

## Open questions

None.
