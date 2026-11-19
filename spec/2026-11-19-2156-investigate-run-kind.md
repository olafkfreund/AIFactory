---
status: draft
issue: 2156
intent: intent/2026-11-19-2156-investigate-run-kind.md
---

# Spec: a read-only `investigate` run kind (#2156)

Paths: **WS** = `apps/web-server/server/`, **BE** = `apps/backend/`.

## Design

### Three facts from the code that shape it

1. **The validator.** The dispatch validator requires pod label `factory.io/kind == "task"` (`BE/core/job_dispatch.py:474-479`). The file is vendored byte-identical across four services behind a drift gate. A new `kind` value would therefore change all four.
   - Investigation pods **keep** `factory.io/kind: task` and add `factory.io/run-kind: investigate-agent | investigate-test`.
   - The build policy gets one `matchExpressions` term that excludes them (see "Egress").
   - The validator stays unchanged.
2. **No per-project slot.** `job_states.admit` is a global cap (`WS/services/job_state_store.py:207-286`), and `job_states.kind` is always `"build"` (`:35`). Investigations get **their own table**, with a database-enforced per-project slot. Builds keep `job_states`.
3. **The build completion and stop paths don't fit.**
   - **Completion:** the build path assumes build evidence and handoffs (`WS/services/agent_kubejob.py:323-380`).
   - **Stop:** it frees the slot even when the delete fails (`:1235-1252`).
   - **Deadline:** its branch is inert, because the loop passes no `deadline_seconds` (`:971`, `build_backend.py:1884-1897`).

   Investigations get their own backend module with its own lifecycle and reconcile loop. None of the build code above changes.

### Switch

- **The variable:** `AIFACTORY_INVESTIGATIONS`, strict `off`|`on` like `_kubejob_flag` (`WS/services/agent_job_backend.py:41-49`); other values raise.
- **Prerequisites when on:**
  - the kubejob build backend (`kubejob_enabled`, `build_backend.py:644-646`);
  - `DATABASE_URL`;
  - the chart value `investigations.enabled` (which renders the policies and the proxy).
- **When off:** every investigation route returns 404 and nothing else changes.

### Data: `investigations` and `investigation_logs` (one Alembic migration)

`investigations` columns:
- **Identity:** `id` (uuid), `project_id`, `repo`, `sha` (40 hex), `request` (≤ 2000 chars), `test_command` (text, nullable), `limits` (json: minutes, tokens, turns, test_calls).
- **State:** `state`, `reason`, `lease_expires_at`, `pods_gone` (bool).
- **Pods:** `agent_job`, `test_jobs` (json list).
- **Usage:** `run_tests_used`, `tokens_used`, `turns_used`.
- **Results:** `report` (json, nullable), `tests` (json list of results).
- **Credentials:** `token_hash` (sha256 of the run token).
- **Timestamps:** `created_at`, `ended_at`.

`state` takes one of:
- `queued` → `running` → `completing` (live);
- `stopping` (live);
- `completed`, `stopped`, `failed`, `expired`, `uncertain` (terminal).

The **per-project slot** is a partial unique index on `project_id` where `pods_gone = false`. A second dispatch for a project therefore fails at insert until the earlier run's pods are confirmed gone, whatever its state, `uncertain` included.

`investigation_logs` holds `(run_id, seq bigserial, at, line ≤ 2000 chars)` and is indexed on `(run_id, seq)`. Lines are scrubbed **before** insert. Rows are deleted 7 days after `ended_at`, by the reconcile loop.

### Module: `WS/services/investigation_backend.py`

Its public functions are `dispatch`, `renew_lease`, `stop`, `get`, `logs`, `reconcile`, plus the pod-side `append_log`, `put_report`, `run_tests`, `lease_status`.

#### Dispatch

**Refusals.** Dispatch is refused with a reason when:
- the switch is off;
- the two NetworkPolicies `<fullname>-investigate-agent` and `<fullname>-investigate-test` are not present: the control plane `get`s both by name, and the chart adds `networkpolicies: get` to the sandbox Role `rbac/job-sandbox-rbac.yaml`;
- the project already has an investigation whose pods are not confirmed gone: the insert conflicts on the slot index;
- a limit is above the kind's maximum: 30 minutes, 2 000 000 tokens, 200 turns, 10 test calls. Missing limits take the maximum;
- the `sha` is not a commit in the project's repository after a fetch with the control plane's existing credential.

