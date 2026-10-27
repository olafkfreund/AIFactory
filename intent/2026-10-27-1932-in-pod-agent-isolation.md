---
status: draft
issue: 1932
author: olafkfreund
---

# Intent: no LLM agent runs unisolated in the control-plane pod

AIFactory #1932 (with #1930 write grants, #1931 same-uid reads). Refs olafkfreund/Factory#1960.

## Problem

Agents that run inside the web-server pod are not isolated from the control
plane. That is every subprocess-backend build and, on every backend, spec
creation: `services/agent_spec_creation.py` starts `runners/spec_runner.py` as a
child of the web-server even when builds run as Kubernetes Jobs.

Those agents run as the web-server's uid (65532 in prod), with:

- Write/Edit grants on the main project's `.aifactory/**` (`core/client.py`
  ~740-806), so they can rewrite `task_metadata.json`
  (`requireReviewBeforeCoding`, `reviewTier`), `task_control.json`,
  `review_state.json` and handoff files (#1930);
- read access to `~/.aifactory/.jwt_secret`, which exists in prod and signs
  session JWTs because `APP_JWT_SECRET` is unset (#1931 correction). Reading it
  lets an agent mint a session for any user;
- an unsandboxed Bash on k3d (`AIFACTORY_BASH_SANDBOX` off, bwrap cannot mount
  `/proc`).

Spec-creation agents read untrusted issue text, so prompt injection there
reaches all of the above before any build Job exists. The kubejob default does
not contain this phase.

Prod can also land on the subprocess backend without anyone noticing.
`AIFACTORY_BUILD_BACKEND=kubejob` with `DATABASE_URL` unset logs a warning and
builds in-pod (`agent_kubejob._kubejob_backend_enabled`); an unknown value
falls back to `subprocess` (`build_backend.selected_backend`); unset is
`subprocess`. The `/proc/<pid>/environ` leg in the original issue is withdrawn:
the server is non-dumpable (#1680) and child env is scrubbed.

## Proposed outcome

The owner approved this direction on 2026-10-27. Slices, in order:

1. **C. Fail closed on fallback (this PR).** When `AIFACTORY_BUILD_BACKEND=kubejob`
   is set and kubejob cannot run (no `DATABASE_URL`), builds and spec creation
   are refused with an error that names the cause, never run in-pod. An unknown
   backend value is an error, not a silent subprocess. Unset still means
   subprocess for local dev, with a startup warning when the server runs
   in-cluster on subprocess.
2. **A1. Spec-creation Job substrate.** A `KubeJobSpecBackend` (sibling of
   `KubeJobBuildBackend`) that dispatches `spec_runner.py --no-build` as a Job
   with the build Job's isolation: own pod, `build_job_env` env allowlist (no
   `JWT_SECRET`/`API_TOKEN`/`DATABASE_URL`), emptyDir `/work` from the packed
   workspace, no server home mounted. Behind a flag, default off.
3. **A2. Allowlisted spec fetch-back.** The Job pushes only the spec artifacts
   (`spec.md`, `requirements.json`, `context.json`, `implementation_plan.json`
   and the complexity/assessment files spec_runner writes) to a per-spec object
   key; the control plane fetches and writes only those names, and never takes
   `task_metadata.json`, `task_control.json` or `review_state.json` from the Job.
4. **A3. Logs and status.** Stream the Job's pod logs into the cockpit and rmux
   through `build_log_stream`, with the #1929 per-task marker nonce, and
   reconcile terminal state like build Jobs. Then flip the flag on in
   factory-gitops (a separate, owner-approved change).
5. **B. Refuse what subprocess cannot protect.** On the subprocess backend,
   refuse to start review-gated or tier-gated tasks (`requireReviewBeforeCoding`,
   quick mode, a blocking `reviewTier`) with a message that says kubejob is
   required and why.
6. **D. Separate uid for remaining in-pod agents.** Any agent still started in
   the web-server pod runs under a different uid with no read access to the
   server's home or the main `.aifactory/` beyond its own spec dir, and the
   `core/client.py` grants narrow to match (#1930). Longer term.

Separately (not in this epic's slices): set `APP_JWT_SECRET` from a Secret,
delete `~/.aifactory/.jwt_secret` and rotate (#1931). That is a live-credential
change for the owner.

## Affected users and systems

- AIFactory web-server: `services/build_backend.py`, `services/agent_kubejob.py`,
  `services/agent_spec_creation.py`, `main.py` startup.
- Operators of local dev (unset backend: unchanged) and of any deployment that
  sets kubejob without `DATABASE_URL` (now refused instead of silently in-pod).
- Prod (factory-gitops `apps/aifactory/manifests/manifests.yaml`): sets
  `AIFACTORY_BUILD_BACKEND=kubejob` and `DATABASE_URL` from
  `factory-db-aifactory` on the `aifactory` container, so slice C does not
  change prod behaviour. Checked read-only on 2026-10-27.

## Constraints

- Must not change factory-gitops, the cluster, or any credential in this PR.
- Must not change behaviour when the backend is unset (local dev).
- Must not crash the server at startup on a bad backend value: the UI, login
  and read paths keep working; only agent launches are refused.
- Stop, status and reconcile paths must keep working under a misconfigured
  backend so running work can still be stopped.

## Open questions

None for slice C. A1 needs a decision on whether spec creation and build share
one Job per task later; the spec records the current answer (separate Jobs).
