---
status: draft
issue: 1932
intent: intent/2026-10-31-1932-spec-creation-kubejob.md
---

# Spec: spec creation as a Kubernetes Job, behind a flag (#1932 A1-A3)

Decision recorded: the owner chose option B on #1932 (2026-10-31). Every LLM
agent session in prod runs as a Kubernetes Job, and the subprocess backend
becomes local-dev only. This spec covers slices A1-A3 (spec creation). It
refines section A of `spec/2026-10-27-1932-in-pod-agent-isolation.md`. Where
the two differ, this spec wins, and the reason is given inline.

## Design

### Flag

`AIFACTORY_SPEC_BACKEND`, read on every call (strip, lower):

- unset, empty or `subprocess`: today's in-pod path, byte for byte.
- `kubejob`: spec creation runs as a Job.
- anything else: `start_spec_creation` raises `BuildBackendUnavailableError`
  naming the valid values. It never falls back to in-pod (same rule as #1942).

The flag does not depend on `AIFACTORY_BUILD_BACKEND`. The Job always passes
`--no-build`. The build that follows is started by the existing post-spec
handling, which goes through `_start_build_unit`.

### Where it branches

`SpecCreationMixin.start_spec_creation` keeps everything up to the spawn:
the running check, `backend_config_error`, the gated-task refusal,
`should_auto_approve` and `spec_phase_model` from the control-plane
`task_metadata.json`/`requirements.json`, and the pooled token. At the spawn
it calls `KubeJobSpecBackend.dispatch(...)` instead of
`asyncio.create_subprocess_exec`. Dispatch returns a `SpecJobProcess`, an
object with the `asyncio.subprocess.Process` surface the rest of the method and
`process_monitor.monitor_process` already use: `pid` (None), `returncode`,
`stdout`, `stderr`, `wait()`, `send_signal()`, `terminate()` and `kill()`.
The tracking, `_process_output`, `_monitor_process` and rmux code that follows
is the same. So spec detection, the review pause, the auto-start of the build
and the failure handling all stay in the one code path the in-pod run uses.

`_monitor_process` gets `cmd=None, env=None` for a Job. Those two arguments
turn on the model-fallback restart, which respawns the argv in-pod. A Job run
must never reach that.

A task id without a `project:spec` prefix has no spec dir. With the flag on,
it is refused.

### The Job (new `services/spec_job_backend.py`)

Reuses the build-Job substrate in `build_backend.py`:

- **Workspace.** Make a standalone clone of the project
  (`git clone --local --no-hardlinks`) under
  `<project>/.aifactory/worktrees/specs/<spec_id>`. Copy the control-plane spec
  dir into it with `core.workspace.copy_spec_to_worktree`. Write the task title
  and description to `.aifactory/spec_task.md` in the clone, so the text is not
  in argv or env (both land in the pod spec). Pack it with
  `core.artifact_store.pack_workspace`, then delete the clone. The Job unpacks
  it into its emptyDir `/work`. The Job mounts no data PVC, so the web-server's
  `~/.aifactory` (with `.jwt_secret`) and other projects are out of reach.
  A pack failure raises. The build path falls back to a PVC co-mount on a pack
  failure, but a spec Job has no co-mount, so a spec Job needs object storage
  (`S3_*`).
- **Manifest.** `core.job_dispatch.JobSpec` with `service="aifactory"` and
  `job_id=f"{task_id}-spec"`. The suffix gives the spec Job a different name
  and `factory.io/job-id` label from the task's later build Job, so the two
  never collide while the spec Job waits for its TTL GC. The other fields are
  the same as for a build: image from `_resolve_build_image`, the
  `aifactory-sandbox` service account, the namespace, `nix_develop=False`,
  `workspace_uri`, no data PVC and no API token. The deadline is 2 hours (a
  constant) instead of the build's 6. The result goes through
  `_inject_seed_creds` (the `_SEED_HOME_VOLUMES` CLI logins) and
  `_inject_build_path`.
- **Command.** All values are shell-quoted:
  `python <APP_BACKEND_PATH>/runners/spec_runner.py --task-file
  /work/.aifactory/spec_task.md --project-dir /work --spec-dir
  /work/.aifactory/specs/<spec_id> --no-build --auto-approve [--model M]
  [--complexity C]`. The Job always auto-approves its own copy, so the run
  does not stop at a review prompt. That copy is never fetched back. The
  control plane makes the approval decision (below).
- **Env.** `build_job_env(oauth_token)` (the allowlist that excludes
  `JWT_SECRET`, `API_TOKEN`, `DATABASE_URL`, `ANTHROPIC_API_KEY` and every
  `*SECRET*` name other than the listed `S3_SECRET_KEY`), minus `GITHUB_TOKEN`
  and `GH_TOKEN`. The spec pipeline makes no GitHub calls (`grep` of `spec/` and
  `runners/spec_runner.py`), and this agent reads untrusted issue text, so it
  gets no GitHub token. Added on top: `mcp_spawn_env(project_path)`,
  `AIFACTORY_MARKER_NONCE` (#1929, from `_marker_nonce_env`),
  `AIFACTORY_TASK_ID`, `QUICK_MODE=true` for `complexity == "simple"`, and
  `AIFACTORY_SPEC_FETCH_ID`, a fresh `uuid4().hex` per dispatch that keys the
  fetch-back objects (below).

### Job side (`apps/backend`)

- `runners/spec_runner.py` calls `core.workspace_fetch.maybe_unpack_workspace`
  on `--project-dir` before anything else. This is a no-op without
  `WORKSPACE_URI`, so a local run is unchanged. `cli/main.py` does the same for
  run.py.
- After a successful run, `spec_runner.py` calls
  `workspace_fetch.maybe_push_spec_files(spec_dir)`. With
  `AIFACTORY_SPEC_FETCH_ID` set, it uploads each allowlisted file that exists,
  one object per file, under `ArtifactRef(service="aifactory",
  job_id=<fetch id>, role="doc", path="spec/<name>")`. Without that env it does
  nothing. It never raises, so a push failure shows up control-side as a
  missing `spec.md`.

### Fetch-back (control side, `workspace_fetch.fetch_spec_files`)

The allowlist is `SPEC_FETCH_FILES`: `spec.md`, `implementation_plan.json`,
`context.json`, `project_index.json`, `research.json`,
`complexity_assessment.json`, `critique_report.json`, `graph_hints.json` and
`architecture.md`.
The fetch reads exactly those keys. Names come from the constant, never from
the Job, so no path, link or archive member is ever parsed. Each object is
checked:

- at most 8 MiB, else it is skipped with a warning;
- `.json`: must parse as a JSON object or array;
- `.md`: must decode as UTF-8;
- written with a temp file and `os.replace` into the control-plane spec dir.

**`requirements.json` is not on the list.** The 10-27 spec had it. But
`should_pass_force` and the PFactory governance check
(`is_governed_requirements`) read it to skip plan review. A Job-written copy
could then force-approve the build. The control plane wrote it before dispatch,
and its copy stays. `task_metadata.json`, `task_control.json`,
`review_state.json`, `memory/` and handoff files are never fetched.

After a successful fetch the control plane sets the review state itself, the
same way `review.reviewer.run_review_checkpoint` does:

- `should_auto_approve`: `ReviewState.load(spec_dir).approve(spec_dir,
  approved_by="auto")`;
- otherwise: if the loaded state is not a valid approval of the fetched files,
  call `invalidate(spec_dir)`, which writes `approved: false`. The monitor
  then takes the existing human-review branch.

A Job that succeeded but whose `spec.md` did not come back exits with code 1.
So does a Job that failed or vanished. The monitor's existing failure handling
takes over.

### Logs and reconcile

- **Logs.** `SpecJobProcess` runs `build_log_stream.KubeJobLogStreamer` on the
  Job, with a `log_sink` that feeds each line into `stdout` (an
  `asyncio.StreamReader`). `_process_output` then does what it does for the
  in-pod process: nonce-checked control markers (#1929), the cockpit log and
  the rmux tee. `job_active` is "no terminal outcome yet". After the outcome,
  the streamer gets up to 30 s to drain before `stdout` gets EOF.
- **Reconcile.** A background task polls `KubeJobBuildBackend._job_outcome`
  (the build reaper's own verdict: succeeded, failed, running or gone) every
  10 s. On a terminal outcome it fetches back and sets `returncode`. `wait()`
  awaits that task under `asyncio.shield`, because the monitor calls it under
  `wait_for(timeout=3)`.
- **Stop.** `send_signal`/`terminate`/`kill` set a stop event. The poller then
  deletes the Job (`propagation_policy="Background"`) and sets `returncode` to
  -15. `AgentService._signal_process_tree` returns early when `pid` is None.
  It now calls `proc.send_signal(sig)` in that case instead, so `stop_task`
  reaches the Job.
- **Orphans.** A module-level set holds the names of live spec Jobs.
  `KubeJobBuildBackend.sweep_orphan_jobs` counts them as owned. A spec Job this
  process does not know about is an orphan from before a restart, and the
  #1714 sweep deletes it after its grace period, the same as a build Job
  without a row. Durable cross-restart reconcile is a later slice.

### Docs

Add an `AIFACTORY_SPEC_BACKEND` row to `docs/docs/environment-reference.md`
next to `AIFACTORY_BUILD_BACKEND`: values, default, that it needs `S3_*`
object storage, that the Job gets no GitHub token or control-plane secrets, and
that the gitops flip happens after a soak.

## Alternatives rejected

- **Durable job-state row and the build reconcile loop for the spec Job.** The
  build `_on_kubejob_build_done` hook runs the build endgame (TFactory handoff,
  completion events), which is wrong for a spec. A spec-specific row would
  touch admission, the slot cap and #1704 ownership. The in-process poller
  reuses `_job_outcome` and leaves the store alone. A restart orphans the Job,
  which is what happens to an in-pod spec run today.
- **One archive pushed back and extracted with a filter.** #1984 had to harden
  `maybe_fetch_memory` against a forged archive. Fixed per-file keys leave
  nothing to traverse.
- **Co-mounting the data PVC subPath instead of packing.** That puts the Job
  on the PVC that also holds `~/.aifactory` (only a subPath, but the in-pod
  agent this replaces is the reason for this work). Prod runs the packed path
  anyway.
- **A separate adapter-free code path for Job completion.** It would duplicate
  the 100-line post-spec branch of `monitor_process` (detection, review pause,
  build auto-start, credential release). The process-shaped adapter keeps a
  single copy.
- **Honouring the Job's `review_state.json`.** The Job's agent can write it.
- **Passing the task text in argv or env.** Both are visible in the pod spec.

## Risks

- **Prod: none until the flag is flipped.** The default path is unchanged and
  tested.
- With the flag on and no `S3_*`, spec creation fails with a logged error. It
  does not run in-pod.
- A spec that relies on a file outside the allowlist loses it. The allowlist
  covers every file `apps/backend/spec/` writes into the spec dir except
  `requirements.json`, which is excluded on purpose.
- A restart mid-spec orphans the Job. With sweep dry-run on (the default),
  it is only reported, and it ends at its 2 h deadline and TTL.
- The Job's agent has `S3_*` credentials, the same as build Jobs, and could
  write other objects. Fetch-back validates content and reads only keys under
  this dispatch's fresh id.

## Verification

- Unit tests (`tests/test_spec_job_backend.py`):
  - flag on: `start_spec_creation` dispatches one Job.
    `create_subprocess_exec` is not called. The manifest's container env has no
    `JWT_SECRET`, `APP_JWT_SECRET`, `API_TOKEN`, `APP_API_TOKEN`,
    `DATABASE_URL`, `ANTHROPIC_API_KEY`, `GITHUB_TOKEN` or `GH_TOKEN`, even when
    all of them are set in `os.environ`. It has the marker nonce and the OAuth
    token. The task text is not in the command. `_monitor_process` gets
    `cmd=None`.
  - fetch-back: given objects for every allowlisted name plus
    `task_metadata.json`, `requirements.json`, `review_state.json` and
    `task_control.json`, only the allowlisted names are written. An oversize
    or invalid-JSON file is skipped.
  - review state: auto-approve gives an approved `review_state.json`, and
    no-auto gives `approved: false`.
  - flag off: `create_subprocess_exec` is called with today's argv and the Job
    backend is not touched.
  - an unknown flag value raises `BuildBackendUnavailableError`.
  - stop: `send_signal` deletes the Job and `wait()` returns -15. The sweep
    treats a live spec Job as owned.
- The regression test (flag on, no in-pod spawn) fails on origin/dev.
- `ruff`, `scripts/ratchet_lint.py`, and the existing spec-creation, build
  backend and process-monitor tests.
