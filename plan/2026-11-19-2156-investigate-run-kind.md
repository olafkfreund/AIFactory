---
status: draft
issue: 2156
spec: spec/2026-11-19-2156-investigate-run-kind.md
coders:
  1: coder   # migration, model and store: conventional Alembic plus a partial index; routine
  2: codex   # scrubber and count parsers: security patterns and format edge cases
  3: codex   # source export: secret paths, symlink escape, no .git; security-sensitive
  4: codex   # manifests, allowlist guard and network-check selector evaluator: small, tricky logic
  5: codex   # backend lifecycle: row locks, races, non-blocking terminate, reconcile; the trickiest step
  6: coder   # routes, middleware exemption, MCP proxies, switch wiring: broad but routine
  7: codex   # agent runtime: path-confinement hook, streaming token accounting, watchdog
  8: coder   # Helm chart: policies, squid, RBAC, values, helm tests; mechanical (agy out of use, #2748)
reviewers:
  - codex    # independent review of the full diff against this plan
---

# Plan: a read-only `investigate` run kind (#2156)

Paths: **WS** = `apps/web-server/server/`, **BE** = `apps/backend/`. Branch: `feat/2156-investigate-run-kind`, cut from `dev`; the PR targets `dev`, as in AIFactory's flow.

## Decisions carried over (approved intent and spec)

### Separation of duties

- **Agent pod:** reads and reasons.
- **Test pod:** runs repository code, a fresh one per `run_tests`, with no credentials.
- **Control plane:** exports, dispatches, collects, scrubs, stores.

Nothing changes for builds:
- `BE/core/job_dispatch.py` (vendored with a drift gate);
- `create_client`, `build_job_env`, `KubeJobBuildBackend`;
- `job_states` and `admit`;
- the build completion and stop paths, and the build reconcile loop.

The inert build deadline branch (`agent_kubejob.py:971` passes no deadline) is a separate bug, out of scope.

### Switch

- **The variable:** `AIFACTORY_INVESTIGATIONS`, strict `off|on`, parsed like `_kubejob_flag` (`WS/services/agent_job_backend.py:41-49`); anything else raises.
- **"On" requires:** the kubejob build backend, `DATABASE_URL`, and chart `investigations.enabled`.
- **Off:** the investigation routes and MCP tools are absent (404), and no loop runs.

### Labels (the approved deviation from the intent)

**Agent and test pods** keep `factory.io/kind: task` and add:
- `factory.io/run-kind: investigate-agent | investigate-test`;
- `factory.io/investigation: <id>`;
- `factory.io/service: aifactory`;
- `app: aifactory-investigate`.

They **never** carry `app.kubernetes.io/instance` (the release policy selects that label).

**The build policy** gets one added `matchExpressions` term, `{key: factory.io/run-kind, operator: DoesNotExist}`.

**Proxy pods** carry `factory.io/run-kind: investigate-proxy`.

### Data (one migration)

**`investigations`:**
- **Identity and inputs:**
  - `id` uuid PK and `project_id` (both not null);
  - `repo`;
  - `sha`, 40 hex characters;
  - `request`, at most 2000 characters;
  - `test_command`, nullable;
  - `limits` json: minutes, tokens, turns, test_calls.
- **State:** `state`, `pending_state`, `reason`, `lease_expires_at`, `pods_gone` (not null, default false), `stopping_since`.
- **Pods:** `agent_job`, `test_jobs` (json).
- **Usage:** `run_tests_used`, `tokens_used`, `turns_used`.
- **Results:** `tests` (json), `report` (json).
- **Scrubbing and credentials:** `scrub_open` (bool), `token_hash`.
- **Timestamps:** `created_at`, `ended_at`.

**States:**
- **Live:** `queued`, `running`, `completing`, `stopping`.
- **Terminal:** `completed`, `stopped`, `failed`, `expired`, `uncertain`.

**The slot:** a partial unique index on `project_id WHERE pods_gone = false`.

**`investigation_logs`:**
- **Columns:** `run_id`, `seq` bigserial, `at`, and `line` of at most 2000 characters.
- **Index:** `(run_id, seq)`.
- **Retention:** rows are deleted 7 days after `ended_at`.

