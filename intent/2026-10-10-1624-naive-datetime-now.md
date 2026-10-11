---
status: approved
issue: 1624
author: olafkfreund
---

# Intent: naive `datetime.now()` timestamps written across web-server and backend

Follow-up to #1619 (PR #1629). That PR fixed one function,
`task_service._iso_from_timestamp` (`apps/web-server/server/routes/task_service.py:927`).
Every other naive writer is unchanged. Not fixed on dev (checked at c59a0ea6):
nothing on dev references #1624 and no PR is open for it.

## Problem

156 lines in `apps/web-server` and `apps/backend` (not counting tests)
contain naive `datetime.now()` (the issue's grep). Ruff DTZ005 finds 154
calls. 6 more use `fromtimestamp()` with no timezone (DTZ006: `files.py` and
`spec/pipeline/models.py`); none use `utcnow()`.
Around 162 other sites already write tz-aware UTC. So the tree mixes two
conventions, and a timestamp string alone does not say which one it uses.

A naive write records the server's local time with no offset. That causes
three problems:

1. **The cockpit shows the wrong time.** `new Date(x)` reads a string with no
   offset as the browser's local time. With a UTC pod and a BST operator, ages
   and durations are off by one hour. This is the #1619 symptom, and it still
   comes from every writer except the one #1619 fixed. Reproduced: a
   `TaskProgress.timestamp` written just now shows as 60 minutes old under
   `TZ=Europe/London`.
2. **Naive and aware values meet and fail.** `fromisoformat` on a naive string
   returns a naive datetime. Comparing it with `datetime.now(UTC)` raises
   `TypeError` (reproduced with `agent_task_models.TaskProgress`). The issue
   reports this in CFactory's WS reader, which is in another repo.
3. **The stale-task sweep is now an hour off.** This is not in the issue; it
   is a side effect of #1619. `updated_at` is now aware UTC
   (`task_service.py:1033`), but `stale.py:63-70` `_now()` returns naive local
   time (`# noqa: DTZ005`). `stale_tasks.py:142-145` strips tzinfo from both
   and subtracts them. On a non-UTC host, tasks are reaped early by the UTC
   offset.

Readers already cope in different ways. `agent_kubejob.py:1009-1013` and
`qa/review_cycle.py:246-268` treat naive values as UTC, and
`services/recovery.py:184-193` keeps two cutoffs. Others, such as
`merge/timeline_models.py`, `merge/types.py`, `runners/github/audit.py` and
`intake/processed_store.py:235`, parse with no normalisation at all.

Nothing stops new naive calls. DTZ is not in the blocking `ruff.toml`
`[lint] select` (lines 107-127). Only `standards/ruff.toml:30` selects it, and
that file feeds only the diff-scoped ratchet.

## Proposed outcome

- A written contract: write tz-aware UTC; readers treat a naive legacy value
  as UTC.
- Every writer listed in the issue is classified as persisted state, API
  response, or in-memory/log, and is brought under the contract.
- The cockpit shows the same age for a timestamp whatever the browser's
  timezone. The stale sweep measures idle time correctly on any host.
- Comparing a stored timestamp with an aware "now" never raises `TypeError`.
- The blocking lint fails on a new naive `datetime.now()` (ruff DTZ005), with
  the few intended local-time uses explicitly marked.

## Affected users and systems

- Operators using the cockpit (`apps/frontend-web`) from a timezone other than
  the pod's. Affected views include PR logs, task logs and progress, kanban and
  pipeline sorting, worktrees, changelog and context refresh times.
- `apps/web-server`: `pr_review_service`, `agent_task_models`,
  `agent_credential`, `agent_spec_creation`, and the routes `execution`,
  `tasks`, `from_issue`, `projects`, `settings`, `context`, `terminal`, and
  `stale`/`stale_tasks`.
- `apps/backend`: `runners/github`, `merge/` (orchestrator, timeline),
  `core/debug`, `query_memory`, `review/`, `qa/`, `services/recovery`,
  `intake/`.
- API consumers: CFactory (`upstream_ws.py`, other repo), the cockpit, PFactory.
- On-disk state that already holds naive strings: PR review logs, task JSON,
  project and settings records.
- CI: `ci.yml:84` ruff step, root `ruff.toml`, `standards/ruff.toml`, the cq
  ratchet.

## Constraints

- Existing naive rows on disk must keep loading. Readers accept both forms
  for as long as old data exists.
- The API timestamp format must stay parseable by CFactory, the cockpit and
  PFactory. An offset suffix is already handled correctly by JS `new Date()`.
- Adding DTZ to the blocking `ruff.toml` fails CI at once unless the fixes,
  or shrinking per-file-ignores, land in the same change.
- Some uses really are local time: `stale.py` file-mtime comparison,
  `terminal.py:585` filenames, `settings.py:2128-2129` 7-day window. Changing
  them changes behaviour that users can see.
- Follow the #1619 precedent (`_iso_from_timestamp` writes aware UTC).
- No in-flight worktree touches these files or `ruff.toml`.
  #1631 and #1632 (OpenAPI) might declare `format: date-time`; this is not
  verified.

## Open questions

1. **Legacy rows.** Read naive values as UTC with no migration, as the issue
   proposes, and accept a skew on rows written by non-UTC dev machines? Or
   rewrite them once?
2. **Wire format.** `+00:00` (Python `isoformat`) or `Z`?
3. **Scope.** One PR for all ~160 sites plus DTZ in the blocking lint? Or
   staged: contract and web-server persisted/API sites first, then
   `apps/backend`, with per-file-ignores in between?
4. **CFactory reader.** Fix it in this task, or open a separate CFactory
   issue?
5. **Local-time uses** (stale mtimes, terminal filenames, settings 7-day
   window). Keep them local with `noqa`, or convert them to UTC?
6. **Stale sweep.** Fix the #1619 side effect in `stale.py`/`stale_tasks.py`
   as part of this task, or ship it first as a separate quick fix?
