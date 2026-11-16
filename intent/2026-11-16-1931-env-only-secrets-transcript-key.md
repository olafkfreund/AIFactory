---
status: draft
issue: 1931
author: olafkfreund
---

# Intent: env-only secrets switch and project-qualified transcript key

## Problem

Two small open items, one PR.

1. #1931: `apps/web-server/server/config.py` still auto-generates `API_TOKEN` and `JWT_SECRET` files under the data dir when the env is empty. Prod sets both from env, but nothing stops a deployed pod from silently falling back to a file that same-uid in-pod agents can read. The issue's last code item is a fail-closed switch.
2. #2059 item 1: the transcript object key uses `spec_id`, which repeats across projects (#1972). `transcriptUris` on a repeated spec id can name another project's objects.

Also found while closing #1930-#1932: the spec-creation Job (live since factory-gitops#349) is dispatched but its pod stops in the `netpol-gate` init container ("kube-api reachable and kubelet ALSO reachable"), so spec creation currently fails closed. That is a live-cluster matter, filed separately as Factory issue, not fixed here.

## Proposed outcome

- `APP_REQUIRE_ENV_SECRETS=true` makes startup fail with a clear error if `APP_API_TOKEN` or `APP_JWT_SECRET` is empty, instead of generating files. Default off, so local dev and tests are unchanged.
- Transcripts are keyed by the Job's project-qualified `JOB_ID` (already `project:spec`), and the control plane lists by the task id, so two projects with the same spec id no longer share a prefix.

## Affected users and systems

AIFactory web-server startup; build Jobs (transcript upload); completion event and revert PR body.

## Constraints

No new env contract between Job and control plane (JOB_ID already exists). Default behaviour of the config switch must not change. No secret values printed.

## Open questions

None. Decisions taken by owner delegation (2026-11-16): switch is opt-in (a default-on switch could crash non-prod installs); gitops flip is a follow-up. Subprocess backend has no JOB_ID and keeps the spec-id key (local dev only).
