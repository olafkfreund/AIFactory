---
status: draft
issue: 2156
intent: intent/2026-11-19-2156-investigate-run-kind.md
---

# Spec: a read-only `investigate` run kind (#2156)

Paths: **WS** = `apps/web-server/server/`, **BE** = `apps/backend/`.

## Design

### Deviation from the approved intent (for the owner to approve with this spec)

- **What the intent says** (outcome 4): investigation pods carry their **own `factory.io/kind` value**.
- **What this spec does instead:**
  - pods keep `factory.io/kind: task` (the vendored validator requires it);
  - they add `factory.io/run-kind`;
  - the build policy excludes them with a `DoesNotExist` term.
- **What stays the same:** build pods are selected exactly as now, and investigation pods by their own policies only. Before every dispatch, the effective selection is **checked** (see "Network check").

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
- the **network check** fails (see "Network check" below);
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
4. **The agent Job** `aif-inv-<id8>-agent` comes from the module's **own builder**, `_agent_manifest`, not `build_job_manifest`. `build_job_manifest` adds a GCP Secret and `GOOGLE_APPLICATION_CREDENTIALS` when `FACTORY_GCP_CREDS_SECRET` is set, plus telemetry env (`job_dispatch.py:516-579`). The final manifest is then passed through `assert_job_policy`, and through a new `_assert_investigation_manifest` that refuses:
   - any env name, volume or mount outside the allowlist;
   - `automountServiceAccountToken` not false;
   - a pod label `app.kubernetes.io/instance`, which would put the pod inside the release policy (`networkpolicy.yaml`, release-scoped);
   - a missing `factory.io/investigation: <id>` **pod** label.

   The manifest:
   - **Pod labels:** `factory.io/kind: task`, `factory.io/run-kind: investigate-agent`, `factory.io/investigation: <id>`, `factory.io/service: aifactory`, `app: aifactory-investigate`.
   - **Account:** `automountServiceAccountToken: false`, and no `serviceAccountName`.
   - **Timing:** `activeDeadlineSeconds = limits.minutes*60`, `terminationGracePeriodSeconds: 10`, `backoffLimit: 0`.
   - **Security:** the existing pod and container hardening, plus `readOnlyRootFilesystem: true`.
   - **Volumes:**
     - `/src` (PVC subPath `investigations/<id>/src`, `readOnly: true`);
     - `/tmp`, an emptyDir (256 Mi), which holds `HOME=/tmp/home` and `CLAUDE_CONFIG_DIR=/tmp/claude`, because the SDK writes its session files there.
   - **Env (exactly):** `JOB_ID`, `AIFACTORY_INVESTIGATION_ID`, `AIFACTORY_API_URL`, `HOME`, `CLAUDE_CONFIG_DIR`, and the two Secret keys via `secretKeyRef`.
   - **Command:** `python -m agents.investigate`.

   **The Secret** has an `ownerReference` to the agent Job, so garbage collection removes it with the Job, and the module also deletes it on termination. The chart adds `secrets: create, get, delete` and `networkpolicies: list` to the sandbox Role, only when `investigations.enabled`.

   **Ordering, safe against crashes:**
   1. The Job name is **deterministic** and written to the row **before** the create call.
   2. Then the Job is created.
   3. Then `state = running`.

   A crash at any point leaves a row that names its Job, and every pod carries the investigation label, so `reconcile` finds both.


#### `run_tests` (called by the agent pod with its run token)

- **Admission is serialised per run.** Inside one transaction holding `SELECT … FOR UPDATE` on the row, the call is refused unless:
  - `state = running`;
  - no test Job of this run is live;
  - `run_tests_used < limits.test_calls`.

  The transaction then increments `run_tests_used`, appends the deterministic Job name to `test_jobs`, and commits. Only then is the Job created. `_terminate` takes the same lock to set `stopping`, so no test Job can be admitted after stopping begins. A Job that is mid-create when the stop lands is already named in the row and labelled, so the pod-gone check waits for it.
