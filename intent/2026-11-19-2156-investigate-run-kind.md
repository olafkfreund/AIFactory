---
status: draft
issue: 2156
author: olafkfreund
---

# Intent: a read-only `investigate` run kind (#2156)

## Problem

RFactory's meeting bot (RFactory#18, approved intent, spec and plan on RFactory branch `docs/18-claude-terminals`) needs a Claude session that **investigates** a repository and **runs its tests** after a person approves it in a call. It must write nothing and publish nothing, and its log is shown in the call while it runs. The owner decided this runs as an AIFactory run, started through CFactory.

AIFactory cannot do that safely today; every run is a build run:

- **Write access.** Agents run with `permission_mode="bypassPermissions"` (`apps/backend/core/client.py:1137`), and every tool, write and shell included, is available. There is no read-only mode; planner-only (`--stop-after-planning`) is not reachable from create-and-run and still writes spec files.
- **Credentials.** A run gets `GITHUB_TOKEN`/`GH_TOKEN` when present (`apps/web-server/server/services/build_backend.py:252`). It can also get provider and storage keys, the project's MCP environment, seeded credential files (`build_backend.py:742`) and a service-account token.
- **Egress.** The task NetworkPolicy allows HTTPS to any public address and reaches internal services (`charts/aifactory/templates/networkpolicy-tasks.yaml`). Repository code run during a task could upload the source.
- **Limits and stop.** `budgetUsd` only alerts and never aborts. A run outlives a caller that disappears, until the 6-hour deadline.
- **Logs.** They are read from worktree files; there is no cursor API for following a live run.

## Proposed outcome

A run kind `investigate`, off by default behind a switch and kubejob only, where:

1. **The source is immutable.** The control plane clones one pinned commit with a read-only credential, strips the Git remote and credentials, removes secret-bearing files, and mounts the result read-only. The agent can read, search and glob it, and nothing else touches the filesystem.
2. **Tests run in the harness, not the agent.** One tool, `run_tests`, runs the project's configured test command on a fresh copy of the pinned commit each time, and returns the exit code, parsed counts (or "not parsed") and the tail of the output. The agent has no shell, write, web or MCP tool, and no AIFactory create, start or merge tool.
3. **The environment is an allowlist:**
   - the owner's Claude credential, the run's id and a run-scoped log token, and nothing else;
   - no GitHub token, provider or storage key, project MCP environment or seeded credential files;
   - no service-account token.
4. **Egress is investigation-only:**
   - **allowed:** DNS, the Claude API, AIFactory's own API (only this run's log and report writes, with its token), and a CONNECT-only package-registry proxy to named hosts;
   - **refused:** everything else, including Postgres and other internal services.
5. **Limits abort:**
   - a 30-minute deadline;
   - a token budget and a turn cap that end the run when reached;
   - the test command's own time limit;
   - one `investigate` run per project at a time.
6. **A lease stops it when the caller is gone.**
   - The caller (CFactory, for RFactory) renews a lease every 10 s, and an in-pod watchdog ends the run when the lease is 60 s old.
   - The pod has a short grace period and is deleted with foreground propagation.
   - "Stopped" is reported only once the pod is gone.
7. **Live log and report:**
   - **The log:** written to the task log store through the API while the run is live, and readable with a cursor (`after=`, at most 500 lines), per run.
   - **The report:** findings, the files and lines relied on, every `run_tests` result and the tested commit. It is stored on the task.
   - **Scrubbing:** both are scrubbed of secrets before they are stored.

## Affected users and systems

- AIFactory web-server (dispatch, the new routes), the agent runtime (tools, harness, watchdog), the kubejob backend and the Helm chart (a new NetworkPolicy, the registry proxy, the switch).
- CFactory (CFactory#561) is the only intended caller; RFactory reaches it only through CFactory.
- Build runs are unchanged.

## Constraints

- **Build runs untouched.** Their tools, credentials, egress and behaviour stay exactly as they are; the new kind is additive and off by default.
- **Enforced, not described.** Read-only, the credential set and the egress are properties of the pod and its policy, tested on the built pod (env, mounts, Git configuration, a refused upload, a refused write), not instructions to the agent.
- **No in-pod agent.** The run is a Job (`AIFACTORY_FORBID_INPOD_AGENTS` stays honoured).
- **Secrets.** No secret file is readable in `/src`; log and report are scrubbed. The scrub is a list and patterns, not a proof; the caller redacts again.
- **Releases.** The release lead's cadence and the existing gates apply; nothing here is deployed outside a release.

## Open questions

1. **Registry proxy:** reuse an existing egress proxy in the cluster, or a small new one in the chart?
2. **Test command source:** a project setting (`test_command`) in AIFactory, or passed by CFactory and checked against CFactory's project configuration (RFactory#18's plan has CFactory refuse a changed command)?
3. **Default caps:** 2 M tokens and 200 turns are RFactory's defaults; should AIFactory enforce caps given per run, or fixed ones per kind?
4. **Secret-file list:** which patterns beyond `.env*`, `*.pem`, `*.key`, `id_*`, `.npmrc`/`.pypirc`/`.netrc` with tokens and `credentials*`?
