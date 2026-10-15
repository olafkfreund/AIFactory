---
status: approved
issue: 1740
author: olafkfreund
---

# Intent: write aware UTC timestamps in apps/backend and provider_benchmark

## Problem

#1624 made the web-server write timezone-aware UTC timestamps and turned on a
blocking DTZ005/DTZ006 lint gate, but carved out `apps/backend/**` and
`scripts/provider_benchmark.py` in `ruff.toml:227-229`. Under the repo config
those paths still hold 77 naive `datetime.now()` / `datetime.fromtimestamp()`
calls (36 backend files plus `scripts/provider_benchmark.py:234`). Naive local
timestamps end up in `implementation_plan.json`, `requirements.json`,
ideation/roadmap/merge state and status files, which other services read as if
they were UTC (the readers in `apps/web-server` already coerce naive values to
UTC, e.g. `server/services/agent_kubejob.py:1103-1108`), so a non-UTC host
shifts every age and staleness check by its UTC offset.

`apps/web-server/server/routes/context.py:207` (`lastRefreshed`) is a naive
writer too, but DTZ005 cannot see it because it goes through
`__import__("datetime")`.

## Proposed outcome

- Every backend and provider_benchmark writer stores an aware UTC timestamp
  (`+00:00` in ISO strings).
- Readers that compare a stored timestamp with now, or with each other, keep
  working on files written before the change (naive) and after it (aware).
- The two DTZ per-file-ignores are gone from `ruff.toml`, so the gate covers
  the backend.

## Affected users and systems

- The backend agent runtime (spec, plan, merge, ideation, roadmap, QA recovery,
  status UI), the web-server context route, and the provider benchmark script.
- Stored JSON state on existing project volumes (old naive values stay on disk).

## Constraints

- Readers and writers change together. Known reader trap:
  `apps/backend/merge/types.py:482` sorts task snapshots by `started_at`, and
  those come from disk (`merge/types.py:405`) mixed with new in-memory ones;
  naive and aware values cannot be compared (TypeError).
- `apps/backend/runners/github/` is excluded from ruff (vendored mirror,
  `ruff.toml:94`); the issue's `bot_detection.py:266-267` trap is checked
  there (see open questions).
- Do not reformat files that are not otherwise changed. Tests keep their DTZ
  ignore (`ruff.toml:260-262`).

## Open questions

- `runners/github/` holds about 20 more naive calls (`models.py`,
  `bot_detection.py`, vendored `rate_limiter.py`). They are outside the
  ruff-gated scope. `bot_detection.py` writes and reads its own
  `last_review_times` naive on both sides, so it is consistent and unaffected by
  this change. Conservative choice: leave `runners/github/` alone and note it
  as a follow-up.
