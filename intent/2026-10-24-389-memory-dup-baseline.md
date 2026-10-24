---
status: draft
issue: 389
author: olafkfreund
---

# Intent: record the RFC-0021 memory duplicate-rate baseline

## Problem

RFC-0021 Phase 1 (promotion gates) waits on one number: how duplicated the
project-pooled memory is once several specs have fed it. Phase 0 shipped
(#1031, #1034, #1035) and memory has accumulated for about three months. Nobody
has measured it.

## Proposed outcome

`scripts/memory_dup_baseline.py` counts exact and near-duplicate pairs
(cosine >= 0.88) across a project's pooled gotchas and session insights. One
real project is measured read-only, and `docs/dev/memory-baseline-2026-10.md`
records counts, project, spec count, date and the similarity measure used.

## Affected users and systems

RFC-0021 owners. Read-only against the live aifactory pod; no runtime change.

## Constraints

Must not modify any project data. Must not spend model quota. No new dependency.

## Open questions

None.