- **No configured command:** returns `{ran: false, reason: "no tests run: none configured"}` and starts nothing.
- **Otherwise** it creates Job `aif-inv-<id8>-test-<n>`:
  - **Labels:** `factory.io/kind: task`, `factory.io/run-kind: investigate-test`.
  - **No credentials:** automount off, no Secret, no env beyond `HTTPS_PROXY`/`HTTP_PROXY` pointing at the registry proxy and `CI=1`.
  - **Image:** the runner image builds use, so the toolchains match.
  - **Init container:** copies `/src` (read-only) to an emptyDir `/work`.
  - **Main container:** runs `timeout <minutes-left, max 15 m> sh -c "$TEST_COMMAND"` in `/work`. `TEST_COMMAND` is a literal env value taken from the row, never from the request.
  - **Timing:** `activeDeadlineSeconds` is the same bound; grace 10 s.
- **Collection:** the control plane waits for the Job, then reads its exit code and its log (up to 5 MiB; beyond that it is marked `truncated`).
  - **Counts:** a new module, `WS/services/investigation_tests.py`, parses them from the summary lines. AIFactory has no count parsers today; `GateResult` holds only pass, skip, exit code and tail (`BE/agents/gate_runner.py:83-97`). Formats:
    - pytest (`N passed, M failed, K skipped`);
    - jest/vitest (`Tests: …`);
    - go test (counting `--- PASS/FAIL/SKIP`);
    - cargo (`test result: …`);
    - surefire (`Tests run: …`).
  - **No counts:** unrecognised or truncated output with no summary gives `parsed: false`.
  - **The tail:** the last 200 lines are scrubbed and kept.
- **Result:** it appends `{n, command, exit_code, passed, failed, skipped | parsed:false, tail}` to `tests`, deletes the test Job (foreground), and returns the result.
- **The agent never sees or chooses the command.**

#### Lease

- `renew_lease(id)` (caller auth) sets `lease_expires_at = now + 60 s`. The caller renews every 10 s.
- The run token **cannot** call it: the pod-side routes are a separate router that only accepts the run token, and the caller routes reject it.
- `lease_status(id)` (run token) returns `{ok: lease_expires_at > now, state}`.

#### Stop and expiry: `_terminate(id, terminal_state, reason)`, non-blocking

1. **Under the row lock:** if the run is not terminal, set `state = stopping`, and record `terminal_state` and `reason` as the pending outcome.
2. **Delete** every Job named in the row, plus any Job with label `factory.io/investigation=<id>`, with `propagation_policy="Foreground"`. Then return. Neither call waits.

