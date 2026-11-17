---
status: draft
issue: 2116
spec: spec/2026-11-17-2116-correction-job.md
coders:
  1: codex   # contained, tricky validation and rollback logic with its own tests
  2: you     # small, needs the fixer routine context
  3: you     # dispatch, route wiring, docs; builds on steps 1 and 2
reviewers:
  - codex
---

# Plan: QA apply-correction as a Job (#2116)

Decisions carried from the approved spec: flag `AIFACTORY_CORRECTION_BACKEND` (`subprocess` default, `kubejob`, other values raise `BuildBackendUnavailableError`), separate from `AIFACTORY_REVIEW_BACKEND`; flag on => the route passes a Job-backed `fixer_fn` to `apply_correction` and skips the FORBID 409, flag off => the existing 409 and in-pod path are unchanged; `BuildBackendUnavailableError` becomes 409, never a fallback to `_default_fixer`. Workspace = local clone of the project dir (committed state) plus the spec dir's top-level regular files (symlink checks on source and clone ancestors, no GitHub credential, `S3_*` endpoint kept, `scope_job_s3` for the Job id and a fresh fetch id, refuse on scoping failure), manifest from `build_agent_job_manifest`, 1800 s. Job entrypoint `qa/correction_job.py`: unpack, record `base` HEAD, run the fixer (`_run_fixer_bg` returns True only for status `"fixed"`, exit non-zero otherwise), push one JSON object `{files:{path:{b64,base_sha256}}, deleted:{path:base_sha256}, plan}` collected against `base` (committed and uncommitted and untracked; skip `.git`/`.aifactory`; refuse symlinks; caps 500 entries and 8 MiB decoded; plan only if a JSON object of at most 1 MiB). Control plane `fetch_correction`: 12 MiB download cap, type checks, path rules (relative, normalised, no `.git`/`.aifactory`, no symlinked ancestor, inside the project, no overlap or write+delete), base-hash conflict check, process-wide lock, temp files first then replace with in-memory rollback, plan written last; cancel honoured. After the Job the background task writes `handback_job_result.json` and runs `check_fix_cycle_assertions`. The Dispatch returns `{"status":"qa_fixing","scheduled":true,"job":name}`.

## Steps

1. `apps/backend/core/workspace_fetch.py` (append after `fetch_pr_review`, ~line 1102): add `CORRECTION_FETCH_ID_ENV = "AIFACTORY_CORRECTION_FETCH_ID"`, `maybe_push_correction(project_dir, spec_dir, base)` and `fetch_correction(project_path, spec_dir, fetch_id, cancel=None) -> bool` as specified. Tests in new `tests/test_workspace_fetch_correction.py` (real git repo in tmp_path, `ArtifactStore` faked in memory): valid set applied; rejects oversize, non-object, traversal, `.git`/`.aifactory` paths, symlink ancestor, symlink target, overlapping paths, too many entries, base-hash conflict; rollback when a replace fails after the first; push collects a committed change against `base`, an untracked file and a deletion, skips symlinks and control paths. → `cd apps/backend && python -m pytest tests/test_workspace_fetch_correction.py -q` (use the repo's test layout).
   Traps: do not edit `artifact_store.py` (vendored, byte-checked); plain-name imports (ruff "as" alias rule); no secrets in logs; the file is imported without boto3 at module load (defer imports like the neighbours).
2. `apps/backend/qa/correction.py` `_run_fixer_bg` (~line 150-200): return `bool` (True only when `run_qa_fixer_session` returns status `"fixed"`), keep best-effort and the `finally` guard. New `apps/backend/qa/correction_job.py`: argparse `--project`, `--spec-id`; insert backend dir in `sys.path`; `maybe_unpack_workspace`, `base = git rev-parse HEAD`, `asyncio.run(_run_fixer_bg(spec_dir))`, `maybe_push_correction`, exit 1 when the fixer was not fixed or the push failed. Tests in `tests/test_correction_job.py` (fake fixer; exit code for fixed/error; existing `tests/test_qa_correction*.py` still pass). → pytest those files.
   Traps: `_default_fixer` still ignores the return value; keep it detached.
3. `apps/web-server/server/services/agent_job_backend.py`: `correction_kubejob_enabled()`, `_prepare_correction_workspace`, `dispatch_correction_fixer(spec_dir)` plus the background waiter (strong reference set, writes `handback_job_result.json`, runs the guard). `apps/web-server/server/routes/execution.py` `apply_task_correction`: flag on => `fixer_fn`, no FORBID 409; flag off => unchanged; `BuildBackendUnavailableError` => 409. Docs line in `docs/docs/environment-reference.md`, CHANGELOG entry. Tests in `apps/web-server/tests/test_correction_job_backend.py` with a fake batch client: flag parsing; manifest (no SA token, 1800 s, argv, no GitHub env, S3 endpoint kept, scoping failure refuses); FORBID on + flag on creates a Job and never calls `_default_fixer` or spawns; flag unset keeps the 409 (extend `test_inpod_slice_d.py` style) and writes nothing; dispatch failure => 409 and no fallback; waiter writes the result file and runs the guard. Mutation check by hand: flip the flag branch, the path validation and the size cap, see a test fail, revert. → `pytest apps/web-server/tests/test_correction_job_backend.py apps/web-server/tests/test_inpod_slice_d.py apps/web-server/tests/test_agent_job_backend.py`; `ruff check` and `ruff format --check` on touched files.
   Traps: AIFactory ruff sorting of "as" aliases (plain names); the generated autonomy matrix, if touched by CI, is regenerated never hand-merged; do not edit factory-gitops (owner action: set `AIFACTORY_CORRECTION_BACKEND=kubejob`).

## Tests

Commands above plus the full touched test directories; ruff check and format; CI workflow checks on the PR.

## Rollback

Revert the PR. Flag off by default, so nothing changes in production until the owner sets it.
