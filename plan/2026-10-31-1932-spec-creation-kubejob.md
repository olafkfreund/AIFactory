---
status: approved
issue: 1932
spec: spec/2026-10-31-1932-spec-creation-kubejob.md
coders:
  1: codex   # the lane brief names Codex as this team's coder
  2: codex
  3: codex
  4: codex
---

# Plan: spec creation as a Kubernetes Job, behind a flag (#1932 A1-A3)

## Approved decisions

These are carried over from the spec. Each one is binding.

- Flag `AIFACTORY_SPEC_BACKEND`: unset, empty or `subprocess` keeps today's
  path. `kubejob` runs a Job. Any other value raises
  `BuildBackendUnavailableError` from `start_spec_creation`. It never falls back
  to in-pod.
- `start_spec_creation` is unchanged up to the spawn. At the spawn it calls
  `KubeJobSpecBackend.dispatch`, which returns a `SpecJobProcess` with the
  Process surface. Everything after the spawn is shared. For a Job,
  `_monitor_process` gets `cmd=None, env=None` (no in-pod model-fallback
  respawn). A task id with no `:` is refused when the flag is on.
- Workspace: a `git clone --local --no-hardlinks` of the project at
  `<project>/.aifactory/worktrees/specs/<spec_id>`, plus
  `core.workspace.copy_spec_to_worktree` of the control-plane spec dir, plus
  `.aifactory/spec_task.md` holding `f"{title}\n\n{description}"`. It is packed
  with `core.artifact_store.pack_workspace` (fail loud) and the clone is then
  removed. There is no PVC co-mount.
- Manifest: `JobSpec(service="aifactory", job_id=f"{task_id}-spec", ...)`, with
  the image, SA and namespace resolved the way builds do, `nix_develop=False`,
  `workspace_uri=<uri>`, `data_pvc=None`, `deadline_seconds=7200` and
  `automount_service_account_token=False`. Then `_inject_seed_creds` and
  `_inject_build_path`.
- Command, every value `shlex.quote`d: `python
  <APP_BACKEND_PATH>/runners/spec_runner.py --task-file
  /work/.aifactory/spec_task.md --project-dir /work --spec-dir
  /work/.aifactory/specs/<spec_id> --no-build --auto-approve [--model M]
  [--complexity C]`.
- Env: `build_job_env(token)` minus `GITHUB_TOKEN`/`GH_TOKEN`, plus
  `mcp_spawn_env(project_path)`, `AIFACTORY_MARKER_NONCE`, `AIFACTORY_TASK_ID`,
  `QUICK_MODE=true` when `complexity == "simple"`, and `AIFACTORY_SPEC_FETCH_ID`
  set to `uuid4().hex`.
- Job side: `spec_runner.py` unpacks `WORKSPACE_URI` into `--project-dir`
  right after argument parsing, before `--task-file` is read. After success it
  calls `maybe_push_spec_files(spec_dir)`, one object per allowlisted file
  under `ArtifactRef(service="aifactory", job_id=<fetch id>, role="doc",
  path=f"spec/{name}")`. The push never raises and does nothing without
  `AIFACTORY_SPEC_FETCH_ID`.
- Fetch-back `fetch_spec_files(spec_dir, fetch_id)` reads only
  `SPEC_FETCH_FILES` = (`spec.md`, `implementation_plan.json`,
  `context.json`, `project_index.json`, `research.json`,
  `complexity_assessment.json`, `critique_report.json`, `graph_hints.json`,
  `architecture.md`). Each file must be at most 8 MiB. `.json` must parse to a
  dict or list and `.md` must decode as UTF-8. Write with a temp file and
  `os.replace`. `requirements.json`, `task_metadata.json`, `task_control.json`
  and `review_state.json` are never fetched.
- After the fetch, the control plane sets the review state. With
  `should_auto_approve` it calls `ReviewState.load(d).approve(d,
  approved_by="auto")`. Otherwise, if `not is_approval_valid(d)`, it calls
  `invalidate(d)`. If `spec.md` did not come back, or the Job failed or
  vanished, `returncode` is 1.
