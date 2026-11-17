---
status: draft
issue: 1932
spec: spec/2026-11-17-1932-inpod-agents-off.md
coders:
  1: you
reviewers:
  - codex
---

# Plan: Job-only agents flags

Approved decisions: set `AIFACTORY_REVIEW_BACKEND=kubejob` and `AIFACTORY_FORBID_INPOD_AGENTS=true` on the
aifactory container together, commented, revert = delete the lines. Merge only after the live image
contains #2089. Reason for coders: one two-line edit in one file, no handoff.

## Steps

1. factory-gitops `apps/aifactory/manifests/manifests.yaml` after the `AIFACTORY_SPEC_BACKEND` line (~651): add the two env entries with comments → verify by `kubectl kustomize apps/aifactory` (or the repo's CI manifest check) and `git diff`.
   Traps: gitops is Argo-synced live; keep the diff to these lines.
2. After merge and sync (coordinator): verify pod Ready, env present, run one smoke task on aifactory-demo, check Jobs.

## Tests

CI of the gitops PR green; live smoke as in step 2.

## Rollback

Delete the two lines in manifests.yaml; Argo re-syncs.