**Steps:**
1. **Insert the row** as `queued`, with `lease_expires_at = now + 60 s`, generating a 32-byte run token and storing its hash.
2. **The source export:**
   - **Archive:** `git -C <project repo> archive --format=tar <sha>` into `/data/investigations/<id>/src`. `git archive` contains **no `.git`** by construction.
   - **Secret paths:** remove every path matching the approved list (intent, decision 4), plus the project's additions (`project_settings` key `investigate_secret_paths`, add-only).
   - **Symlinks:** remove every symlink whose resolved target is outside the export or absolute.
   - **Completion marker:** write `.aifactory-export-ok`. A failure anywhere here marks the run `failed (export)`, with `pods_gone = true` because nothing has started yet.
3. **A per-run Secret** `aif-inv-<id8>` holds `CLAUDE_CODE_OAUTH_TOKEN` (from `_resolve_claude_token_pooled`, `WS/services/agent_credential.py:181`) and `AIFACTORY_RUN_TOKEN`. It is deleted when the run ends.
4. **The agent Job** `aif-inv-<id8>-agent`, built with `build_job_manifest` (`job_dispatch.py`), so the validator applies:
   - **Labels:** `factory.io/kind: task`, `factory.io/run-kind: investigate-agent`.
   - **Account:** `automountServiceAccountToken: false` and no `serviceAccountName` (the namespace default, which has no token).
   - **Timing:** `activeDeadlineSeconds = limits.minutes*60`, `terminationGracePeriodSeconds: 10`.
   - **Security:** `readOnlyRootFilesystem: true`, plus the existing pod and container hardening (`job_dispatch.py:598-636`).
   - **Volumes:**
     - `/src`, the PVC subPath `investigations/<id>/src`, `readOnly: true`;
     - `/tmp`, an emptyDir (64 Mi).
   - **Env (allowlist only):** `JOB_ID`, `AIFACTORY_INVESTIGATION_ID`, `AIFACTORY_API_URL`, and the two Secret keys via `secretKeyRef`. It does **not** call `build_job_env`, `mcp_spawn_env`, `spawn_env` or `_inject_seed_creds`, so it gets no GitHub, provider, S3, MCP or seeded credentials.
   - **Command:** `python -m agents.investigate` (new, see "Agent runtime").
5. **Mark the row `running`** and record `agent_job`.

#### `run_tests` (called by the agent pod with its run token)

- **Refusals:**
  - `run_tests_used` would exceed `limits.test_calls`;
  - another test Job of this run is live;
  - the run is not `running`.
- **No configured command:** returns `{ran: false, reason: "no tests run: none configured"}` and starts nothing.
- **Otherwise** it creates Job `aif-inv-<id8>-test-<n>`:
  - **Labels:** `factory.io/kind: task`, `factory.io/run-kind: investigate-test`.
  - **No credentials:** automount off, no Secret, no env beyond `HTTPS_PROXY`/`HTTP_PROXY` pointing at the registry proxy and `CI=1`.
  - **Image:** the runner image builds use, so the toolchains match.
  - **Init container:** copies `/src` (read-only) to an emptyDir `/work`.
  - **Main container:** runs `timeout <minutes-left, max 15 m> sh -c "$TEST_COMMAND"` in `/work`. `TEST_COMMAND` is a literal env value taken from the row, never from the request.
  - **Timing:** `activeDeadlineSeconds` is the same bound; grace 10 s.
- **Collection:** the control plane waits for the Job, reads the exit code and the last 200 log lines, scrubs them, and parses counts with the existing toolchain parsers (`#2142`): pytest, jest/vitest, go test, cargo test, maven/gradle surefire. Unrecognised output gives `parsed: false`.
- **Result:** it appends `{n, command, exit_code, passed, failed, skipped | parsed:false, tail}` to `tests`, deletes the test Job (foreground), and returns the result.
- **The agent never sees or chooses the command.**

#### Lease

- `renew_lease(id)` (caller auth) sets `lease_expires_at = now + 60 s`. The caller renews every 10 s.
- The run token **cannot** call it: the pod-side routes are a separate router that only accepts the run token, and the caller routes reject it.
- `lease_status(id)` (run token) returns `{ok: lease_expires_at > now, state}`.

