---
status: draft
issue: 2156
author: olafkfreund
---

# Intent: a read-only `investigate` run kind (#2156)

## Problem

RFactory's meeting bot (RFactory#18, approved intent, spec and plan on RFactory branch `docs/18-claude-terminals`) needs a Claude session that **investigates** a repository and **runs its tests** after a person approves it in a call. It must write nothing and publish nothing, and its log is shown in the call while it runs. The owner decided this runs as an AIFactory run, started through CFactory.

AIFactory cannot do that safely today. Today's Jobs are build runs, plus review and clarification Jobs, and none is read-only:

- **Write access.** Agents run with `permission_mode="bypassPermissions"` (`apps/backend/core/client.py:1137`), and every tool, write and shell included, is available. Planner-only (`--stop-after-planning`) is not reachable from create-and-run and still writes spec files.
- **Credentials.** A run gets a GitHub token when present (`build_backend.py:513-522`), and can get provider and storage keys, the project's MCP environment (`build_backend.py:1561-1566`), seeded credential files and a service-account token.
- **Egress.** The task NetworkPolicy selects every `factory.io/kind: task` pod (`charts/aifactory/templates/networkpolicy-tasks.yaml:14-16`), and the dispatcher requires that label (`apps/backend/core/job_dispatch.py:474-478`). It allows HTTPS to any public address and reaches internal services. NetworkPolicies only add permissions, so a second, stricter policy on such a pod would change nothing.
- **Limits and stop.**
  - `budgetUsd` only alerts.
  - The build token ceiling (`apps/backend/agents/act_loop_hooks.py:59-98`) is charged after a session, not as an immediate abort.
  - A run outlives a caller that disappears, until the 6-hour deadline.
  - Stop frees the project slot even when the Job deletion failed (`agent_kubejob.py:1235-1256`).
  - The deadline branch can mark a row failed without deleting its Job (`build_backend.py:1884-1897`).
- **Logs.** REST logs already read kubejob pod tails, with a bounded `tail` (`routes/tasks_logs.py:34-64`), and Job output streams into the cockpit (`agent_kubejob.py:619-688`). What is missing: durable per-run cursor reads, authorisation scoped to one run, and scrubbing before storage.

## Proposed outcome

A run kind `investigate`, off by default behind a switch and kubejob only. It is built from three pods' worth of separation:
- the **agent pod** reads and reasons;
- a **test pod** runs repository code;
- the **control plane** checks out, collects and reports.

1. **A sanitized source export, no Git metadata.**
   - The control plane clones one pinned commit with a read-only credential, then exports its files **without `.git`**, removing configured secret paths.
   - Symlinks may not point outside the export.
   - It mounts the export read-only.
   - The commit identity is kept by the control plane, outside the export.
   - The agent can read, search and glob the export, and nothing else.
2. **Tests run in their own pod, with nothing to steal.**
   - `run_tests` takes no arguments. The control plane starts a **separate test pod** on a fresh copy of the export, running the run's fixed test command.
   - The test pod has **no credential**, no service-account token, no access to the Claude API, AIFactory's API or the agent pod, and egress only to the package-registry proxy.
   - The control plane collects its exit code, parsed counts (or "not parsed") and output tail, and returns them to the agent.
   - Repository code therefore never runs beside the Claude credential or the harness.
   - With no configured command, `run_tests` returns "no tests run".
3. **The agent pod's environment is an allowlist:**
   - the owner's Claude credential, the run's id, and a run-scoped token that may write only this run's log and report;
   - no GitHub token, provider or storage key, project MCP environment or seeded credential files;
   - no service-account token, and no role that creates Jobs.

   The run's token may **not** renew its own lease.
4. **Egress by mutually exclusive selection.**
   - Investigation pods carry their own `factory.io/kind` value (agent, test), so the build policy never selects them, and the build policy and its permissions stay exactly as they are.
   - The **agent pod** may reach DNS, the Claude API, and AIFactory's API for its own log and report.
   - The **test pod** may reach DNS and the CONNECT-only package-registry proxy to named hosts.
   - Everything else is refused.
   - Dispatch is **refused** when the investigation policies are not present.
   - The promise is "only to these destinations", not "no upload". Allowed destinations still receive data; AIFactory's API accepts only this run's log and report.
5. **Limits that abort:**
   - a 30-minute deadline;
   - a token budget and a turn cap checked **during** the session, ending it immediately;
   - the test command's own time limit;
   - one `investigate` run per project at a time.
