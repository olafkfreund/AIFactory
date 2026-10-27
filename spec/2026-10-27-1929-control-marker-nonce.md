---
status: draft
issue: 1929
intent: intent/2026-10-27-1929-control-marker-nonce.md
---

# Spec: authenticate build stdout control markers

## Design

**Per-run nonce on the existing stdout channel.**

1. **Nonce.** At spawn the control plane generates `secrets.token_hex(16)` per
   task, keeps it in `AgentEmitMixin._task_marker_nonces[task_id]`, and passes
   it to the build as env `AIFACTORY_MARKER_NONCE`:
   - subprocess backend: set on the spawn `env` in
     `agent_service.py` (build) and `agent_spec_creation.py` (spec creation,
     which `execv`s run.py). Respawns (fallback model, credential failover in
     `agent_credential.py` / `process_monitor.py`) reuse that `env` dict and the
     same `task_id`, so they inherit the nonce with no extra code.
   - kubejob backend: `agent_kubejob.py` passes `marker_nonce=` to
     `KubeJobBuildBackend.dispatch` (`build_backend.py`), which adds it to the
     Job container `extra_env`. The log stream that reads the pod logs runs in
     the same control-plane process and uses the same map.
2. **The agent never sees it.** `AIFACTORY_MARKER_NONCE` goes into
   `_AGENT_ENV_DENY_EXACT` in `apps/backend/core/auth.py`, so every
   `child_env()` (agent tools, providers, gate runner running model-written
   tests, git) drops it, and `get_agent_env_blanks()` blanks it for the
   Claude SDK child. Control-plane spawn sites add it after
   `make_subprocess_env()`, so the deny entry does not strip it there.
3. **Wire format** (`apps/backend/core/phase_event.py`):
   `__EXEC_PHASE__:<nonce>:{json}`, `__USAGE__:<nonce>:{json}`, and a new
   `__RATE_LIMIT__:<nonce>:`. With no nonce in env (CLI use) the build prints
   an empty nonce; the control plane never accepts an empty one.
4. **Parser** (`agent_emit.py`): a marker counts only if the line starts with
   `<prefix><known non-empty nonce>:`. The unauthenticated formats
   `[PHASE_EVENT] k=v` and bare `{"phase"|"status":…}` JSON lines are removed
   (no in-tree emitter exists; grep verified). Anything else is plain log text
   (still logged, still written to `task_logs.json` as text).
5. **Rate limit.** `_is_rate_limit_line` free-text matching is removed. The
   build emits `__RATE_LIMIT__` from the shared SDK parse hook
   (`core/client.py` `_patched_parse`) when a parsed `AssistantMessage` has
   `error == "rate_limit"` — the SDK's field for the CLI's "You've hit your
   limit" message. The hook runs for every SDK consumer (session, qa, spec,
   ideation, roadmap). The control plane sets `_task_rate_limits[task_id]`
   only on an authenticated `__RATE_LIMIT__` marker.
6. **Reader robustness.** `subtask_index`/`subtask_total` use a tolerant int
   conversion (bad value → None). The `old_phase` crash lived in the removed
   bare-JSON block. `_process_output` also catches any exception from one
   line's handling, logs it and keeps draining, so no line can stop the
   reader (kubejob `_fan_out` already isolates its sink).

## Alternatives rejected

- **Separate channel (fd 3 or a control-plane-owned file).** Kubejob has only
  the pod log stream; a second channel needs a new transport through the Job
  (sidecar file sync or a second container stream). The subprocess path would
  need `pass_fds` plumbing through the optional bwrap sandbox and the pty
  spawn. More code on both backends, and no stronger against a same-uid
  process (it can open `/proc/<pid>/fd/3` just as it can read
  `/proc/<pid>/environ`).
- **Escape/prefix every model-text and tool-output print.** Seven-plus print
  sites today (session, qa fixer/reviewer, spec agent runner, ideation,
  roadmap) and every future one; one miss reopens the hole. Does not close
  the class.
- **HMAC per marker line.** The nonce is already secret from the model; an
  HMAC adds code and no protection the nonce lacks here.
- **Keep `[PHASE_EVENT]`/bare JSON behind the nonce.** No emitter uses them.

## Risks

- **Residual, not a regression:** a tool process running as the same uid as
  run.py could read `/proc/<run.py pid>/environ` unless the bwrap sandbox
  (`AIFACTORY_AGENT_SANDBOX`, `--unshare-pid`) is on; a kubejob pod with an
  automounted service-account token that can read Jobs could read its own
  spec. Today the barrier is zero.
- **Rolling deploy:** a build started by an old control plane prints old-format
  markers that a new control plane drops (and vice versa), so in-flight builds
  lose live phase/cost updates until they finish. Completion, QA and the PR
  endgame are not marker-driven (issue's table).
- **Rate-limit failover depends on the SDK `error` field.** If a CLI version
  does not set it, failover does not fire and the build fails as an ordinary
  failure. Not verified against a live usage-limit event.
- Hosts: AIFactory control plane on the k3d cluster and dev; no gitops change.

## Verification

New `tests/test_marker_nonce.py` plus updates to `tests/test_phase_event.py`,
`tests/test_live_usage_marker.py`, `tests/test_kubejob_execution_progress.py`:

- forged phase/usage/rate-limit marker without nonce, with a wrong nonce, and
  `[PHASE_EVENT]` / bare JSON lines change nothing;
- a real nonce marker moves the phase and emits usage on the subprocess handler
  and through the kubejob sink;
- `{"status":"x"}`, `subtask_index=abc`-style payloads and a handler that
  raises do not stop `_process_output`;
- model text containing "you've hit your limit" does not set the rate-limit
  flag; an authenticated `__RATE_LIMIT__` marker does; the SDK parse hook
  emits it for `AssistantMessage(error="rate_limit")`;
- `child_env(agent=True)`, `child_env()` and `get_agent_env_blanks()` never
  pass the nonce through; kubejob dispatch puts it in the Job env.

Plus ruff, `scripts/ratchet_lint.py`, and the targeted pytest files.
