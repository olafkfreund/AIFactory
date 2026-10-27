---
status: approved
issue: 1940
spec: spec/2026-10-27-1940-plan-command-snapshot.md
---

# Plan: plan-command grants come only from the plan the build was allowed to start with

Approved decisions (from the spec):

- When review is required before coding, parse `implementation_plan.json`
  once per run, before any agent session, and grant plan commands only from
  that snapshot for every session (serial and parallel wave children).
  Missing/unreadable plan at start → `{}` (grants nothing).
- When review is not required (incl. solo), behaviour is unchanged: seed from
  the live plan.
- `trusted_plan.py` (seeds once at signed-plan ingest) is unchanged.
- The agent-writable profile file is a separate follow-up issue; not touched here.

All paths below are under `apps/backend/` unless they start with `tests/`.

## Steps

1. `project/analyzer.py:441-466` `seed_profile_with_plan_commands`: add keyword
   `plan: dict | None = None`. If `plan is not None` use it; else read
   `plan_path` exactly as now. Add one docstring line: callers pass `plan`
   to grant from a snapshot instead of the live, agent-writable file (#1940).
   → verify: `pytest tests/test_plan_seed.py -q` still green.
   Traps: keep the read error handling; do not change the return value.

2. `agents/coder.py`:
   a. Add module-level helper near `_should_require_human_review` (~1208):
      `_approved_plan_snapshot(spec_dir: Path, require_review: bool) -> dict | None`.
      Returns `None` when not `require_review`; else `json.loads` of
      `spec_dir / "implementation_plan.json"` if it is a dict, otherwise `{}`
      (also `{}` on `OSError`/`json.JSONDecodeError`/missing file).
   b. In `run_autonomous_agent`, right after `require_review = ...` (~214):
      `approved_plan = _approved_plan_snapshot(spec_dir, require_review)` with a
      one-line comment (#1940: grants come only from the plan the start gate saw).
   c. Per-session seed (~384-386): `seed_profile_with_plan_commands(project_dir, plan_file, plan=approved_plan)`.
      Update the comment above it to say grants come from the run-start snapshot
      when review is required.
   d. `_maybe_run_parallel_phase` (def ~1436, call ~644): add keyword
      `approved_plan: dict | None = None`, pass it from the call site, and pass
      it on to `run_parallel_coding_phase(..., approved_plan=approved_plan)` (~1485).
   → verify: `ruff check` + `ruff format --check` on the file; `python scripts/ratchet_lint.py` if present.
   Traps: `json` is already imported in coder.py; mypy ratchet type-checks this file alone.

3. `agents/parallel_integration.py`: `run_parallel_coding_phase` (~428) gains
   keyword `approved_plan: dict | None = None`; the wave-child seed call (~523-528)
   passes `plan=approved_plan` (`asyncio.to_thread(seed_profile_with_plan_commands, child_path, _child_plan, plan=approved_plan)`).
   → verify: ruff; `pytest tests -q -k parallel` for touched code paths.
   Traps: keep the bare `except Exception: pass` around seeding.

4. `tests/test_plan_seed.py`: add tests (import `_approved_plan_snapshot` from `agents.coder`):
   - review required: write plan with `["uv","pytest"]`, snapshot, rewrite live
     plan adding `"npx"`, seed with `plan=snapshot` → granted == {uv, pytest}, and
     `validate_command("npx foo", project_dir)` is blocked.
   - review required, no plan file → snapshot `{}`, seed returns `[]`.
   - review not required → snapshot `None`; seed with `plan=None` after the live
     edit grants `npx` (as today).
   → verify: `pytest tests/test_plan_seed.py -q`; the first test fails if step 2c is reverted.
   Traps: call `reset_profile_cache()` at the start of each test like the existing ones.

## Tests

```
cd apps/backend && .venv/bin/python -m pytest ../../tests/test_plan_seed.py ../../tests/test_review_validation.py -q
ruff check apps/backend/agents/coder.py apps/backend/agents/parallel_integration.py apps/backend/project/analyzer.py tests/test_plan_seed.py
```
Expected: all pass.

## Rollback

Revert the PR. Seeding returns to reading the live plan; no data migration.