**Each reconcile tick, for runs in `stopping`:**
- **When** no Job **and** no pod with label `factory.io/investigation=<id>` remains:
  - set `pods_gone = true`;
  - delete the Secret;
  - release the pooled Claude credential (`agent_credential`'s release);
  - delete the export;
  - set the pending terminal state and `ended_at`. This frees the slot.
- **After 120 s in `stopping`:** set `state = uncertain`, `reason = "termination not confirmed"`, and keep retrying the deletes every tick.

**`stop(id)`** (caller auth) runs `_terminate(id, stopped, "stopped by caller")`. It then waits at most 20 s, checking for pods every 2 s, and returns `{state, pods_gone}`. The caller polls `GET` for `pods_gone` after that.

#### `reconcile()`, on its own loop

It runs on **its own** supervised asyncio task, started at app startup when the switch is on, every 10 s. It is **not** part of the build loop (`agent_kubejob.py:953-984`), so build reconciliation timing is unchanged. Each tick does only non-blocking API calls, with at most 50 runs per tick.

For each run with `pods_gone = false`:
- **Expired lease** (`lease_expires_at < now`) → `_terminate(expired, "lease expired")`.
- **Agent Job succeeded** → `completing`: build the report, then `_terminate(completed, …)`.
- **Agent Job failed or past its deadline** → `_terminate(failed, <reason>)`.
- **`stopping` or `uncertain`** → the pod check above.
- **`queued` with its Job name recorded but no Job:**
  - if the lease is still valid, create the Job (idempotent, by name);
  - otherwise there are no pods to wait for, so set `pods_gone = true` and mark it `failed (dispatch)`.

Restart needs no special path: every row with `pods_gone = false` is picked up on the next tick.

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
- **Across lines and batches:** a `-----BEGIN … PRIVATE KEY-----` line opens a redaction that drops every line until the matching `END`, across `append_log` batches. The open state is kept on the row (`scrub_open`).

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
| `GET /api/investigations/{id}/pod/config` | run token | → `{request, sha, limits, has_tests}`, fixed at dispatch |

**Run-token checks:**
- **The header:** `X-Run-Token`, compared to `token_hash` in constant time, for **that** `id` only, while `pods_gone = false`.
- **The middleware exemption.** The API auth middleware rejects any request without a bearer token or session cookie (`WS/auth.py:334-354`). It gets one narrow exemption:
  - **What it matches:** only the exact pattern `^/api/investigations/[0-9a-f-]{36}/pod/(log|run-tests|report|lease|config)$`, with the method matching the table;
  - **What happens then:** the request goes to the pod router, whose dependency **requires** a valid run token;
  - **Everything else is unchanged:** the caller routes stay behind the existing auth.
- **Tested through the full app middleware stack:**
  - a pod route without the token → 401;
  - a pod route with the token of another run → 403;
  - a caller route with only a run token → 401.

**MCP:** `WS/mcp_stdio/router.py` gets `investigation_create`, `investigation_get`, `investigation_logs`, `investigation_lease`, `investigation_stop`, each a proxy to the caller routes and listed only when the switch is on. CFactory uses these.

### Agent runtime: `BE/agents/investigate.py`

- **The client:** one Claude SDK session, built by a new `create_investigate_client` beside `create_client` (`BE/core/client.py:582`). `create_client` is not changed, so builds stay untouched.
- **Inputs:** the runtime reads `GET pod/config` once at start: the request, the limits and `has_tests`. It never takes them from the environment or arguments.
- **Session options:**
  - `cwd="/src"`;
  - `allowed_tools=["Read","Glob","Grep","mcp__investigate__run_tests"]`;
  - `disallowed_tools` = every other base tool (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `Bash`, `WebFetch`, `WebSearch`, `Task`, `TodoWrite`);
  - `mcp_servers = {"investigate": <in-process server with one tool, run_tests>}`, with **none** of the servers at `client.py:915-1030`;
  - `max_turns = limits.turns`;
  - `permission_mode="default"`, not bypass;
  - a `can_use_tool` callback that allows only the four tools.
- **The path boundary: a PreToolUse hook that always applies.** `allowed_tools` auto-approves matching tools, so `can_use_tool` is no check for them, and the hook is the real boundary. It:
  - denies any tool outside the four;
  - for `Read` (`file_path`), `Glob` (`path`, `pattern`) and `Grep` (`path`, `glob`), resolves the path with `os.path.realpath`, relative to `/src`, and denies it unless the result is `/src` or below;
  - denies any `pattern`/`glob` that is absolute or contains `..`.

  This keeps `/proc`, `/tmp/claude` and `/var/run` out of reach.
- **Defence in depth:** `allowed_tools` drops to `["mcp__investigate__run_tests"]`, so Read, Glob and Grep go through `can_use_tool`, which applies the same path check. The hook remains the guarantee.
- **The run is watched while it runs:**
  - **Token budget, with `include_partial_messages=True`:**
    - **Per response:** for each response id, take `input_tokens + cache_creation_input_tokens` from its `message_start` event, and the **latest cumulative** `output_tokens` from its `message_delta` events.
    - **Total:** the sum across distinct response ids. Repeated assistant messages for the same id are not added again; cache reads are excluded, as in `charge_tokens`.
    - **Abort:** when the total crosses `limits.tokens` mid-stream, it calls `interrupt()`, then exits non-zero with reason `token budget`.
    - **Recorded:** `tokens_used` is reported in each `pod/log` batch.
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

   The base release policy allows AIFactory's ingress only from the ingress-controller namespace (`networkpolicy.yaml:31-44`). So an **ingress** policy `networkpolicy-investigate-api.yaml` selects the AIFactory server pods (`selectorLabels`) and allows ingress from `factory.io/run-kind: investigate-agent` on the server's target port.
3. **`networkpolicy-investigate-test.yaml`** selects `factory.io/run-kind: investigate-test`. It has no ingress. Egress allows DNS and the registry proxy's port only.
4. **The registry proxy** (`deployment-investigate-proxy.yaml`, `service-…`, `configmap-…`) is **squid**, configured for CONNECT only:

   ```text
   acl SSL_ports port 443
   acl CONNECT method CONNECT
   acl registries dstdomain registry.npmjs.org pypi.org files.pythonhosted.org
                            crates.io static.crates.io index.crates.io
                            proxy.golang.org sum.golang.org repo.maven.apache.org
                            repo1.maven.org
   http_access deny !CONNECT
   http_access deny CONNECT !SSL_ports
   http_access allow CONNECT registries
   http_access deny all
   ```

   - **Names:** `dstdomain` matches names exactly; there are no leading-dot wildcards, and values can add exact names only.
   - **Labels:** the proxy pods carry `factory.io/run-kind: investigate-proxy` and **not** the release `instance` label.
   - **Its own NetworkPolicy:**
     - ingress only from `investigate-test` pods on 3128;
     - egress to 443 on public addresses plus DNS.

### Network check (before every dispatch)

1. The control plane lists the NetworkPolicies in its namespace (`networkpolicies: list`).
2. It evaluates each `podSelector` (`matchLabels` and `matchExpressions`: In, NotIn, Exists, DoesNotExist) against the **exact pod labels** of the agent and test manifests.
3. **It refuses** the dispatch unless:
   - the agent pod is selected by `…-investigate-agent` and **no other** policy;
   - the test pod is selected by `…-investigate-test` and no other;
   - the ingress policy `…-investigate-api` exists.

   An older build policy without the exclusion, an `extraRules` policy, or any other policy that selects them makes the check fail with the policy's name.

### Not changed

- `BE/core/job_dispatch.py` (and so the drift gate).
- `create_client`, `build_job_env`, `KubeJobBuildBackend`, the `job_states` table and `admit`, the build completion and stop paths, and the inert deadline branch. The last is noted as a separate build bug, out of scope.

## Alternatives rejected

- **A new `factory.io/kind` value.** It changes the vendored validator in four services and its drift gate. The label exclusion achieves the same separation with one selector term.
- **Reusing `job_states` and `admit`.** Its cap is global, its kind is always `build`, and its terminal handling frees slots on unconfirmed deletes.
- **Tests in the agent pod.** Repository code would run beside the Claude credential and the harness (Codex P1 on the intent).
- **Cloning with `.git` and deleting files.** Git objects keep the deleted secrets. `git archive` never exposes history.
- **Relying on `allowed_tools` alone.** Its enforcement under `bypassPermissions` is unverified. The design uses `permission_mode="default"`, `can_use_tool`, `disallowed_tools` and a deny hook, and still assumes a tool might slip through, so the pod has nothing to write and nowhere to send.
- **Reusing RFactory's proxy code (Node).** A cross-repo image dependency for something squid does with configuration. Same pattern, own instance (intent, decision 1).
- **tinyproxy.** It forwards plain HTTP too; `ConnectPort` only limits CONNECT ports, and its filters are regular expressions. It cannot meet CONNECT-only (Codex review).
- **`build_job_manifest` for investigation pods.** It injects GCP credentials and telemetry env by environment switch.
- **Investigation reconcile inside the build loop.** Slow deletes would delay build reconciliation.
- **Using the existing `GET /tasks/{id}/logs`.** It tails pods with no cursor, and is keyed to build task ids.

## Risks

- **Registry hosts can redirect** to CDNs outside the list (e.g. npm tarballs, Maven mirrors). Downloads from each listed registry are part of verification, and any CDN name a registry needs is added by exact name.
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
  - **Agent:**
    - a disallowed tool is denied by the hook;
    - the watchdog exits on `ok: false` and after three failed reads;
    - the agent consumes the values from `pod/config`.
  - **The path boundary:** `Read /proc/self/environ`, `Read /src/../etc/passwd`, a `/src` symlink pointing outside, `Glob` with `pattern: "/**"`, and `Grep path=/tmp` are all denied; `Read /src/README.md` is allowed.
  - **Token accounting**, with fixtures for:
    - a response id repeated over three assistant messages, counted once;
    - cached input;
    - cumulative `output_tokens` deltas;
    - a budget crossed mid-stream, which interrupts before the session completes.
  - **The manifest guard,** with `FACTORY_GCP_CREDS_SECRET` and OTEL set in the control-plane env: the investigation manifests contain neither.
  - **Concurrency:**
    - two concurrent dispatches for one project → one;
    - two concurrent `run_tests` → one;
    - `stop` against a `run_tests` that is mid-create → no test Job is admitted after `stopping`, and the slot stays held until both Jobs' pods are gone.
  - **Crashes and restart:**
    - after the Job name is recorded but before the create → reconcile creates it or fails the run;
    - after the create but before `running` → found by name and label;
    - a restart with `stopping` and `uncertain` rows → resumed.
  - **Limits:** zero, negative or above-maximum → refused.
  - **Scrubbing:** a PEM block split across two `append_log` batches is removed.
  - **Cleanup:** after the terminal state, the pooled credential is released and the export and the Secret are deleted.
  - **The parsers:** every format, plus truncated output.
  - **The build loop:** its ticks continue on time while an investigation's deletion is stuck.
- **Helm** (`tests/helm/test_p4_helm.py`):
  - with `investigations.enabled`, the build policy has the `DoesNotExist` term and its rules unchanged;
  - both investigation policies render with exactly the rules above;
  - the proxy renders;
  - with it off, the output is byte-identical to today;
  - **the network check, against the rendered chart:** it passes on the default render, and fails on a render whose build policy lacks the exclusion or that has an `extraRules` policy selecting `factory.io/kind: task`.
- **On the cluster** (p620 k3d, after a release with the switch on, owner's OK):
  - **The pod:** env shows only the allowlist; `/src` is read-only with no `.git`; `touch /src/x` fails.
  - **Egress from the agent pod:** `curl https://example.com` is refused and the Claude API answers.
  - **A deliberately hostile test command**, in a fixture repository, that:
    - prints its env (no token);
    - reads `/var/run/secrets` (absent);
    - curls the Claude API and the AIFactory API (both refused);
    - tries to write the report (no token, refused);
    - uploads to example.com (refused) and installs a package from npm (works);
    - through the proxy, refuses a plain `http://registry.npmjs.org/`, `registry.npmjs.org.evil.example`, and port 8443.
  - **Downloads** from each listed registry (npm, PyPI, crates, Go, Maven) succeed through the proxy.
  - **A real SDK session** runs on the hardened manifest (read-only root, `/tmp` config).
  - **Stop:** `stop` → pods gone within 20 s, slot free.
  - **Lease:** kill the caller → `expired` within 70 s.
- **Suites:** `apps/backend/.venv/bin/pytest tests/ -m "not slow"` and `npm run test:backend` show 0 failures, and `python apps/backend/core/job_dispatch.py` passes (the vendored self-test, unchanged).
