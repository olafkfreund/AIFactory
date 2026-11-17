---
status: draft
issue: 1932
author: olafkfreund
---

# Intent: turn on Job-only agents in the aifactory Deployment (#1932, #1931, #1930)

## Problem

AIFactory#2089 (in release v3.13.0) added `AIFACTORY_REVIEW_BACKEND=kubejob` (PR review and
clarification as Jobs) and `AIFACTORY_FORBID_INPOD_AGENTS=true` (refuse in-pod agent spawns). Both
default off. Until they are set in factory-gitops, PR review and clarification still run as LLM
sessions in the control-plane pod, reading attacker-controlled PR and issue text as the server uid.
Builds and spec creation already run as Jobs (`AIFACTORY_BUILD_BACKEND`, `AIFACTORY_SPEC_BACKEND`).
`APP_REQUIRE_ENV_SECRETS=true` is already live (factory-gitops#364).

## Proposed outcome

The aifactory Deployment sets `AIFACTORY_REVIEW_BACKEND=kubejob` and
`AIFACTORY_FORBID_INPOD_AGENTS=true`, each with a comment and a one-line rollback. A smoke task on
`aifactory-demo` completes with spec, build and review as Jobs and no in-pod agent.

## Affected users and systems

factory-gitops `apps/aifactory/manifests/manifests.yaml` (Argo CD syncs it; pod restarts).
AIFactory web-server on the live cluster.

## Constraints

- Flip only once the running image contains #2089 (v3.13.0).
- Do not restart pods while a dev->main PR is open or a release/deploy run is in progress in the four factory repos.
- Insights, changelog, conflict resolution and the settings probe stay in-pod (slice D); the forbid switch does not cover them.

## Open questions

None. Decision (owner delegation 2026-11-17): turn both on together, because FORBID without
REVIEW=kubejob would refuse every PR review.
