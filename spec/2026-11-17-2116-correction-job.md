---
status: approved
issue: 2116
intent: intent/2026-11-17-2116-correction-job.md
---

# Spec: QA apply-correction as a Job (#2116)

## Design

Flag: `correction_kubejob_enabled()` in `agent_job_backend.py` reads `AIFACTORY_CORRECTION_BACKEND` (`subprocess` default, `kubejob`; any other value raises `BuildBackendUnavailableError`). Independent of `AIFACTORY_REVIEW_BACKEND`. Requires DATABASE_URL (artifact store), like review Jobs.

Route (`apply_task_correction`): on `confirm=true`, if the flag is on, pass `fixer_fn=dispatch_correction_fixer` to `apply_correction` and skip the FORBID refusal (the Job path is the allowed path); if the flag is off the existing `inpod_agent_refusal()` 409 is unchanged. `BuildBackendUnavailableError` (bad flag value, missing prerequisites, packing failure) becomes 409 with the message; there is never a fallback to `_default_fixer`. `apply_correction` already validates triage and writes `handback_received.json` and `QA_FIX_REQUEST.md` before calling the fixer, so those stay control-plane work and the Job sees them.

Dispatch (`dispatch_correction_fixer(spec_dir)`): packs a workspace made of a `git clone --local --no-hardlinks` of the project dir (the dir the fixer edits, `spec_dir.parent.parent.parent`; committed state only) plus the spec dir's top-level regular files (no symlinks, 1 MiB each) at `.aifactory/specs/<id>`. Symlink checks cover the source and the fresh clone's `.aifactory/specs` ancestors before any copy (as `_prepare_review_workspace`); origin restored without credentials (`_restore_origin`). Job env is `build_job_env` minus every GitHub name (`github_names()`, `GITHUB_*`, `GH_*`); the `S3_*` endpoint and bucket stay so the artifact store works, then `scope_job_s3` swaps the bucket-wide key for credentials scoped to the Job and a fresh fetch id (it raises when scoping fails, and it is a silent no-op when scoping is off or no S3 key is set, so dispatch also refuses unless the env then holds scoped `AWS_*` credentials and no `S3_ACCESS_KEY`/`S3_SECRET_KEY`). `AIFACTORY_CORRECTION_FETCH_ID` carries the fetch id. Manifest via `build_agent_job_manifest` (no service-account token, 1800 s deadline, models sonnet/opus). Argv: `python <backend>/qa/correction_job.py --project /work --spec-id <id>`. Returns immediately with `{"status":"qa_fixing","scheduled":true,"job":<name>}`; a strong-referenced background task awaits the `SpecJobProcess` (whose `finish` does the fetch) and then runs `check_fix_cycle_assertions(spec_dir)` on the control plane against the applied tree (#467), whatever the outcome. The task writes `handback_job_result.json` (status, job name, time) into the spec dir so success or failure is recorded, and logs failures.

Job entrypoint (`qa/correction_job.py`): `maybe_unpack_workspace`, record `base = git rev-parse HEAD`, run the existing fixer routine (`_run_fixer_bg`, changed to return True only when `run_qa_fixer_session` returns status `"fixed"`; an `"error"` status or an exception is False, and the exit code is then non-zero so nothing is applied). Then `maybe_push_correction`: the fixer prompt commits its work, so changes are collected against `base`, not the working tree alone: `git diff --name-status --no-renames -z <base>` (committed and uncommitted) plus `git ls-files -o --exclude-standard -z`. Anything under `.git` or `.aifactory` is skipped; symlinks and non-regular files are refused. One JSON object is uploaded: `{files: {path: {b64, base_sha256}}, deleted: {path: base_sha256}}` where `base_sha256` is the hash of the file at `base` (null for a new file). Caps: 500 entries, 8 MiB decoded.

Fetch (`fetch_correction` in `core/workspace_fetch.py`, keyed by the fetch id, under a process-wide lock so two corrections never apply at once): download cap 12 MiB; must be a JSON object with the keys above and the right types; every path must be relative, POSIX, normalised, with no `.`/`..`/empty part, no `.git` or `.aifactory` component, no symlinked ancestor, and resolve inside the project; no path may be a prefix of another, none both written and deleted; entry and decoded-size caps re-checked; a target that exists as a symlink or directory is refused. Conflict check: the current local file's sha256 must equal `base_sha256` (absent for a new file), otherwise the whole result is rejected as a conflict (local edits, or another correction, landed since dispatch). All temp files are written first; then each is replaced in turn, keeping the original bytes in memory, and any failure or cancel restores the replaced files and removes created ones (so the set is applied fully or not at all, barring a crash mid-restore). Any violation fails the Job result with nothing applied.

Docs: `AIFACTORY_CORRECTION_BACKEND` in `docs/docs/environment-reference.md`, CHANGELOG.

## Alternatives rejected

- Run `run.py --spec X --qa` through the build backend: it runs the whole QA loop (reviewer too), not the one fixer pass the endpoint promises, and a standalone `--qa` pushes nothing back on the packed path.
- Push a branch to origin from the Job: needs a GitHub write credential in the Job and moves the result outside control-plane validation.
- Return a git patch: needs a diff parser to police renames, modes and symlinks; a file set is simpler to validate.
- Reuse `AIFACTORY_REVIEW_BACKEND`: couples two unrelated rollouts; the issue asks for its own flag.

## Risks

Uncommitted changes in the project dir are not in the Job's clone (the fixer sees the last commit); if the fixer then changes such a file the base-hash check rejects the result, which is safe and loud. A fixer can still edit any non-control file, as in-pod, but now only through validated writes. Job cost; flag off by default, so no production change until the owner sets it. The control-plane assertion guard runs after apply, as the in-pod one runs after the fixer.

## Verification

Tests with a fake batch client: flag parsing; manifest has no service-account token and the right argv; env has no GitHub or S3 bucket-wide key; dispatch with FORBID on creates a Job and never spawns in-pod; route 409 unchanged when the flag is unset; route with flag on dispatches and never calls `_default_fixer`; dispatch failure refuses with no fallback; `fetch_correction` accepts a valid set and rejects oversize, non-object, traversal, `.git`/`.aifactory` paths, symlink ancestor, symlink target, overlapping paths, too many entries, hash conflict, and rolls back when a write fails after the first; push side collects committed changes against the base, skips symlinks and control paths; `_run_fixer_bg` returns False for status error; scoping failure refuses dispatch. Mutation check: flip the flag branch, the path check and the size cap in turn and confirm a test fails. `pytest` on touched files, ruff check and format.

### Revisions after Codex plan review (2026-11-17)

- No plan is returned from the Job. A returned `implementation_plan.json` could clear control-plane-owned state (`qa_signoff`, `contractHold`, status keys). Completion is the Job's exit status; the control plane records it in `handback_job_result.json`.
- Scoped S3 credentials are mandatory for this Job (see Dispatch), unlike review Jobs, because the Job's output is a code edit.
- Prerequisites: DATABASE_URL (`_require_prerequisites`), copied spec files capped at 1 MiB each (an oversized one refuses dispatch), clone with `--local --no-hardlinks`, models sonnet and opus for credentials.
