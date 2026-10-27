---
status: approved
issue: 1932
intent: intent/2026-10-27-1932-in-pod-agent-isolation.md
---

# Spec: no LLM agent runs unisolated in the control-plane pod

Decision recorded: the owner approved (2026-10-27) direction A + B + C + D, in
the slice order of the intent. This spec designs all of it; the plan and this
PR cover slice C only.

## Design

### C. Fail closed on fallback (this PR)

One function decides whether the configured backend can run, in
`apps/web-server/server/services/build_backend.py`:

```python
class BuildBackendUnavailableError(RuntimeError): ...

def backend_config_error(store_enabled: bool) -> str | None
```

It reads `AIFACTORY_BUILD_BACKEND` the same way `selected_backend()` does
(strip, lower; empty is the same as unset, so the default) and returns:

- `None` for unset/`subprocess`, and for `kubejob` with the store enabled;
- a message for an unknown value (names the value and the two valid ones);
- a message for `kubejob` without `DATABASE_URL` (names the variable).

Two refusal points, both raising `BuildBackendUnavailableError` with that
message:

1. `AgentKubejobMixin._start_build_unit` (`services/agent_kubejob.py` ~491),
   the single backend-selection point that `start_task_execution`, the
   in-memory drain and the durable drain all go through. Refusing here covers
   every build launch. Existing callers already treat a raise as a spawn
   failure: the durable admit marks the row failed and re-raises, the drains
   log and drop the task, so no slot leaks.
2. `start_spec_creation` (`services/agent_spec_creation.py`), before the
   subprocess is spawned. Today, kubejob-without-store makes
   `_kubejob_backend_enabled()` False, so `build_chain_args` omits
   `--no-build` and spec_runner chains the whole build in-pod via `os.execv`.
   That is the fourth fallback, and it bypasses `_start_build_unit`, so spec
   creation must refuse too.

The read-side paths (`_kubejob_backend_enabled`, stop, reconcile, reap,
`trusted_contract`) are unchanged: they keep returning "not kubejob" under a
bad config, so a misconfigured server can still stop and report work.
`selected_backend()` keeps returning `subprocess` for an unknown value for those
callers; its log line stops saying "falling back" and says builds are refused.
`_kubejob_backend_enabled`'s warning text changes the same way.

Startup report, `log_backend_config(logger, store_enabled)` in
`build_backend.py`, called from the `main.py` lifespan next to the reconcile
loop start (~235):

- config error → `logger.error("Agent launches will be refused: <message>")`;
  the server still starts (UI, login and read paths keep working);
