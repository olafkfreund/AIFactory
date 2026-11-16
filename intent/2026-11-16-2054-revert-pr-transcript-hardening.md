---
status: draft
issue: 2054
author: olafkfreund
---

# Intent: revert PR after a post-merge regression, and transcript hardening

## Problem

When the pipeline merges its own PR and a later regression run fails on that
merge, nothing proposes the undo: a person has to notice, find the run and the
agent transcript, and write the revert by hand. Separately, phase 1 of #2034
(transcripts, AIFactory#2056) left gaps: the QA tool-fallback path is not
captured, and a few other items in #2054 need cross-repo or shared-contract
changes.

## Proposed outcome

- A failing post-merge regression for a commit the pipeline merged opens a
  revert PR whose body links the failing run and the stored transcript(s). It is
  never merged by the pipeline; a person approves it.
- The QA tool-fallback provider's tool calls appear in the run transcript.
- The remaining #2054 items that cannot be done safely here are filed as one new
  issue, not silently dropped.

## Affected users and systems

AIFactory web-server (`pr_endgame`, a new task route), AIFactory backend
(`qa/reviewer.py`), maintainers who review revert PRs.

## Constraints

- The revert is never auto-merged; no code path calls merge on it.
- Only a commit the pipeline itself merged is eligible (recorded at merge time).
- Idempotent: one revert PR per merged PR.
- `core/artifact_store.py` and `factory_common/` are vendored: not edited here.
- No live-cluster change.

## Open questions

None. Deferred to a new issue: run id in the transcript key (needs the Job to
know its run id and the control plane to agree on it), spec-creation Jobs,
streaming upload of interrupted Jobs, `sk-ant-` redaction shapes (hub
`factory_common.secrets`, Factory) and the RFC-0004 status line (Factory).
