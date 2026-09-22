---
status: draft
issue: 1569
spec: spec/2026-09-23-1569-task-created-at-stamp.md
---

# Plan: Report the recorded creation time, and label a guess as a guess

## Decisions (carried from the approved intent and spec)

- Three sources, in order: `requirements.json`'s `created_at` → the oldest `st_mtime` of
  `requirements.json` / `spec.md` → the directory's `st_ctime`.
- Sources 2 and 3 set `created_at_is_estimate: bool = False` on `Task`. `created_at` stays a
  required ISO string in every case, so no consumer's sorting breaks.
- A stamp that is not a valid ISO-8601 string is ignored (debug log) and falls through.
- **No backfill and no writes to task data.** Spec dirs are gitignored, so there is no history
  to recover, and inventing a date is worse than labelling an estimate.
- `updated_at` keeps using `st_mtime`, unchanged.

## Steps

1. `apps/web-server/server/routes/task_service.py`, `load_spec_metadata` (`:494-533`): where it
   already parses `requirements.json` for the title, also read `created_at` into
   `metadata["created_at"]` (absent → `None`). Additive: no existing key changes.
   → verify: existing `load_spec_metadata` callers/tests unaffected (`grep -rn load_spec_metadata`).
2. Same file: add a module-level helper
   `_creation_time(spec_dir: Path, stamped: str | None, stat: os.stat_result) -> tuple[str, bool]`
   returning `(iso, is_estimate)`:
   - a `stamped` value that `datetime.fromisoformat` accepts → `(stamped, False)`;
   - else the oldest `st_mtime` of `requirements.json`/`spec.md` that can be `stat`ed →
     `(iso, True)`;
   - else `(datetime.fromtimestamp(stat.st_ctime).isoformat(), True)`.
   Never raises: any `OSError`/`ValueError` falls to the next source, logged at debug.
   → verify by step 5.
3. Same file, `spec_to_task` (`:960`): use the helper for `created_at` and pass the flag.
   `updated_at` untouched.
   → verify by step 5.
4. `apps/web-server/server/routes/task_models.py` (`Task`, `:125-134`): add
   `created_at_is_estimate: bool = Field(False, description="created_at is inferred, not recorded at creation")`.
   → verify: `python -c "from server.routes.task_models import Task"` and step 6.
5. New `tests/test_task_created_at.py`:
   - **the #1569 case**: a spec with a stamp → that exact value, flag false; then touch a file
     inside the spec dir and re-read → **the value does not move** (this fails on today's code);
   - no stamp → oldest of `requirements.json`/`spec.md` mtime, flag true;
   - malformed stamp (`"yesterday"`) → falls through, flag true;
   - neither file readable → dir ctime, flag true;
   - a listing of several specs still sorts (mirrors `routes/tasks.py:186`).
   → verify: `apps/backend/.venv/bin/pytest tests/test_task_created_at.py -v` passes;
   mutation: revert step 3 to `st_ctime` → the first test fails.
6. Regenerate the OpenAPI spec, which CI checks for drift (`techdocs.yml:88-118`):
   `APP_DISABLE_AUTH=true apps/web-server/.venv/bin/python scripts/generate-openapi-spec.py`
   from the repo root, and commit `apps/web-server/openapi.yaml` in the same commit.
   → verify: `git diff --quiet -- apps/web-server/openapi.yaml` after a second run, and the
   diff shows only the new field.
7. Lint gates: `ruff format --check apps/backend apps/web-server scripts tests`; after
   committing, both cq-ratchets (`--base origin/dev`, ruff and mypy) at "0 regressed"
   (they diff committed history — see the memory note).

## Tests

```bash
apps/backend/.venv/bin/pytest tests/test_task_created_at.py -v
apps/backend/.venv/bin/pytest tests/ -k "task_service or spec_to_task or tasks_route" -q
(cd apps/web-server && ../backend/.venv/bin/pytest tests -q -o asyncio_mode=auto -k "task")
```
Expected: all pass; the existing task-listing tests are unchanged.

## Rollback

Revert the commit. Nothing is written to task data, so there is nothing to undo on disk; the
API returns the old ctime-based value again.
