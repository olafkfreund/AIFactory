---
status: draft
issue: 1931
spec: spec/2026-11-17-1931-env-secrets-flip.md
coders:
  1: you     # one-line manifest edit
reviewers:
  - codex
---

# Plan: APP_REQUIRE_ENV_SECRETS=true (#1931)

Decisions carried from the spec: set `APP_REQUIRE_ENV_SECRETS=true` on the aifactory container only, in factory-gitops; image `sha-1d03c52` (v3.12.0) already deployed and both secrets set from `factory-secrets` (JWT env `APP_JWT_SECRET` from key `AIFACTORY_JWT_SECRET`); tier auto; one-line revert.

## Steps
1. factory-gitops `apps/aifactory/manifests/manifests.yaml` after the `APP_JWT_SECRET` line (~405): add the env var with a comment citing AIFactory#1931/#2080. → `python -c 'import yaml,sys;list(yaml.safe_load_all(open(sys.argv[1])))' <file>` and repo CI checks.
   Traps: Argo syncs this live; keep the diff to one line plus comment.

## Tests
YAML parses; repo CI green; after merge (coordinator) the pod is Ready.

## Rollback
Delete the line.