6. **A lease the caller owns.**
   - The caller (RFactory, through CFactory, which only **relays** its renewals) renews every 10 s, and a watchdog ends the run when the lease is 60 s old.
   - The watchdog also covers a dispatch that was never renewed, a lease read that fails, and a lost start response.
   - The pods get a short grace period and foreground deletion.
7. **Its own lifecycle.** An investigation never enters the build completion path: no build-evidence gate, no publishing, no verification handoff.
   - **Its terminal states** are completed, stopped, failed, expired and uncertain.
   - **The project slot** stays held while termination is uncertain, through a restart, and is released only once the pods are confirmed gone.
   - **The deadline** deletes the Jobs before it marks the run.
8. **Fixed inputs:**
   - the repository, the commit SHA, the request text, the test command (or none) and the limits are fixed at dispatch and never change during the run;
   - the test command never comes from speech, the request, or the repository's own files.
9. **Live log and report:**
   - **Log:** written to a per-run store while the run is live and readable with a cursor (`after=`, at most 500 lines), authorised for this run only.
   - **Report:** findings, the files and lines relied on, every `run_tests` result and the tested commit. It is stored on the task.
   - **Scrubbing:** both are scrubbed of secrets **before** storage.

## Affected users and systems

- AIFactory web-server (dispatch, the new routes), the agent runtime (tools, harness, watchdog), the kubejob backend and the Helm chart (a new NetworkPolicy, the registry proxy, the switch).
- CFactory (CFactory#561) is the only intended caller; RFactory reaches it only through CFactory.
- Build runs are unchanged.

## Constraints

- **Build runs untouched.** Their tools, credentials, egress and behaviour stay exactly as they are; the new kind is additive and off by default.
- **Enforced, not described.** Read-only, the credential set and the egress are properties of the pods and their policies, not instructions to the agent. They are tested on the built pods: env, mounts, no `.git`, a symlink escape refused, a refused upload, a refused write. A **deliberately hostile test** must find no credential, reach neither the Claude API nor AIFactory's API, and be unable to alter the report.
- **The control plane keeps its own Kubernetes permissions;** only investigation workloads lose theirs.
- **No in-pod agent.** The run is a Job (`AIFACTORY_FORBID_INPOD_AGENTS` stays honoured).
- **Secrets.** Configured secret paths are excluded from the export, and Git history is never exposed. Secrets elsewhere in the source are caught only by pattern scrubbing of the log and report, which is a list and patterns, not a proof; the caller redacts again.
- **Releases.** The release lead's cadence and the existing gates apply; nothing here is deployed outside a release.

## Decisions on the open questions

The owner said on 2026-11-19 to use the recommendations ("use your recommendations for this, use what makes most sense"):

1. **Registry proxy: a small new one in AIFactory's chart.** No general egress proxy exists in the cluster. The only CONNECT proxy is RFactory's desktop proxy (`rfactory-desktops/rfactory-proxy`, from RFactory `desktop/proxy/proxy.js`), which is CONNECT-only with a host allowlist and has already been reviewed. AIFactory runs its **own instance** of that pattern in its namespace, with a registry allowlist (npm, PyPI, crates.io, Go proxy, Maven Central). It does not share RFactory's instance, because the two are separate trust domains.
2. **Test command: CFactory supplies it, AIFactory fixes it.** CFactory's project configuration is the source; it sends the command in the dispatch and refuses a changed one (CFactory#561). AIFactory stores the command on the run at dispatch and never changes it for that run. It never reads the command from the repository or the request. Absent → "no tests run".
3. **Caps: given per run, bounded per kind.** The caller sends its limits. AIFactory refuses any limit above the kind's maximum (30 minutes, 2 M tokens, 200 turns, 10 `run_tests` calls) and applies the kind's maximum when none is given.
4. **Secret paths excluded from the export:**
   - `.env*` and `.envrc`;
   - `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks` and `*.keystore`;
   - `id_*` and `.ssh/`;
   - `.npmrc`, `.pypirc`, `.netrc` and `.git-credentials`;
   - `credentials*` and `secrets.*`;
   - `.aws/`, `.kube/`, `kubeconfig*` and `.docker/config.json`;
   - `*.tfstate*`, `*.tfvars` and `.terraform/`.

   The list is configurable per project, and it can only be added to.
5. **Test pod: a fresh one for each `run_tests` call.** Each call gets a clean start and no state carries over; this is the simplest isolation. It costs a pod start per call, which the 10-call cap bounds.