- Logs: `KubeJobLogStreamer(log_sink=<feed line + "\n" into stdout>)` with
  `job_active` = no outcome yet. It gets up to 30 s to drain, then stdout gets
  EOF. stderr is an empty reader that already has EOF.
- Reconcile: a background poller calls `KubeJobBuildBackend._job_outcome`
  every 10 s. `wait()` is `await asyncio.shield(poller)`.
- Stop: `send_signal`/`terminate`/`kill` set an event. The poller deletes the
  Job (Background propagation) and returns -15.
  `AgentService._signal_process_tree` calls `proc.send_signal(sig)` when `pid`
  is None.
- Orphans: a module-level set of live spec Job names is added to `owned` in
  `KubeJobBuildBackend.sweep_orphan_jobs`.
- Docs: an `AIFACTORY_SPEC_BACKEND` row in `docs/docs/environment-reference.md`.
  No gitops change.

## Steps

1. `apps/backend/core/workspace_fetch.py` (append after `maybe_fetch_memory`)
   and `apps/backend/runners/spec_runner.py` (~242, right after
   `args = parser.parse_args()`, and ~330, after
   `debug_success("spec_runner", "Spec creation succeeded", ...)`). Add
   `SPEC_FETCH_ID_ENV = "AIFACTORY_SPEC_FETCH_ID"`, `SPEC_FETCH_FILES`,
   `_SPEC_FILE_MAX_BYTES = 8 << 20`, `_spec_file_key(fetch_id, name)`,
   `maybe_push_spec_files(spec_dir) -> int` and
   `fetch_spec_files(spec_dir, fetch_id) -> list[str]`, as decided above. In
   spec_runner, call `maybe_unpack_workspace(args.project_dir)` (deferred
   import, like `cli/main.py` ~406) and `maybe_push_spec_files(orchestrator.spec_dir)`.
   → verify by `apps/backend/.venv/bin/python -m pytest tests/test_spec_job_backend.py -k "fetch or push" -q`
   (the tests come from step 4. Until then, check with `ruff check` on both files.)
   Traps: lazy-import `core.artifact_store` inside the functions, like the
   siblings, to keep boto3 off the import path. Do not log file contents.

2. New `apps/web-server/server/services/spec_job_backend.py`, plus
   `build_backend.py` `sweep_orphan_jobs` (~1681, the `owned = {...}` line) and
   `agent_service.py` `_signal_process_tree` (~1404, the `if pid is None:
   return` branch). New module: `_ENV_SPEC_BACKEND`, `spec_kubejob_enabled()`,
   `_LIVE_SPEC_JOBS` / `live_spec_job_names()`, `build_spec_job_manifest(...)`,
   `_prepare_spec_workspace(...)`, `class SpecJobProcess`, and
   `class KubeJobSpecBackend` with `dispatch(...) -> SpecJobProcess`. In
   `sweep_orphan_jobs`, add `owned |= live_spec_job_names()` (deferred import).
   In `_signal_process_tree`, a `pid is None` proc gets
   `proc.send_signal(sig)` under `contextlib.suppress(ProcessLookupError,
   OSError)`.
   → verify by `ruff check` on the three files, then
   `apps/backend/.venv/bin/python -m pytest tests/test_build_backend_kubejob.py -q`.
   Traps: reuse `build_job_env`, `_resolve_build_image`,
   `_resolve_run_py_path` (take its dirname for `runners/spec_runner.py`),
   `_inject_seed_creds`, `_inject_build_path`, `_dispatch_namespace`,
   `KubeJobBuildBackend._batch_api` and `_job_outcome`. Do not copy them.
   `core.*` imports are lazy (see `build_run_py_job_manifest`). Never put the
   token or task text in argv or logs. Remove the name from `_LIVE_SPEC_JOBS`
   when the poller ends. Close the batch api_client in `finally`.

