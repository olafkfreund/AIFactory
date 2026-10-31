---
status: approved
issue: 1932
author: olafkfreund
---

# Intent: spec creation as a Kubernetes Job, behind a flag (#1932 slices A1-A3)

Refs #1932. Parent design: `spec/2026-10-27-1932-in-pod-agent-isolation.md`
(section "A. Spec creation as a Kubernetes Job").

## Problem

Spec creation still runs inside the web-server pod on every backend. Re-checked
on origin/dev (0d8161ed): `services/agent_spec_creation.py`
`start_spec_creation` builds a `runners/spec_runner.py` argv and starts it with
`asyncio.create_subprocess_exec` in the web-server pod. With the kubejob build
backend it only adds `--no-build`.

That agent reads untrusted issue text and runs as the server uid. It can read
`~/.aifactory/.jwt_secret` (live in prod, per the issue) and write the main
project's `.aifactory/` gate files (`task_metadata.json`, `task_control.json`,
`review_state.json`) before any build Job exists. Slice C (#1942), slice B
(#2009) and #1985 narrowed the build side. Spec creation is the largest LLM
session left in the control-plane pod.

Owner decision on #1932 (2026-10-31): option B. Every LLM agent session in prod
runs as a Kubernetes Job, and the in-pod subprocess backend becomes local-dev
only.

## Proposed outcome

- With `AIFACTORY_SPEC_BACKEND=kubejob`, spec creation runs as a Kubernetes
  Job. It reuses the build-Job substrate: the job_dispatch manifest, the packed
  workspace, the `_SEED_HOME_VOLUMES` CLI logins, the build env allowlist and
  the #1929 marker nonce. No control-plane secret is in the Job env, and the
  web-server's home and data PVC are not mounted.
- Only an allowlisted set of spec files comes back. Each is validated on the
  control plane. The gate files stay control-plane owned.
- The cockpit and rmux show the Job's logs, and completion goes through the
  same post-spec handling as the in-pod path (review pause or build start).
  Stop deletes the Job. Orphaned spec Jobs are swept like orphaned build Jobs.
- With the flag unset (the default), behaviour is unchanged.
- The flag is documented. It is not flipped in gitops here.

## Affected users and systems

- `apps/web-server/server/services/agent_spec_creation.py`, a new
  `services/spec_job_backend.py`, small touches to `build_backend.py`
  (orphan sweep) and `agent_service.py` (stop signal).
- `apps/backend/runners/spec_runner.py` and `core/workspace_fetch.py` (Job side:
  unpack the workspace, push the spec files).
- Docs: `docs/docs/environment-reference.md`.
- Prod is unaffected until the lead flips the flag in factory-gitops.

## Constraints

- Default off. With the flag unset, the code path is the current one.
- Must not put `JWT_SECRET`, `API_TOKEN`, `DATABASE_URL` or any other
  control-plane secret into the Job.
- Must not take `task_metadata.json`, `task_control.json`, `review_state.json`,
  `requirements.json` or handoff files from the Job.
- No gitops or cluster change, no deploy, no credential rotation.
- An unknown flag value must refuse spec creation, not fall back to in-pod
  (the same rule as #1942).

## Remaining slices after this one (from the #1932 decision)

1. A4: PR review and clarification sessions as Jobs on the same substrate.
2. Finish #555 (deprecate the wildcard token), which closes #1931.
3. Gitops flag flip for `AIFACTORY_SPEC_BACKEND=kubejob` after a soak (lead).
4. Durable reconcile of an in-flight spec Job across a control-plane restart
   (today such a Job is orphaned and swept, and the task is recovered like an
   in-pod spec run killed by a restart).

## Open questions

None. The owner chose option B on 2026-10-31.