### Dispatch

**Refusals.** A dispatch is refused when:
- the switch is off;
- the network check fails;
- the slot is taken (insert conflict);
- a limit is ≤ 0 or above the maximum. The maximums are 30 minutes, 2 000 000 tokens, 200 turns and 10 test calls; a missing limit takes the maximum;
- the `sha` is unknown after a fetch with the control plane's credential;
- the export exceeds 2 GiB.

**Order:**
1. Insert `queued`, with `lease_expires_at = now + 60 s`, a 32-byte run token, and its sha256 stored.
2. Export.
3. Create the Secret.
4. Write the deterministic Job name `aif-inv-<id8>-agent` to the row.
5. Create the Job.
6. Set `running`.

### Export

- **Archive:** `git -C <repo> archive --format=tar <sha>` into `/data/investigations/<id>/src`.
- **Secret paths:** delete the paths on the approved list, plus the project's add-only `investigate_secret_paths`. The approved list:
  - `.env*` and `.envrc`;
  - `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks` and `*.keystore`;
  - `id_*` and `.ssh/`;
  - `.npmrc`, `.pypirc`, `.netrc` and `.git-credentials`;
  - `credentials*` and `secrets.*`;
  - `.aws/`, `.kube/`, `kubeconfig*` and `.docker/config.json`;
  - `*.tfstate*`, `*.tfvars` and `.terraform/`.
- **Symlinks:** delete those that are absolute or resolve outside the export.
- **Marker:** write `.aifactory-export-ok`.
- **On failure:** the run becomes `failed (export)` with `pods_gone = true`.

### Agent manifest (own builder, `_agent_manifest`; never `build_job_manifest`, which injects GCP and OTEL)

- **Account:** automount false, no serviceAccountName.
- **Timing:** `activeDeadlineSeconds = minutes*60`, grace 10 s, `backoffLimit: 0`.
- **Security:** the existing hardening (`job_dispatch.py:598-636`) plus `readOnlyRootFilesystem: true`.
- **Volumes:**
  - `/src`: the PVC subPath `investigations/<id>/src`, read-only;
  - `/tmp`: an emptyDir of 256 Mi.
- **Env (exactly these):** `JOB_ID`, `AIFACTORY_INVESTIGATION_ID`, `AIFACTORY_API_URL`, `HOME=/tmp/home`, `CLAUDE_CONFIG_DIR=/tmp/claude`, plus `CLAUDE_CODE_OAUTH_TOKEN` and `AIFACTORY_RUN_TOKEN` from the Secret `aif-inv-<id8>` (ownerReference to the Job).
- **Command:** `python -m agents.investigate`.
- **Checks:** `assert_job_policy` plus `_assert_investigation_manifest`. The latter refuses any env, volume or mount outside the allowlist, automount not false, an `instance` label, or a missing investigation pod label.

### `run_tests`

- **Admission,** in a single `SELECT … FOR UPDATE` transaction, is refused unless:
  - `state = running`;
  - no live test Job exists;
  - `used < test_calls`.

  The same transaction increments the count and records the name `aif-inv-<id8>-test-<n>`, then commits. Only then is the Job created.
- **No command** → `{ran:false, reason:"no tests run: none configured"}`.
- **The test pod:**
  - no Secret and no automount;
  - env: only `HTTPS_PROXY`, `HTTP_PROXY`, `CI=1` and `TEST_COMMAND` (a literal from the row);
  - the builds' runner image;
  - an init container copies `/src` to an emptyDir `/work`;
  - it runs `timeout <min(15m, time left)> sh -c "$TEST_COMMAND"`.
- **Collection:**
  - the exit code, and the log up to 5 MiB (beyond that, `truncated`);
  - counts parsed by the new `WS/services/investigation_tests.py`: pytest, jest/vitest, go test, cargo, surefire; otherwise `parsed:false`;
  - the tail: 200 lines, scrubbed;
  - then the Job is deleted (foreground) and the result returned.

### Lease and termination