3. `apps/web-server/server/services/agent_spec_creation.py`
   `start_spec_creation`. After the gated-task refusal (~102), compute
   `use_job = spec_kubejob_enabled()`, and refuse when `use_job and spec_dir is
   None`. Replace the pty + `create_subprocess_exec` block (~290-305) with an
   `if use_job:` branch that awaits `KubeJobSpecBackend().dispatch(task_id=...,
   project_path=..., spec_id=..., spec_dir=..., task_text=f"{title}\n\n{description}",
   model=spec_phase_model, complexity=complexity, auto_approve=should_auto_approve,
   oauth_token=token, mcp_env=await project_settings_store.mcp_spawn_env(project_path),
   marker_nonce=self._marker_nonce_env(task_id)["AIFACTORY_MARKER_NONCE"])`.
   The else branch is the existing pty path, unchanged. Pass `cmd=None if use_job
   else cmd` and the same for `env` to `_monitor_process`.
   → verify by `apps/backend/.venv/bin/python -m pytest tests/test_agent_spec_creation_mixin.py -q`.
   Traps: `build_sandboxed_command` only applies in-pod. Keep the existing
   tests' source-string asserts (`build_chain_args(self._kubejob_backend_enabled())`,
   `cmd.extend(chain_args)`) true.

4. New `tests/test_spec_job_backend.py`, covering the cases in the spec's
   Verification section. Use a fake batch client (`create_namespaced_job`,
   `read_namespaced_job`, `delete_namespaced_job`) and a fake
   `ArtifactStore` (monkeypatch `core.artifact_store.ArtifactStore` and
   `pack_workspace`), with `git init` in `tmp_path` for the project.
   → verify by `apps/backend/.venv/bin/python -m pytest tests/test_spec_job_backend.py -q`.
   It must fail on origin/dev: the module does not exist there.
   Traps: test files need `sys.path` set the way
   `tests/test_agent_spec_creation_mixin.py` does it. Check
   `uncollected-tests-allowlist.toml`/conftest collection rules.

5. `docs/docs/environment-reference.md` (~246). Add an `AIFACTORY_SPEC_BACKEND`
   row after `AIFACTORY_BUILD_BACKEND`. I (Claude) do this step myself: it is a
   one-line doc edit.
   → verify by reading the rendered row.

## Deviations (from the codex and agy security reviews of the diff)

Each was confirmed against the code and fixed in the same commit as this note.

- A failed, vanished or stopped spec Job (`proc.pid is None`, non-zero return
  code) skips the monitor's spec-completion branch, so it cannot auto-start a
  build from stale spec files (`process_monitor.py`). In-pod runs are unchanged.
- `ArtifactStore.get_bytes` takes `max_bytes`; `fetch_spec_files` reads at most
  8 MiB + 1, so an oversize object is skipped without being loaded.
- `_prepare_spec_workspace` refuses a spec dir that is or contains a symlink,
  and a clone whose `.aifactory` or `.aifactory/specs` is a symlink.
- The Job id is `f"{task_id}-spec-{fetch_id[:8]}"`, not `f"{task_id}-spec"`:
  the 20-character short name collided on re-runs and across projects.
- `_inject_seed_creds` takes the credential file list. The spec Job gets the
  Copilot login only when its model is a `copilot:` model.
- A fetch that brings back no `spec.md` invalidates any earlier approval.
- From the final review: the clone check also covers
  `.aifactory/specs/<spec_id>` and `.aifactory/spec_task.md`; a stop ends the
  log stream at once instead of after the 30 s drain; a dispatch that raises
  releases the pooled credential; the packed workspace key carries the same
  per-dispatch suffix as the Job id.
- Not fixed here, tracked: bucket-wide `S3_*` in Jobs (#2023), the marker
  nonce in the Job env (#2024), unused CLI logins in build Jobs (#2025).

## Tests

- `apps/backend/.venv/bin/python -m pytest tests/test_spec_job_backend.py tests/test_agent_spec_creation_mixin.py tests/test_build_backend_kubejob.py -q`: all pass.
- `ruff check` and `ruff format --check` on the touched files, and
  `python scripts/ratchet_lint.py` with no regression.
- The pre-commit hook suite passes on each commit.

## Rollback

Revert the PR. With the flag unset, nothing changed at runtime, so a revert is
safe at any point before the gitops flip. After the flip, unset
`AIFACTORY_SPEC_BACKEND` first.
