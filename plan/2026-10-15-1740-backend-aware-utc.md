---
status: approved
issue: 1740
spec: spec/2026-10-15-1740-backend-aware-utc.md
---

# Plan: write aware UTC timestamps in apps/backend and provider_benchmark

Approved decisions (from the spec):

- Writers use `datetime.now(UTC)` and `datetime.fromtimestamp(ts, UTC)`, with
  `from datetime import UTC` added to the file's existing `from datetime import ...`
  line. The file keeps its own import style.
- The only exception is `core/debug.py`'s six console clock lines. They keep
  `datetime.now()` with `# noqa: DTZ005 - local wall-clock console time`.
- Readers that load stored timestamps and compare them use
  `datetime.fromisoformat(s).astimezone(UTC)`. A naive legacy value was local
  time, and `astimezone` reads naive values as local. Do not use `.replace(tzinfo=UTC)`.
- `apps/backend/runners/github/` is out of scope (ruff-excluded vendored dir).
  `bot_detection.py` is self-consistent naive and stays unchanged.
- Remove the DTZ per-file-ignores from `ruff.toml`.

Check environment: `export PATH=<main clone>/apps/backend/.venv/bin:$PATH`, run from the repo root.

## Steps

1. **Merge readers + regression test.**
   - `apps/backend/merge/types.py:15`: import `UTC`. Lines 405, 406, 459: append
     `.astimezone(UTC)` to each `datetime.fromisoformat(...)`.
   - `apps/backend/merge/timeline_models.py:18`: import `UTC`. Lines 67, 97,
     118, 220, 300, 301: same.
   - `tests/test_merge_types.py`: add a test. Build a `FileEvolution` via
     `FileEvolution.from_dict` whose one task snapshot has a naive
     `started_at` ("2026-01-01T10:00:00"). Call `add_task_snapshot` with a
     `TaskSnapshot` whose `started_at=datetime.now(UTC)`. Assert that nothing
     raises, there are 2 snapshots, and every `started_at.tzinfo is not None`.
   - Check: `pytest tests/test_merge_types.py -q`. The new test must fail with
     `TypeError` when the two merge files are reverted.
   - Deviation (done): `TestTaskSnapshot`'s roundtrip fixture now uses an aware
     `started_at`, because the loader returns aware values and a naive original no
     longer compares equal.
   - Traps: keep the `if data.get(...) else None` conditionals intact. Do not
     reformat untouched code.
2. **Merge writers.** `datetime.now()` → `datetime.now(UTC)` in
   `merge/file_evolution/baseline_capture.py:158`,
   `merge/file_evolution/modification_tracker.py:115,124,327`,
   `merge/orchestrator.py:232,233,263,294,334,337,387,394,656`,
   `merge/prompts.py:526`, `merge/timeline_models.py:250,260`,
   `merge/timeline_persistence.py:120`,
   `merge/timeline_tracker.py:128,199,248,281,288`.
   - Check: `ruff check --select DTZ --config 'lint.per-file-ignores={}' apps/backend/merge`
     reports 0, and `pytest tests/test_merge_*.py -q` passes.
   - Deviation (done): also `merge/timeline_models.py:244-245`
     `field(default_factory=datetime.now)` → `lambda: datetime.now(UTC)`. A bare
     reference is invisible to DTZ005, found by grep.
   - Traps: `orchestrator.py` subtracts `start_time` from `completed_at`, so
     both must be aware (lines 233/263/294 and 337/387).
3. **Remaining backend writers.** `datetime.now()` → `datetime.now(UTC)` at:
   `agents/artifacts.py:57`, `agents/build_report.py:137`, `agents/wave_log.py:46`,
   `ideation/formatter.py:93,98,118`, `ideation/phase_executor.py:94,140,203`,
   `implementation_plan/factories.py:52,124,159`, `implementation_plan/plan.py:140`,
   `integrations/graphiti/config.py:530`, `project/analyzer.py:258`,
   `query_memory.py:196,262,384,398,409,475`, `review/html_generator.py:242`,
   `review/state.py:146,184`, `runners/ai_analyzer/runner.py:67`,
   `runners/roadmap/competitor_analyzer.py:163,181`,
   `runners/roadmap/graph_integration.py:90,104,118`, `spec/complexity.py:540`,
   `spec/context.py:105`, `spec/phases/quick_optimizations.py:218`,
   `spec/phases/requirements_phases.py:96`, `spec/requirements.py:153,163`,
   `spec/validator.py:23,44,63`, `spec/writer.py:44`, `ui/status.py:76,155,245`.
   Plus:
   - `spec/pipeline/models.py:71-72`: `datetime.fromtimestamp(..., UTC)` and
     `datetime.now(UTC)`.
   - `services/recovery.py:183-193`: parse with
     `datetime.fromisoformat(attempt["timestamp"]).astimezone(UTC)`, delete
     `cutoff_time_naive` and the conditional, and compare against `cutoff_time`.
   - `core/debug.py:135,177,192,207,222,237`: append
     `  # noqa: DTZ005 - local wall-clock console time`.
   - Check: `ruff check --select DTZ --config 'lint.per-file-ignores={}' apps/backend`
     (tests are excluded by path, so look only at non-test hits). Expect 0 outside
     `runners/github/`, which the config excludes. Then run
     `pytest tests -q -k "recovery or spec or ideation or implementation_plan or status or query_memory or review or build_report or wave_log or artifacts"`.
   - Traps: a file may use `import datetime` rather than `from datetime import`.
     Match its style. Line numbers may shift after step 2; grep the call.
4. **Script, web-server, gate.**
   - `scripts/provider_benchmark.py:234` → `datetime.now(UTC)` (import `UTC`).
   - `apps/web-server/server/routes/context.py:207`: replace
     `__import__("datetime").datetime.now().isoformat()` with an aware UTC call,
     using the file's existing datetime import if it has one, or else
     `__import__("datetime").datetime.now(__import__("datetime").UTC)`. Prefer a
     normal import.
   - Deviation (done): a module-level import is used, so the function-local
     `from datetime import UTC, datetime` (old line 745) is removed, and `index` is
     annotated `dict[str, Any]`. The `__import__` call had typed it `Any`, and
     without the annotation mypy --strict infers `object` (ratchet 18 -> 20).
   - `ruff.toml`: delete lines 227-229 (the carve-out comment and both entries).
     Reword the comment on line 127 to
     `# #1624/#1740: aware UTC timestamps, gated repo-wide (tests exempt below)`.
   - Check: `ruff check .` and `ruff format --check` on all touched files.

## Tests

- `pytest tests/test_merge_types.py -q`: the new test passes, and fails on base.
- `pytest tests/test_merge_*.py tests -q -k "<step 3 keywords>"`: green.
- `ruff check .`: 0 errors with the carve-out removed.

## Rollback

Revert the PR. Files written in the meantime hold aware `+00:00` strings,
which every reader parses. Reverting the merge loaders would bring back the
mixed naive/aware `TypeError`, so revert writers and readers together.
