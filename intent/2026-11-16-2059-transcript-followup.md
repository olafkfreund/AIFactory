---
status: approved
issue: 2059
author: olafkfreund
---

# Intent: transcript follow-up, TFactory revert caller, hub hardening

## Problem

#2054 added `POST /api/tasks/{task_id}/post-merge-regression` (opens a revert PR) but
nothing calls it: TFactory's regression run never tells AIFactory a post-merge run failed.
The hub redaction table (`factory_common.secrets`) has no Anthropic key shape, and
RFC-0004 still lists the revert path as "not implemented".

## Proposed outcome

- TFactory's regression run (CLI and shared trigger) calls the AIFactory route when a run on a commit
  has regressions and the spec carries an AIFactory task id. Inert unless a public https
  run URL is configured.
- `factory_common.secrets` redacts `sk-ant-` keys fleet-wide.
- RFC-0004 states phase 1 and the revert path are implemented.

## Affected users and systems

TFactory `agents/regression/trigger.py`; Factory hub `secrets.py` and RFC-0004; this repo
holds the artifacts only.

## Constraints

Never merge a revert; bearer token only to the configured AIFactory origin; no live
cluster change; consumers re-vendor `factory_common` on their own pin bump.

## Open questions

Out of scope here, stay open on #2059: (1) run id and project id in the transcript key,
(3) spec-creation Job transcripts, (4) chunked upload for interrupted Jobs, (7) revert
idempotency across control-plane replicas. Each needs a Job/control-plane contract
change that is larger than this round.