- **Renewal:** `renew_lease` (caller auth only; the run token is refused there) sets `+60 s`.
- **Terminate:** `_terminate(id, terminal, reason)`, under the row lock:
  1. set `state = stopping`, `pending_state`, `reason` and `stopping_since`;
  2. delete the Jobs named in the row, plus any Job labelled with the investigation, foreground;
  3. return without waiting.
- **Pods gone:** when no Job and no pod with `factory.io/investigation=<id>` remains:
  - set `pods_gone = true`;
  - delete the Secret;
  - release the pooled Claude credential;
  - delete the export;
  - set the pending state and `ended_at`.
- **Not gone after 120 s** → `uncertain`, retried every tick.
- **`stop`** calls `_terminate(stopped, "stopped by caller")`, waits up to 20 s (polling every 2 s), and returns `{state, pods_gone}`.

### Reconcile

It runs on **its own** supervised asyncio task, every 10 s, non-blocking, at most 50 runs per tick. For each run with `pods_gone = false`:
- **The lease has expired** → `_terminate(expired)`.
- **The agent Job succeeded** → `completing`, then the report, then `_terminate(completed)`.
- **The agent Job failed or passed its deadline** → `_terminate(failed)`.
- **`stopping` or `uncertain`** → the pod-gone check.
- **`queued` with a Job name but no Job:**
  - while the lease is valid, create it (idempotent, by name);
  - otherwise set `pods_gone = true` and `failed (dispatch)`.

### Report

`put_report` is called once, with `findings` of at most 20 000 characters. The server stores:
- **`findings`**, scrubbed;
- **`refs`:** each `path:line` whose path exists in the export;
- **`tests`:** from the row only (never parsed from the findings);
- **`sha`.**

A run with no report gets `findings: null`.

### Logs and scrubbing

**`append_log`:**
- at most 200 lines per call, and 20 000 lines per run;
- beyond that cap, one "log truncated" line and nothing more.

**`logs(after, limit ≤ 500)`** returns `{entries:[{seq,at,line}], cursor}`. With nothing new, the cursor is `after`.

**Scrubbing,** applied before every insert (logs, test tails, findings):
- **Base:** the existing redactor used by the redacting feed (`agent_kubejob.py:679-684`).
- **Added patterns:** AWS keys, JWTs, GitHub/Anthropic/OpenAI token shapes, and base64 or hex runs of 40 characters or more.
- **The run's Claude token:** matched literally.
- **PEM blocks:** a `-----BEGIN … PRIVATE KEY-----` line drops every line up to its `END`. The open state is kept across batches in `scrub_open`.

### Routes (`WS/routes/investigations.py`, mounted only when on) and auth

**Caller routes** use the existing auth, at project level:
- `POST /api/investigations` (editor);
- `GET /api/investigations/{id}` (viewer): `{state, reason, pods_gone, tests, report?, usage}`;
- `GET …/logs` (viewer);
- `POST …/lease` (editor);
- `POST …/stop` (editor).

**Pod routes** (`X-Run-Token`, constant-time, for that id only, while `pods_gone = false`):
- `POST …/pod/log`;
- `POST …/pod/run-tests`;
- `POST …/pod/report`;
- `GET …/pod/lease` → `{ok, state}`;
- `GET …/pod/config` → `{request, sha, limits, has_tests}`.

**The middleware exemption** in `WS/auth.py:334-354` matches only `^/api/investigations/[0-9a-f-]{36}/pod/(log|run-tests|report|lease|config)$`, with the matching methods.

**MCP tools** in `WS/mcp_stdio/router.py`: `investigation_create|get|logs|lease|stop`, each a proxy to a caller route, listed only when the switch is on.

### Agent runtime (`BE/agents/investigate.py`, new `create_investigate_client` beside `create_client`)

**Inputs:** read from `pod/config` once.