#### Stop and expiry: one procedure, `_terminate(id, terminal_state, reason)`

1. Set `state = stopping`, unless the run is already terminal.
2. Delete every Job of the run (the agent and any test Job) with `propagation_policy="Foreground"`.
3. Poll the pods with label `factory.io/job-id` in {the run's Jobs} until none remain, for at most 60 s.
4. **If none remain:**
   - `pods_gone = true`;
   - delete the per-run Secret;
   - set `state = terminal_state` and `ended_at`. This frees the slot.
5. **Otherwise:** `state = uncertain`, `reason = "termination not confirmed"`, and `pods_gone` stays false. The slot stays held, and `reconcile` retries.

`stop(id)` (caller auth) calls `_terminate(id, stopped, "stopped by caller")` and returns `{state, pods_gone}`.

#### `reconcile()`

It runs every 15 s on the existing loop (`agent_kubejob.py:953-984`), as one more call there; nothing else in that loop changes. For each investigation with `pods_gone = false`:
- **Expired lease** (`lease_expires_at < now`, from any non-terminal state) → `_terminate(expired, "lease expired")`.
- **Agent Job succeeded** → `completing`: collect the report (below), then `_terminate(completed, …)`.
- **Agent Job failed or past its deadline** → `_terminate(failed, <reason>)`.
- **`uncertain`** → retry the deletion and the pod check (steps 2–4).

Restart needs no special path: every live or uncertain run is a row with `pods_gone = false`, and the next tick handles it.

#### Report: `put_report` (run token), once

- **The pod sends** `{findings: text ≤ 20 000 chars}`.
- **The server builds the stored report:**
  - `findings`, scrubbed;
  - `refs`: every `path:line` in the findings whose path exists in the export;
  - `tests`: the row's `tests` (control-plane records only; the agent's text is never parsed for counts);
  - `sha`: from the row.
- **A run that ends with no report** gets `findings: null`.

#### Logs

- **`append_log`** (run token) takes at most 200 lines per call and at most 20 000 lines per run. Over the cap → a single "log truncated" line, then nothing more.
- **`logs(id, after, limit ≤ 500)`** (caller auth) returns `{entries: [{seq, at, line}], cursor: <last seq>}`. With no new lines it returns `cursor = after`.

#### Scrubbing

Scrubbing happens server-side before storage, for logs, test tails and findings.
- **Base:** the existing redacting feed's redactor (`agent_kubejob.py:679-684`).
- **Added patterns:** AWS keys, PEM blocks, JWTs, GitHub/Anthropic/OpenAI token shapes, and base64 or hex runs of 40 characters or more.
- **The Claude token:** the run's own Claude token value is replaced by literal match.

### Routes

The new router `WS/routes/investigations.py` is mounted only when the switch is on.