- subprocess backend and `KUBERNETES_SERVICE_HOST` set (any pod) →
  `logger.warning` that in-cluster agents run in the control-plane pod
  unisolated and to set `kubejob` + `DATABASE_URL` (#1932);
- otherwise nothing new.

Callers' HTTP mapping is unchanged: routes wrap the start in their existing
`except Exception` → `client_error(...)`, so the client gets "Failed to start
... (reference …)" and the cause is in the log under that reference. A
dedicated status code would touch nine call sites; not needed for an operator
misconfiguration.

Docs: `docs/docs/environment-reference.md` row for `AIFACTORY_BUILD_BACKEND`
says unknown values are refused and kubejob needs `DATABASE_URL`.

### A. Spec creation as a Kubernetes Job

Grounded in the build Job path:

- **Inputs.** The control plane already populates the spec dir
  (`task_metadata.json`, `requirements.json`) and, for builds, packs the
  worktree to object storage (`build_backend._maybe_pack_workspace` →
  `WORKSPACE_URI`; Job side `core/workspace_fetch.maybe_unpack_workspace`
  into an emptyDir `/work`). A spec Job reuses that: pack the project checkout
  plus the spec dir, pass the task title/description as a file in the archive
  (not argv or env, which land in the pod spec), and run
  `spec_runner.py --no-build --project-dir /work`. Env comes from
  `build_job_env` (the allowlist that already excludes `JWT_SECRET`,
  `API_TOKEN`, `DATABASE_URL`); home is the container's own filesystem plus the
  `_SEED_HOME_VOLUMES` emptyDirs. The web-server's `~/.aifactory` is never
  mounted.
- **Dispatch and ownership.** A `KubeJobSpecBackend` beside
  `KubeJobBuildBackend`, using the same `job_dispatch` manifest builder, durable
  `worker_ref`, reaper and one-owner rules (#1704). A separate Job from the
  build: spec and build have different approval gates between them (plan
  review), and one Job per phase keeps the gate on the control plane.
- **Logs.** `build_log_stream` follows the Job's pod logs into the cockpit and
  rmux, as for builds. Control markers in that stream are trusted only with the
  per-task `AIFACTORY_MARKER_NONCE` (#1929), which the spec Job gets the same
  way `KubeJobBuildBackend.dispatch` sets it.
- **Fetch-back.** Mirrors `maybe_push_plan` / `maybe_fetch_plan`: the Job
  pushes a per-spec archive; the control plane extracts only an allowlist
  (`spec.md`, `requirements.json`, `context.json`, `implementation_plan.json`,
  `project_index.json`, `research.json`, `complexity_assessment.json`,
  `critique_report.json`, `graph_hints.json`), each a regular file under a size
  cap, no links, no paths. `task_metadata.json` (spec_runner writes it once),
  `task_control.json`, `review_state.json` and handoff files are never taken
  from the Job; the control plane's copies are authoritative, same rule as the
  sync allowlist (#1921).
- **Rollout.** Behind a flag, default off; flipped in factory-gitops in a
  separate owner-approved change after a live run.

### B. Refuse gated tasks on subprocess

In `start_task_execution` and `start_spec_creation`: on the subprocess backend,
a task whose control-plane metadata has `requireReviewBeforeCoding`, quick mode
or a blocking `reviewTier` is refused with an `InputRejectedError` whose
message says kubejob is required because in-pod agents can rewrite the gate
state. Read from the control plane's copy before any agent runs.

### D. Separate uid for in-pod agents

Whatever still starts an agent in the web-server pod runs it as a second uid
(a setuid-free launcher via a pre-created user and `runAsUser` on a sidecar, or
a dedicated pod), with no read on the server's home, `/proc/<server>`, or the
main `.aifactory/` beyond its own spec dir; `core/client.py` grants narrow to
the agent's own spec/worktree (#1930). Designed in detail when A and B have
shrunk what remains in-pod.

## Alternatives rejected

- **Crash at startup on a bad backend (C).** Fails closed harder, but takes the
  UI and login down for an agent-launch misconfiguration, and a crash-looping
  pod hides the reason in restarts. Refusing launches with a logged cause keeps
  the blast radius to what is actually unsafe.
- **Make unset mean kubejob (C).** Breaks every local dev setup with no
  cluster. Rejected by the brief; the in-cluster warning covers the risk.
- **Treat kubejob-without-store as kubejob with no reconcile (C).** Strands
  builds with nothing reaping them; the original reason for the fallback.
- **Make `_kubejob_backend_enabled` raise (C).** It is also used by stop,
  reconcile and reap; raising there stops a misconfigured server from stopping
  work.
- **HMAC'd approvals instead of isolation (A/D).** The key would be readable by
  the same in-pod agent (#1926 discussion).
- **Spec and build in one Job (A).** Moves the plan-review gate into the pod
  that the gate guards against.
- **Tool-grant narrowing alone (D).** Bash is unsandboxed on k3d, so grants do
  not bind; needs a uid boundary.

## Risks

- **Slice C, prod:** none expected. factory-gitops sets
  `AIFACTORY_BUILD_BACKEND=kubejob` and `DATABASE_URL` on the `aifactory`
  container (`apps/aifactory/manifests/manifests.yaml` lines 339 and 623,
  read 2026-10-27). If a future manifest drops `DATABASE_URL`, launches stop
  with a logged reason instead of running in-pod; that is the intended change.
- **Slice C, dev clusters** that set kubejob without a database: builds and
  spec creation now fail. The error says which variable to set.
- **Slice C, typo'd backend** (`kubejobs`): previously ran in-pod with a
  warning, now refused.
- **A:** spec latency rises by Job start time; the packed archive must not carry
  the server's secrets (it is the project checkout plus the spec dir only).
- **B:** local-dev users of review-gated tasks on subprocess lose that path;
  they need kubejob or an ungated task.

## Verification

Slice C:

- Unit tests in `tests/test_build_backend_kubejob.py`:
  `backend_config_error` for unset, empty, `subprocess`, `kubejob`+store,
  `kubejob` without store, unknown; `log_backend_config` error and in-cluster
  warning via `caplog`.
- `tests/test_agent_service_kubejob_backend.py`: with kubejob and no store,
  `start_task_execution` raises `BuildBackendUnavailableError` and neither
  `_spawn_task_execution` nor `_dispatch_build_job` is called; with an unknown
  value, same. The old `test_kubejob_falls_back_to_subprocess_without_store`
  keeps asserting `_kubejob_backend_enabled() is False` (read side unchanged).
- Spec creation: with kubejob and no store, `start_spec_creation` raises
  before any subprocess is created.
- Each new test fails on `origin/dev`.
- ruff, `scripts/ratchet_lint.py`, the touched pytest files, then CI green.

Slices A/B/D carry their own verification in their own plans.
