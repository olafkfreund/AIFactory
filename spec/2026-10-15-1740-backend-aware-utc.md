---
status: approved
issue: 1740
intent: intent/2026-10-15-1740-backend-aware-utc.md
---

# Spec: write aware UTC timestamps in apps/backend and provider_benchmark

## Design

Follow #1624's pattern (`from datetime import UTC`, `datetime.now(UTC)`).

1. **Writers → UTC.** Every naive `datetime.now()` flagged by DTZ005 under the
   repo config in `apps/backend/**` (36 files) and
   `scripts/provider_benchmark.py:234` becomes `datetime.now(UTC)`. The one
   DTZ006 site, `spec/pipeline/models.py:71`, becomes
   `datetime.fromtimestamp(..., UTC)`, and its paired `datetime.now()` on
   line 72 is changed at the same time. `apps/web-server/server/routes/context.py:207`
   (`lastRefreshed`, invisible to DTZ005) changes the same way.
2. **Exception: console debug times.** `core/debug.py:135,177,192,207,222,237`
   print `HH:MM:SS.mmm` to the operator's terminal. They keep local wall-clock
   time with `# noqa: DTZ005 - local wall-clock console time`, the same
   approach #1624 took in `web-server/server/routes/terminal.py:585`.
3. **Readers that compare must accept both shapes.** Old files on disk hold
   naive local values. A naive value was written by `datetime.now()` in host
   local time, so `datetime.fromisoformat(s).astimezone(UTC)` reads it
   correctly (Python treats a naive value as local time in `astimezone`). It
   also normalises aware values.
   - `merge/types.py:405-406,459` and `merge/timeline_models.py:67,97,118,220,300-301`
     (all merge `from_dict` loaders). `FileEvolution.add_task_snapshot`
     (`merge/types.py:482`) sorts loaded snapshots together with new ones,
     which raises `TypeError` on mixed naive and aware values.
   - `services/recovery.py:183-193` already branches on `tzinfo` with a
     second naive cutoff. With the parse normalised, the naive cutoff and the
     branch can be deleted.
   - Readers that only format (`review/formatters.py:297`,
     `review/reviewer.py:218`) or already coerce (`qa/review_cycle.py`,
     web-server `agent_kubejob.py`, `stale_tasks.py`, `delegation_tracker.py`)
     need no change.
4. **Gate.** Delete `ruff.toml:227-229` (the comment and both per-file-ignores),
   and update the comment at `ruff.toml:127`.

Out of scope: `apps/backend/runners/github/`, which `ruff.toml:94` excludes as
a vendored mirror. `bot_detection.py` writes (`:370`) and reads
(`:266-267,432-437`) its own `last_review_times` naive on both sides, so it
is consistent and no changed writer feeds it. It is left as is and noted as a
follow-up together with `models.py` and the vendored `rate_limiter.py`.

## Alternatives rejected

- **A shared `parse_stored_ts()` helper**: the one-line `.astimezone(UTC)`
  is clearer at each loader than a new import across two modules.
- **Coerce naive to UTC with `.replace(tzinfo=UTC)`** (the web-server readers do this):
  for backend-written legacy values that is wrong by the host offset. They
  were local time, and `astimezone` reads them as local.
- **`noqa` for all display-only strings** (`review/html_generator.py:242`,
  `review/state.py:184`, merge filenames): UTC is the fleet convention.
  Only the live console clock keeps local time.

## Risks

- Display strings with no zone suffix (`html_generator` "generated_at",
  `review/state.py` approval line, ideation/merge filenames) now show UTC.
  Pods run in UTC, so nothing changes there. On a developer laptop they shift by the offset.
- ISO strings gain `+00:00`. Python readers use `fromisoformat` (3.11+) and the
  frontend uses `new Date()`, so both parse it. No `strptime` reader exists in
  `apps/backend` (grep).
- Tests that assert an exact naive string might need updating.

## Verification

- New regression test: `FileEvolution.from_dict` with a naive `started_at`,
  then `add_task_snapshot` with an aware snapshot. It must not raise, and the
  loaded value comes back aware. It fails on the base branch with `TypeError`.
- `ruff check .` with the per-file-ignores gone (the gate) and
  `ruff format --check` on the touched files.
- Targeted backend pytest: `tests/` for merge, recovery, spec, ideation,
  implementation_plan, status.