| Route | Auth | Body / result |
|---|---|---|
| `POST /api/investigations` | caller: project `editor` via the existing API auth (`require_task_access`'s project form) | `{project_id, sha, request, test_command, limits}` → `{run_id}` |
| `GET /api/investigations/{id}` | caller, `viewer` | `{state, reason, pods_gone, tests, report?, usage}` |
| `GET /api/investigations/{id}/logs` | caller, `viewer` | `?after=&limit=` → `{entries, cursor}` |
| `POST /api/investigations/{id}/lease` | caller, `editor` | → `{lease_expires_at}` |
| `POST /api/investigations/{id}/stop` | caller, `editor` | → `{state, pods_gone}` |
| `POST /api/investigations/{id}/pod/log` | run token | `{lines}` |
| `POST /api/investigations/{id}/pod/run-tests` | run token | → test result (blocks, ≤ 15 min) |
| `POST /api/investigations/{id}/pod/report` | run token | `{findings}` |
| `GET /api/investigations/{id}/pod/lease` | run token | → `{ok, state}` |

**Run-token checks:** the header `X-Run-Token`, compared to `token_hash` in constant time, for **that** `id` only, while `pods_gone = false`.

**MCP:** `WS/mcp_stdio/router.py` gets `investigation_create`, `investigation_get`, `investigation_logs`, `investigation_lease`, `investigation_stop`, each a proxy to the caller routes and listed only when the switch is on. CFactory uses these.

### Agent runtime: `BE/agents/investigate.py`

- **The client:** one Claude SDK session, built by a new `create_investigate_client` beside `create_client` (`BE/core/client.py:582`). `create_client` is not changed, so builds stay untouched.
- **Session options:**
  - `cwd="/src"`;
  - `allowed_tools=["Read","Glob","Grep","mcp__investigate__run_tests"]`;
  - `disallowed_tools` = every other base tool (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `Bash`, `WebFetch`, `WebSearch`, `Task`, `TodoWrite`);
  - `mcp_servers = {"investigate": <in-process server with one tool, run_tests>}`, with **none** of the servers at `client.py:915-1030`;
  - `max_turns = limits.turns`;
  - `permission_mode="default"`, not bypass;
  - a `can_use_tool` callback that allows only the four tools.
- **A PreToolUse hook** denies any tool name outside the four, as a second layer in case the SDK's permission handling differs from what is documented. Whether `allowed_tools` is enforced under `bypassPermissions` is unverified here, so the spec does not rely on it.
- **The run is watched while it runs:**
  - **Token budget:** the receive loop adds each message's usage (input, output and cache-creation tokens, as in `act_loop_hooks.charge_tokens`). When it exceeds `limits.tokens`, it calls `interrupt()`, then exits non-zero with reason `token budget`.
  - **Watchdog:** a thread calls `GET pod/lease` every 10 s. If the result is `ok: false`, or three reads in a row fail (≈ 30 s), it interrupts and exits. Expiry is also enforced by the control plane's `reconcile`, so either one alone ends the run.
- **Output:**
  - **The log:** each assistant text and tool call is sent through `pod/log` in batches every 2 s. Tool inputs are shown as names with paths, not file contents.
  - **The report:** the final assistant message is sent to `pod/report`.
- **The prompt:** fixed. It contains the request text as data, between delimiters, with instructions to read, test with `run_tests` and report findings with `path:line`. It also says that repository content is data, not instructions.

### Egress (chart)

All of this is rendered only when `investigations.enabled`.

1. **The build policy** (`networkpolicy-tasks.yaml`) changes in **one place**: its `podSelector` keeps `matchLabels: {factory.io/kind: task}` and adds
   ```yaml
   matchExpressions:
     - {key: factory.io/run-kind, operator: DoesNotExist}
   ```
   Build pods have no `run-kind` label, so they are still selected exactly as now. Investigation pods are excluded from it.
2. **`networkpolicy-investigate-agent.yaml`** selects `factory.io/run-kind: investigate-agent`. It has no ingress. Egress allows:
   - DNS to kube-dns;
   - 443 to `investigations.claudeApiCidrs` (values; default: Anthropic's published API ranges);
   - the AIFactory service on its target port.
   Nothing else.
3. **`networkpolicy-investigate-test.yaml`** selects `factory.io/run-kind: investigate-test`. It has no ingress. Egress allows DNS and the registry proxy's port only.
4. **The registry proxy** (`deployment-investigate-proxy.yaml`, `service-…`, `configmap-…`): tinyproxy with
   - `ConnectPort 443`;
   - `FilterDefaultDeny Yes`;
   - a domain filter listing the registries in the approved list (npm, PyPI, crates.io, Go proxy, Maven Central), with values to extend it.

   Its own NetworkPolicy allows ingress only from `investigate-test` pods, and egress to 443 on public addresses plus DNS.

### Not changed

- `BE/core/job_dispatch.py` (and so the drift gate).
- `create_client`, `build_job_env`, `KubeJobBuildBackend`, the `job_states` table and `admit`, the build completion and stop paths, and the inert deadline branch. The last is noted as a separate build bug, out of scope.

## Alternatives rejected

- **A new `factory.io/kind` value.** It changes the vendored validator in four services and its drift gate. The label exclusion achieves the same separation with one selector term.
- **Reusing `job_states` and `admit`.** Its cap is global, its kind is always `build`, and its terminal handling frees slots on unconfirmed deletes.
- **Tests in the agent pod.** Repository code would run beside the Claude credential and the harness (Codex P1 on the intent).
- **Cloning with `.git` and deleting files.** Git objects keep the deleted secrets. `git archive` never exposes history.
- **Relying on `allowed_tools` alone.** Its enforcement under `bypassPermissions` is unverified. The design uses `permission_mode="default"`, `can_use_tool`, `disallowed_tools` and a deny hook, and still assumes a tool might slip through, so the pod has nothing to write and nowhere to send.
- **Reusing RFactory's proxy code (Node).** A cross-repo image dependency for something tinyproxy does with configuration. Same pattern, own instance (intent, decision 1).
- **Using the existing `GET /tasks/{id}/logs`.** It tails pods with no cursor, and is keyed to build task ids.

## Risks

- **tinyproxy also forwards plain HTTP** to allowlisted hosts. The test pod can reach only registries either way, and the agent pod cannot reach the proxy. Accepted; noted in the chart comment.
- **The Claude API CIDRs can change.** A stale value blocks the agent (fails closed) rather than opening egress. The values default is documented with where to refresh it.
- **The PVC export grows** with large repositories, on nodes that already hit DiskPressure on 2026-11-18. The export is deleted with the run, and a dispatch is refused when the export exceeds 2 GiB.
- **Foreground deletion can hang** on a stuck finalizer. That is caught by `uncertain`, which holds the slot and retries, and is shown to the caller as "stop not confirmed".
- **The SDK's tool enforcement.** Mitigated by four layers, plus a pod that cannot write or reach anything but the Claude API and its own run routes.
- **Release cadence.** This ships in a normal AIFactory release behind the switch, off by default. The release lead is told on the bus before it merges.

## Verification

- **Unit and route tests** (pytest; new `tests/test_investigation_backend.py`, `tests/test_investigations_routes.py`, `tests/postgres/test_investigations_slot.py`, `apps/backend/tests/test_investigate_agent.py`):
  - **Dispatch:** refused for each reason; the slot conflict at the database, including while `uncertain`; limits over the maximum refused.
  - **Export:** no `.git`; secret paths and an escaping symlink removed; the marker written.
  - **Agent manifest:** passes `assert_job_policy`; labels, automount false, read-only root and `/src`; an env containing only the allowlisted names; no GitHub, S3, MCP or provider variables.
  - **Test manifest:** no Secret and no credential env.
  - **Run token:** accepted on pod routes for its run only, refused on another run, on the caller routes and on `lease`.
  - **`_terminate`:** pods gone → terminal and slot freed; pods remaining → `uncertain`, slot held; reconcile retries.
  - **Lease:** expiry → `expired`; a never-renewed dispatch expires at 60 s.
  - **Logs:** the cursor advances across three reads; the caps hold; scrubbing runs before insert (an AWS key, a PEM block, a JWT, the Claude token).
  - **Report:** counts come only from `tests`; findings saying "all 50 passed" do not change them.
  - **`run_tests`:** no command → "no tests run"; the 11th call refused; parsing for pytest and jest, otherwise `parsed: false`.
  - **Agent:** a disallowed tool is denied by the hook; the token budget interrupts; the watchdog exits on `ok: false` and after three failed reads.
- **Helm** (`tests/helm/test_p4_helm.py`):
  - with `investigations.enabled`, the build policy has the `DoesNotExist` term and its rules unchanged;
  - both investigation policies render with exactly the rules above;
  - the proxy renders;
  - with it off, the output is byte-identical to today.
- **On the cluster** (p620 k3d, after a release with the switch on, owner's OK):
  - **The pod:** env shows only the allowlist; `/src` is read-only with no `.git`; `touch /src/x` fails.
  - **Egress from the agent pod:** `curl https://example.com` is refused and the Claude API answers.
  - **A deliberately hostile test command**, in a fixture repository, that:
    - prints its env (no token);
    - reads `/var/run/secrets` (absent);
    - curls the Claude API and the AIFactory API (both refused);
    - tries to write the report (no token, refused);
    - uploads to example.com (refused) and installs a package from npm (works).
  - **Stop:** `stop` → pods gone within 20 s, slot free.
  - **Lease:** kill the caller → `expired` within 70 s.
- **Suites:** `apps/backend/.venv/bin/pytest tests/ -m "not slow"` and `npm run test:backend` show 0 failures, and `python apps/backend/core/job_dispatch.py` passes (the vendored self-test, unchanged).