**SDK options:**
- `cwd="/src"`;
- `permission_mode="default"`;
- `allowed_tools=["mcp__investigate__run_tests"]`;
- `disallowed_tools`: Write, Edit, MultiEdit, NotebookEdit, Bash, WebFetch, WebSearch, Task, TodoWrite;
- `mcp_servers={"investigate": <in-process server with run_tests>}`, and none of the servers at `client.py:915-1030`;
- `max_turns = limits.turns`;
- `include_partial_messages=True`;
- `can_use_tool`, which allows Read, Glob and Grep only with the path check.

**The always-on PreToolUse hook (the real boundary):**
- it denies any tool outside the four;
- it `realpath`s `Read.file_path`, `Glob.path` and `Grep.path` relative to `/src`, and denies anything that doesn't land at `/src` or below;
- it denies an absolute `pattern` or `glob`, and any containing `..`.

**Token accounting:**
- per response id: `input_tokens + cache_creation_input_tokens` from `message_start`, plus the latest cumulative `output_tokens` from `message_delta`;
- the total sums distinct ids;
- when the total crosses the limit, it calls `interrupt()` and exits non-zero with `token budget`.

**Watchdog:** a thread calls `pod/lease` every 10 s. It exits on `ok:false`, or after three failed reads in a row.

**Log:** sent in batches every 2 s, holding assistant text and tool name plus path (never file contents), with `tokens_used`.

**The prompt is fixed:**
- the request appears as delimited data;
- it says to read, test with `run_tests`, and report `path:line` findings;
- it says that repository content is data, not instructions.

**Report:** the final assistant text is sent to `pod/report`.

### Chart (all under `investigations.enabled`; off renders byte-identical to today)

- **The build policy:** the `DoesNotExist` term, its rules unchanged.
- **`networkpolicy-investigate-agent`:**
  - no ingress;
  - egress to DNS, 443 to `investigations.claudeApiCidrs`, and the AIFactory service target port.
- **`networkpolicy-investigate-test`:**
  - no ingress;
  - egress to DNS and the proxy on 3128.
- **`networkpolicy-investigate-api`:** selects the server's `selectorLabels` and allows ingress from `investigate-agent` on the target port.
- **The proxy:** a squid Deployment, Service and ConfigMap with exactly this configuration (CONNECT only, port 443, exact `dstdomain` names; values may add exact names only):
  - `acl SSL_ports port 443`;
  - `acl CONNECT method CONNECT`;
  - `acl registries dstdomain`, listing:
    - `registry.npmjs.org`;
    - `pypi.org` and `files.pythonhosted.org`;
    - `crates.io`, `static.crates.io` and `index.crates.io`;
    - `proxy.golang.org` and `sum.golang.org`;
    - `repo.maven.apache.org` and `repo1.maven.org`;
  - `http_access deny !CONNECT`;
  - `http_access deny CONNECT !SSL_ports`;
  - `http_access allow CONNECT registries`;
  - `http_access deny all`.
- **The proxy's own policy:**
  - ingress from `investigate-test` on 3128;
  - egress to 443 on public addresses, and DNS.
- **RBAC:** the sandbox Role (`rbac/job-sandbox-rbac.yaml`) gains `secrets: create, get, delete` and `networkpolicies: list`.

### Network check (before every dispatch)

1. List the NetworkPolicies in the namespace.
2. Evaluate each `podSelector`'s `matchLabels` and `matchExpressions` (In, NotIn, Exists, DoesNotExist) against the **exact labels** of the agent and test manifests.
3. Refuse with the offending policy's name unless:
   - the agent pod is selected by `…-investigate-agent` only;
   - the test pod is selected by `…-investigate-test` only;
   - `…-investigate-api` exists.

## Steps

**Before starting:**
- this plan is `status: approved`;
- the branch `feat/2156-investigate-run-kind` is cut from current `dev` in a new worktree `AIFactory-wt-2156-impl`;
- the agent bus has been told (the AIFactory release lead works on `dev`).

**Waves** (each step in its own worktree, cut from the integration branch; the lead reviews against this plan, runs the step's check, then commits):
- **A:** steps 1, 2, 3, 8 (parallel);
- **B:** steps 4 and 7 (after step 8, for its labels; step 7 uses a fake API);
- **C:** step 5;
- **D:** step 6;
- then steps 9 to 12 (lead).

1. **Migration, model, store.**
   - **Files:**
     - `WS/database/alembic/versions/20261119_<rev>_investigations.py`;
     - the `Investigation` and `InvestigationLog` classes in `WS/database/models.py`;
     - new `WS/services/investigation_store.py`, with `insert` (raises `SlotTaken` on the unique conflict), `lock(id)` (a context manager doing `SELECT … FOR UPDATE`), `update`, `append_logs`, `read_logs(after, limit)`, `live_runs(limit)`, `purge_logs(older_than)`;
     - tests in `tests/postgres/test_investigations_store.py`.
   - **Tests:**
     - two concurrent inserts for one project → one `SlotTaken`;
     - a row with `pods_gone = false` in `uncertain` still blocks;
     - setting `pods_gone = true` frees the slot;
     - the cursor advances across three reads;
     - `limit` is capped at 500;
     - the purge removes only rows of runs that ended more than 7 days ago;
     - downgrade drops both tables.
   - **Verify:** `apps/backend/.venv/bin/pytest tests/postgres/test_investigations_store.py -v`.
   - **Traps:** follow the revision-chain convention of the newest migration in `versions/`; do not touch `job_states`.
2. **Scrubber and count parsers.**
   - **Files:**
     - new `WS/services/investigation_scrub.py`, with `scrub_lines(lines, *, claude_token, pem_open) -> (lines, pem_open)` and `scrub_text`;
     - new `WS/services/investigation_tests.py`, with `parse(log_text, truncated) -> {passed, failed, skipped} | {parsed: False}`;
     - tests in `tests/test_investigation_scrub.py` and `tests/test_investigation_tests.py`.
   - **Scrubber:** the existing redactor (`agent_kubejob.py:679-684`) plus the added patterns and the PEM state.
   - **Tests:**
     - an AWS key, a JWT, a `ghp_` token, an `sk-ant-` token, a 48-character base64 run, and the literal Claude token are removed;
     - a PEM block split across two calls is removed;
     - ordinary pytest output survives;
     - each of the five formats, both passing and failing;
     - truncated output with no summary → `parsed: false`;
     - an agent-style line "all 50 tests passed" is not parsed.
   - **Verify:** `apps/backend/.venv/bin/pytest tests/test_investigation_scrub.py tests/test_investigation_tests.py -v`.
3. **Source export.**
   - **Files:** new `WS/services/investigation_export.py`, with `export(repo_path, sha, dest, extra_patterns) -> {bytes}` raising `ExportError`; tests in `tests/test_investigation_export.py`.
   - **Behaviour:** as in Decisions, including the fetch through the existing project-repo helper and the 2 GiB cap.
   - **Tests** (a fixture git repository containing `.env`, `id_rsa`, `a/.npmrc`, `terraform.tfstate`, a symlink to `/etc/passwd`, a symlink to `../../x`, and a safe relative symlink):
     - no `.git` in the export;
     - every secret path is gone;
     - both escaping symlinks are gone and the safe one stays;
     - the marker is present;
     - an unknown `sha` → `ExportError`;
     - over the cap (patched to 1 KiB) → `ExportError`;
     - extra patterns can only add.
   - **Verify:** `apps/backend/.venv/bin/pytest tests/test_investigation_export.py -v`.
4. **Manifests, guard, network check** (after step 8).
   - **Files:** new `WS/services/investigation_manifest.py`, with `agent_manifest(run)`, `test_manifest(run, n)`, `secret_manifest(run, job_uid)`, `assert_investigation_manifest(m)` and `network_check(policies, agent_labels, test_labels) -> None | reason`; tests in `tests/test_investigation_manifest.py`.
   - **Tests:**
     - both manifests pass `assert_job_policy` (imported, unchanged) and the guard;
     - with `FACTORY_GCP_CREDS_SECRET` and the OTEL variables set, neither manifest contains them;
     - the env names are exactly the allowlists;
     - there is no `instance` label;
     - the investigation pod label is present;
     - `readOnlyRootFilesystem` is set, and automount is false;
     - the network check:
       - passes on the policies from `helm template` of the default render with `investigations.enabled=true`;
       - fails (naming the policy) on a build policy without the term, on an `extraRules` policy selecting `factory.io/kind: task`, and when `…-investigate-api` is missing;
       - evaluates In, NotIn, Exists and DoesNotExist.
   - **Verify:** `apps/backend/.venv/bin/pytest tests/test_investigation_manifest.py -v`.
5. **Backend lifecycle** (after steps 1 to 4).
   - **Files:** new `WS/services/investigation_backend.py`, holding `dispatch`, `renew_lease`, `stop`, `get`, `logs`, `run_tests`, `append_log`, `put_report`, `lease_status`, `config`, `_terminate`, `reconcile_tick`, and `start_loop()` (a supervised task with backoff on errors); tests in `tests/test_investigation_backend.py`, using a fake Kubernetes client and a clock, with the store on Postgres.
   - **Behaviour:** everything in Decisions, under Dispatch, `run_tests`, Lease and termination, Reconcile and Report.
   - **Tests:**
     - each refusal reason, including limits of 0 or −1 and over the maximum;
     - the order: the Job name is recorded before the create;
     - crash injection: between recording the name and the create → reconcile creates it while the lease is valid, else `failed (dispatch)`; between the create and `running` → found by name and label;
     - two concurrent `run_tests` → one; the 11th → refused;
     - no command → "no tests run";
     - `stop` against a `run_tests` that is mid-create → nothing admitted after `stopping`, and the slot held until both Jobs' pods are gone;
     - `_terminate` doesn't block; pods gone → terminal, with the Secret, credential and export cleaned up;
     - pods remaining for 120 s → `uncertain`, slot held, then a retry succeeds;
     - expiry for a run that was never renewed → `expired` at 60 s;
     - the report's counts come only from `tests`;
     - a restart with `stopping` and `uncertain` rows → resumed;
     - **the build loop keeps its timing:** a stuck investigation does not delay `agent_kubejob`'s 15 s tick (assert that the build loop is never awaited from here).
   - **Verify:** `apps/backend/.venv/bin/pytest tests/test_investigation_backend.py -v`.
6. **Routes, middleware, MCP, switch** (after step 5).
   - **Files:**
     - new `WS/routes/investigations.py`;
     - `WS/auth.py`: the exemption only;
     - the app router mount plus `start_loop` at startup, each only when on;
     - `WS/mcp_stdio/router.py`: the five proxies;
     - the switch parser in `WS/services/investigation_backend.py`;
     - `WS/.env.example`: document `AIFACTORY_INVESTIGATIONS`;
     - tests in `tests/test_investigations_routes.py` and `WS/tests/test_investigations_switch.py`.
   - **Tests, through the full app middleware stack:**
     - with the switch off, every route → 404 and the MCP tools are absent;
     - an invalid switch value raises;
     - a pod route with no token → 401; with another run's token → 403; on an ended run → 403;
     - a caller route with only a run token → 401;
     - `lease` with a run token → 401;
     - viewer and editor roles enforced;
     - the cursor and its limit;
     - `pod/config` returns exactly the dispatched values;
     - `stop` returns within 20 s.
   - **Verify:** `apps/backend/.venv/bin/pytest tests/test_investigations_routes.py apps/web-server/server/tests/test_investigations_switch.py -v`.
7. **Agent runtime** (after step 8; uses a fake API).
   - **Files:**
     - new `BE/agents/investigate.py` (the entry point `python -m agents.investigate`);
     - `create_investigate_client` in new `BE/core/investigate_client.py` (`create_client` untouched);
     - the hook in new `BE/agents/investigate_guard.py`;
     - tests in `apps/backend/tests/test_investigate_agent.py`.
   - **Tests:**
     - **The hook:**
       - denies Write, Bash and an unknown MCP tool;
       - denies `Read /proc/self/environ`, `Read /src/../etc/passwd`, a `/src` symlink resolving outside, `Glob pattern "/**"`, `Glob pattern "../*"`, and `Grep path=/tmp`;
       - allows `Read /src/README.md` and `Grep path=/src/a`.
     - **The options:** exactly as in Decisions.
     - **Token accounting** with fixtures: one id repeated three times counts once; cached input counted; cumulative output deltas; a budget crossed mid-stream → `interrupt()` called before the session ends, with exit reason `token budget`.
     - **The watchdog:** exits on `ok:false`, and after three failed reads.
     - **Config:** the agent uses the values from `pod/config`.
     - **The log:** contains no file contents.
   - **Verify:** `apps/backend/.venv/bin/pytest apps/backend/tests/test_investigate_agent.py -v`.
8. **Helm chart.**
   - **Files:**
     - `charts/aifactory/templates/networkpolicy-tasks.yaml`: the term, only when on;
     - new `templates/networkpolicy-investigate-{agent,test,api,proxy}.yaml`;
     - new `templates/{deployment,service,configmap}-investigate-proxy.yaml`;
     - `templates/rbac/job-sandbox-rbac.yaml`: the rules, only when on;
     - `values.yaml`: `investigations.enabled: false`, `claudeApiCidrs`, `proxy.image`, `proxy.extraDomains`;
     - tests in `tests/helm/test_p4_helm.py`.
   - **Tests:**
     - off → `helm template` output byte-identical to `dev` (stored golden file);
     - on → the build policy has the term with the rest unchanged;
     - the four policies have exactly the stated rules;
     - the proxy pods have no `instance` label;
     - the squid configuration contains the stated lines;
     - `extraDomains` entries with a leading dot or a regex character are refused by a template `fail`.
   - **Verify:** `apps/backend/.venv/bin/pytest tests/helm/test_p4_helm.py -v`.
9. **Suites (lead):**

   ```bash
   apps/backend/.venv/bin/pytest tests/ -m "not slow"
   npm run test:backend
   python apps/backend/core/job_dispatch.py   # vendored self-test, unchanged
   git diff dev -- apps/backend/core/job_dispatch.py   # empty
   ```

   Every suite must show 0 failures, and the `git diff` must be empty.
10. **Reviews (lead):** a fresh Opus agent and Codex, each given this plan and `git diff dev`, read-only. Findings are fixed with tests that fail on the old code; step 9 is rerun afterwards.
11. **PR to `dev`** (lead), linking intent, spec and plan, and saying which steps Codex and the coder did. The switch is off by default, so merging changes no behaviour. The release lead is told on the bus before it merges.
12. **Live verification (owner's OK; after a release, with the switch on in factory-gitops):**
    - **The pod:** env shows only the allowlist; `/src` is read-only with no `.git`; `touch /src/x` fails; `/var/run/secrets` is absent.
    - **Egress from the agent pod:** `curl https://example.com` is refused, and the Claude API answers.
    - **A hostile test command** in a fixture repository, which must:
      - print its env and find no token;
      - fail to reach the Claude and AIFactory APIs;
      - fail to write the report;
      - fail to upload to example.com;
      - succeed at an npm install.
    - **The proxy:** refuses plain `http://registry.npmjs.org/`, `registry.npmjs.org.evil.example` and port 8443, and lets downloads from npm, PyPI, crates, Go and Maven through.
    - **A real session:** a real SDK session completes on the hardened manifest.
    - **Stop:** pods gone within 20 s, and the slot is free.
    - **Lease:** killing the caller gives `expired` within 70 s.

    Results go into the PR, or into a follow-up issue for each failure.

## Tests

On the integration branch, with 0 failures:

```bash
apps/backend/.venv/bin/pytest tests/ -m "not slow"
npm run test:backend
python apps/backend/core/job_dispatch.py
apps/backend/.venv/bin/pytest tests/helm/test_p4_helm.py
```

## Rollback

- **Turn it off:** set `AIFACTORY_INVESTIGATIONS=off` and `investigations.enabled: false` in factory-gitops, and Argo syncs. The routes, the loop, the policies and the proxy go away; build runs never depended on them.
- **Revert:** the commits are additive. The migration's downgrade drops both tables. The only shared-file edits are the build policy term (rendered only when on), the auth exemption (pod-route pattern only), and the RBAC rules (only when on).
