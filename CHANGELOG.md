## [Unreleased]


## 3.6.28 - 2026-07-10

### Fixed

- **Release pipeline can pull the private `tfactory-runner-nix` base image.** `release.yml` logged into GHCR with `GITHUB_TOKEN`, which can't read the private, repo-unlinked `ghcr.io/olafkfreund/tfactory-runner-nix` package the image build `COPY`s from (`Dockerfile:255`) — so the first version-bumped release (`v3.6.27`) 403'd on the baked-Nix-store image step. Now uses `GHCR_PAT || GITHUB_TOKEN`, mirroring `deploy.yml`. This release also regenerates the full release artifacts (image, SBOM, cosign signature) that `v3.6.27` could not produce.


## 3.6.27 - 2026-07-10

### Removed

- **Repo-wide over-engineering cleanup (ponytail-audit) — ~10.5k lines, 7 deps (#797).** Deleted verified-dead code: backend `runners/github/` review-intelligence modules (trust, learning, confidence, duplicates, multi_repo, cleanup, onboarding, memory_integration), the `bmad/` subagent framework + context_shard/agent_adapter/session_spawner, and 18 unraised exception classes; web-server dead `verify_token`/`bearer_scheme`; frontend dead components/shims (DevTools\*, DisplaySettings, TerminalDropdown, AddWorkspaceModal, release-store, shell-escape, `components/` barrel). Removed unused dependencies `gitpython`, `aiofiles`, `python-dotenv`, `zod`, `react-resizable-panels`, `uuid`, `@types/uuid`. Test-covered modules (qa/providers, gemini shims, output_validator, build_cached_system_blocks) and live CLI shims were kept; the required backend gate (ruff + pytest) is green.

### Changed

- **Behavior-preserving refactors to stdlib/native (#797).** Frontend `use-toast` hand-rolled reducer → zustand, `uuid` → `crypto.randomUUID()`, `hookProxyFactory` → native `Proxy`, inlined single-consumer wrappers; web-server lazy singletons → `@functools.cache`, extracted a `_json_store` helper, and deduped `_slugify` (→ `server/utils/slug.py`) and profiles-path resolution.

### Added

- **Safety-net commit so agent work is never lost (#611 g, RFC-0008 §3.2).** The coding agent commits its own work, but if it finished with files written and uncommitted, a later post-session bookkeeping step that aborts (e.g. LLM insight extraction) — or a worktree teardown — could lose them (the demo nearly lost `app/main.py`). New defensive `agents/utils.commit_uncommitted_changes()` stages + commits any leftover changes in the worktree; `agents/session.post_session_processing` calls it right after computing the agent's own commit count and **before** the abortable bookkeeping. It only preserves files (does not count toward `new_commits`/completion, so an unconfirmed subtask is still retried with its work safely committed) and never raises. 3 unit tests over a real temp repo; session/agent suites green.

- **qa_fixer auto-repairs mechanical defects instead of escalating (#611 e, RFC-0008 §3.2).** The taskboard demo escalated to human review on deterministic, fixable problems. `prompts/qa_fixer.md` (CRITICAL ACTIONS + a PHASE 3 repair table) now directs the fixer to repair mechanical/structural defects itself — create a missing runnable entrypoint, add a used-but-undeclared dependency to the manifest (`httpx`, `pytest`, …), create a missing config/asset, fix a wrong import path / missing `__init__.py` — and to escalate only for genuine product/human-judgement calls.
- **Provider bounded-retry + failover in the QA loop (#611 c, RFC-0008 §3.2).** The taskboard demo escalated to human review after a hung gemini CLI timed out 300s×3 on the *same* provider — there was no failover and no overall time budget. New pure `core/provider_failover.py`: `failover_chain()` (primary first, then a default `claude→codex→antigravity` chain, overridable via `AIFACTORY_PROVIDER_FAILOVER`), `next_provider()`, `should_failover()` (the danger-zone guard — returns False for auth/model-not-found/quota faults a different provider can't fix, so those still escalate immediately), and `DeadlineBudget` (monotonic, `AIFACTORY_PROVIDER_DEADLINE_S`, default 900s). Wired into `qa/loop.py`: when a provider stalls (`MAX_CONSECUTIVE_ERRORS` without progress) the loop now rotates to the next provider in the chain and resets the counter instead of escalating — bounded by the deadline and short-circuited on config-fault errors. 22 unit tests on the decision core; existing QA-loop suites green. Wiring the same failover into the coder/build loop is a follow-up.

- **QA smoke-boot the artifact, not just pytest (#611 d, RFC-0008 §3.2).** The taskboard demo passed pytest against an app assembled only in `conftest` while there was no runnable entrypoint, and escalated to human review. `prompts/qa_reviewer.md` gains a mandatory **PHASE 2.5: SMOKE-BOOT** (plus a CRITICAL ACTIONS rule): start the artifact from its real entrypoint, probe `/health` (2xx) on the running process, and exercise at least one acceptance criterion over HTTP — not via in-process `TestClient`/`conftest`. No runnable entrypoint / failed boot / `/health` never green ⇒ **REJECT**, not approve. A green pytest run over an unbootable artifact is a FAIL.
- **Coder prompts must declare test dependencies (#611 f, RFC-0008 §3.2).** The taskboard demo shipped an unrunnable artifact because the generated `requirements.txt` omitted `httpx` — which FastAPI's `TestClient` needs but `fastapi` does not pull in. `prompts/coder.md` (CRITICAL ACTIONS) and `prompts/coder_story_enhanced.md` (STEP 6) now require every imported package — runtime **and** test (`httpx`, `pytest`, …) — to be declared in the project's dependency manifest so the artifact installs and the suite runs on a clean machine, calling out the `httpx`/`TestClient` trap by name. Prevention half of the missing-dep failure (the qa_fixer mechanical cure is the (e) follow-up).
- **Build-time auth pre-flight + opt-in headless API-key preference (#611 a+b, RFC-0008 §3.2).** The 2026-06-18 taskboard demo built nothing because the `CLAUDE_CODE_OAUTH_TOKEN` was *present but expired* — the build ran empty and escalated to human review. New `core/auth_preflight.py` runs a live, generation-free credential probe (`GET /v1/models`) just before a build and classifies it `ok` / `auth_failed` / `inconclusive`; wired into `cli/build_commands.handle_build_command` after `validate_environment`. Three modes via `AIFACTORY_AUTH_PREFLIGHT` — `off`, `warn` (default: probe + log, never blocks), `enforce` (abort with a named error on a definitive 401/403). Default `warn` so enabling it can never false-abort a valid build; `inconclusive` (network/5xx/SSL) never blocks in any mode. Prefers `ANTHROPIC_API_KEY` (verified `x-api-key` probe) and otherwise probes the OAuth token via `authorization: Bearer` + the oauth beta header. (b) adds an opt-in `AIFACTORY_HEADLESS_PREFER_API_KEY` so headless/benchmark runs can prefer the non-expiring `ANTHROPIC_API_KEY` over the interactive OAuth token — effective only when `AIFACTORY_ALLOW_API_KEY` is already set, so the anti-silent-billing default holds. Slice 1 of #611 (probes anthropic, the demo cause + pinned planner; gemini/openai probes are a follow-up).
- **Real OS sandbox around agent bash, enforced on-cluster (#363, security epic #318 — last open child).** The agent runs under `bypassPermissions` with a soft command allowlist; this adds the missing OS boundary (AC1). `server/services/sandbox.py`'s `fs` mode now runs **unprivileged in-pod** (k3d included): the privilege-requiring `--unshare-pid` + fresh `/proc` (which silently disabled bwrap on k3d — "can't mount /proc") is now **opt-in** via `AIFACTORY_AGENT_SANDBOX_PIDNS`, and the default keeps the host PID namespace with a read-only `/proc` while still binding the worktree read-write and everything else read-only. Enabled by default in the chart (`sandbox.agent.mode: "fs"` → `AIFACTORY_AGENT_SANDBOX`); pure passthrough if bwrap is absent. The two previously-skipped sandbox-escape corpus tests (write-outside-worktree, read-host-secret) are now live assertions — proven rejecting both inside the live k3d pod and in CI (CI installs `bubblewrap` and relaxes Ubuntu 24.04's unprivileged-userns AppArmor restriction so the tests exercise a real boundary). The command hook + env-scrub + egress policy remain as defense-in-depth, not the perimeter. Closes the `bypassPermissions`-as-sole-control finding (H1).
- **Rewrite mode for language migrations (RFC-0010, Factory#105).** When the signed contract carries `change_mode == "migration"`, AIFactory generates in the **target** language against the legacy source as a read-only reference oracle instead of editing it in place. `core/migration_mapper.py`: `resolve_generation_language()` (target wins over repo detection), `mount_oracle()` (copies the legacy source read-only under `.aifactory/oracle/`, excludes `.git`/`.aifactory`), `scaffold_target()` (coexisting target crate/dir + per-module stubs + a protocol-conformant `parity_harness` stub TFactory's equivalence lane invokes), `module_briefs()` (per-module source→target briefs carrying the legacy source), and `prepare_migration_workspace()` which also drops a `MIGRATION_BRIEF.md` at the worktree root (rewrite rules + source→target map) so the coder generates the new language and never edits the oracle. Wired into `cli/build_commands.py` after worktree setup; no-op + never fatal for non-migration builds.
- **In-cluster ephemeral verification sandbox with worktree co-mount (#591, #592, #594; RFC-0005/0006).** Trailing build gates (lint/test/build) can now run in a per-task **ephemeral container** instead of the host. `agents/gate_runner.py` routes gates via `_select_runner()` to one of three backends — host subprocess (default), a vendored podman/docker `factory-sandbox` (`core/factory_sandbox.py`), or an in-cluster **Kubernetes Job** (`core/kube_sandbox.py`) — selected by `AIFACTORY_SANDBOX_GATES` (default off), `AIFACTORY_SANDBOX_BACKEND` (`docker`|`kubejob`), and `AIFACTORY_SANDBOX_IMAGE`. The kubejob backend exists because the AIFactory pod has no container runtime: each gate runs as a one-shot Job (`backoffLimit: 0`, `ttlSecondsAfterFinished: 120`, `restartPolicy: Never`, `automountServiceAccountToken: false`, `ghcr-pull`, CPU/mem limits) under a least-privilege `aifactory-sandbox` ServiceAccount + Role (create/get/list/watch/delete jobs; read pod logs). The Job **co-mounts the task worktree** at `/work` via the `aifactory-data` PVC `subPath`, derived by stripping the data root (`AIFACTORY_DATA_ROOT`, default `/home/nonroot/.aifactory`; PVC override `AIFACTORY_SANDBOX_REPO_PVC`, default `aifactory-data`) from the gate cwd — so code-reading gates run against real files, not just toolchain checks; a cwd outside the data root falls back to a toolchain-only Job rather than mounting the wrong path. Validated live: a gate Job ran `go test -v ./...` green against a real co-mounted worktree, then self-GC'd. **Opt-in/default-off** — no behavior change unless the flags are set. Honest caveats: the kubejob backend takes **one** toolchain image per call (no per-language multiplexing yet), reports a synthetic 0/1 rather than the container's real exit code, and the PVC co-mount relies on a single-node / `local-path` cluster.
- **Multi-language build toolchains in the coder sandbox (#586, #587).** The coder runs in the AIFactory image but it shipped only `g++`/Python/Node, so specs in Rust/Go/Java/CMake could be written but not built or tested (`cannot execute cargo`). The `Dockerfile` now `apk add`s `go-1.25`, `rust-1.90`, `maven-3.9`, `openjdk-21-default-jdk` (the JDK with `javac`, not the runtime-only `-default-jvm`), `cmake`, and `build-base`, sets `JAVA_HOME`/`PATH`, and runs a fail-fast build-time verification (`go version && cargo --version && mvn -v && javac -version && cmake --version && g++ --version`) so a bad PATH fails the build instead of the agent at runtime. Image grows ~1.5 GB.
- **Planner honors the spec's language over the repo's (#585).** A new `PHASE 0.0` rule in `prompts/planner.md` derives the target language/stack from the spec (acceptance-criteria build/test commands like `cargo test` / `mvn test` / `ctest` are authoritative); it matches the repo's conventions but never its language. On a language mismatch it scaffolds a self-contained sub-project in the spec's language (own manifest + explicit `files_to_create`) or HALTs with a `LANGUAGE CONFLICT` blocker — closing the silent failure where a Rust/Java/C++ spec dropped into a Go repo produced Go and was marked complete. The coder prompt (`prompts/coder_story_enhanced.md`) mirrors the guard.
- **Per-worker billing mode on the usage block (#96).** Each worker's provider is classified into a billing mode — `api` / `cloud` (metered, real dollars) or `subscription` / `local` (no dollar spend; `cost_usd` is notional/zero) — inferred zero-config from how the run is authenticated (provider + API-key env presence + Ollama endpoint). Carried on `usage.workers[].billing_mode` and `usage.by_provider{}.billing_mode` (with a `duration_ms` rollup). This lets CFactory show real cost only for metered work and tokens + time for subscription/local, instead of a misleading notional dollar figure. New `services/billing.py` is pure and unit-tested; additive — `additionalProperties: true` validates the field.
- **Subtask graph + timing fields on the task-detail API (#577).** The subtasks in `GET /api/tasks/{id}` now expose `depends_on` (dependency edges), `service`, `started_at`, and `completed_at`. These were already tracked on the internal `implementation_plan` subtask but were dropped at the API boundary; they are now serialized so CFactory can render the live code-stage execution diagram. Additive and optional — plans without them serialize as `[]` / `null` and the diagram degrades gracefully.
- **RFC-0001a evidence gate on the completion event (#575).** A build may only claim a success status (`completed`/`passed`/`verified`/`succeeded`) if it carries proof it ran. A build event with `usage.total_tokens` at 0 is downgraded to `failed` with `halt_reason: "no_evidence: build emitted 0 tokens (did not run)"`, and the event gains an `evidence{proof_kind, total_tokens, cost_usd}` block. This closes the silent-failure mode where an expired provider credential produced a stub plan that finished in seconds and was historically reported green. Scoped to build events that carry usage; never touches the legitimate `failed` path or non-build events.
- **Evidence / red-flags block in the coder and qa_fixer prompts (#576).** Both agent prompts gained a "Red flags — STOP, do not claim success" section calling out the silent-failure modes that have shipped dead builds as green: reporting a phase complete with no real change, a failed tool/credential (e.g. `401`), every generated test failing identically, or skipping a step because it "probably works". The closing rule — "Evidence ends the task" — requires the diff plus passing checks over it before the work is considered done.
- **Per-worker observability for parallel builds (#45 P1).** Parallel multi-provider builds already existed; this adds the instrumentation. `token_usage.json` now carries a per-worker `workers` map (input/output tokens, cost, duration per worker) with the scalar aggregate preserved unchanged, and the completion event grows to **v1.3** with an additive `usage.workers[]` / `usage.by_provider` / `usage.by_model` block (#566). The web-server emits OpenTelemetry metrics — `gen_ai.input_tokens`, `gen_ai.output_tokens`, `gen_ai.cost_usd`, `worker.duration_ms` — tagged `{provider, model, phase}` with bounded cardinality (never `task_id`), a no-op when OTel is disabled; the completion-event `traceparent` was fixed to link to the real active span (#567). Live `phase:"worker"` sub-events fire as each worker finishes (#568) and throttled (~10s) `phase:"worker_progress"` heartbeats keep long workers visibly ticking (#570). A soft, **observe-only** budget alert adds `usage.budget{limit,spent,exceeded}` and a `budget.exceeded` OTel counter — it reports but **never aborts a build** (#569). Emitted from the web-server because agent subprocesses deliberately don't export.
- **Auth hardening (#558).** Startup CORS guard refuses a wildcard origin combined with credentials; the WebSocket terminal token is now accepted via the `Authorization` header (query-param still works but logs a deprecation warning); the legacy wildcard `API_TOKEN` now emits a loud deprecation warning (scoped `acw_` keys are the replacement).
- **Actions security pass + CI gates (#557, for #553/#554).** Fixed a GitHub Actions script-injection — an untrusted issue title is now bound through `env:` instead of interpolated into a shell step — and hardened `copilot-pr-review.yml`: the spoofable `[bot]`-suffix actor trust (the CVE-2025-66032 pattern) is replaced with an `author_association` membership gate, and `--allow-all-tools` is removed from the path that runs over untrusted PR content. Added non-blocking frontend `vitest` and `mypy` jobs with broadened triggers (report first, promote to required once the signal is clean).
- **`routes/tasks.py` decomposed (#556).** Reduced from 5,346 LOC to ~4,000 by extracting cohesive sub-routers — worktree tools (#559), plan-approval (#560), PR-creation (#563), inbox (#565) — with backward-compatible re-exports and byte-identical routes (no behavior change).
- Mission Control — a full-page three-pane task workspace (plan & subtasks · live activity + embedded Live Console · output tabs for preview / files / review), opened from the task-detail header (⤢). Reuses the existing task-detail data layer; collapses back to the modal; pane sizes persist (#311).
- Live app **Preview** pane in Mission Control — iframe preview with an address bar and one-click dev-server port presets (#311).
- Portal UX pass: skeleton loaders on the Kanban board and logs, a live progress timeline with elapsed timer + animated working state, a theme-aware terminal that follows light/dark + Gruvbox/shadcn, a "waiting for you" beacon on tasks in human review, and animated streaming log entries (#311).
- `APP_RMUX_ENABLED` web-server setting to enable the rmux Live Agent Console for local dev (honored alongside the `AIFACTORY_RMUX_ENABLED` process env var) (#311).
- Multi-agent **Live Console grid** at `/console/:projectId` — every active agent's console for a project streamed at once in a responsive grid (live "N active" count, per-tile fullscreen link), the multi-agent counterpart to the single `/console/:projectId/:specId` page. Reachable via "All consoles" on the single console header and an "All consoles ↗" badge in the task detail (#314).

### Fixed

- **Successful parallel builds no longer falsely reported `failed` (#588).** After each subtask merged, `mark_complete` loaded the plan from the worktree `spec_dir/implementation_plan.json` and swallowed any exception — so when that worktree path held no plan, completion was silently lost, the canonical plan stayed at 0 completed (observed: 7 subtasks / 0 done with passing `go test`/`cargo test`), the finalize step saw an empty plan ("No implementation subtasks yet"), and the coding agent exited non-zero → `final_status=failed`. Extracted `record_subtask_completion()` (`agents/utils.py`) which falls back to the canonical source plan and returns False (logged, not swallowed) when no plan is found. 4 new unit tests; 24 existing parallel tests green.
- **Pin `fastapi==0.136.3` / `starlette==1.3.1` (#584).** An image rebuild pulled `fastapi 0.137.0` (unpinned at `>=0.109.0`), which broke route introspection — `prometheus-fastapi-instrumentator.get_route_name()` hit an `_IncludedRouter` with no `.path` and 500'd every `/api/*` route (only `/api/health` survived), taking down the cockpit's polling and the PARR conductor. Same regression + pin TFactory shipped.
- **`blocked` subtask status no longer 500s the task list (#583).** A coder can mark a subtask `blocked`; the `Subtask.status` Literal only allowed `pending/in_progress/completed/failed`, so one blocked subtask raised a `ValidationError` in `load_spec_metadata` and 500'd the entire `GET /api/projects/{pid}/tasks` list — blinding the cockpit for every task. Added `blocked` to the Literal.
- Defined several portal CSS classes (`task-running-pulse`, `column-*` accents, `column-count-badge`, `drop-zone-highlight`, `progress-working`) that were referenced by components but never defined, so the intended card pulses, column accents, and progress animation now render (#311).
- Terminal and scrollbars no longer hardcode a dark palette; both follow the active theme (#311).
- rmux Live Agent Console now actually streams. Two bugs blocked it: (1) the pane FIFO defaulted to `/var/run/aifactory/panes`, which isn't writable on non-container hosts — it now resolves a writable default (`AIFACTORY_RMUX_PANES_DIR` → data dir `panes/`); (2) the agent already runs under agent_service's PTY, so rmux re-spawning it would double-run the agent — the integration now registers a FIFO-only "passive" session and tees the agent's existing stdout/stderr into it (`feed_if_enabled`), which the WS bridge streams read-only. Attach/send-keys remains for true rmux sessions.
- Create New Task dialog used a hardcoded blue (`bg-[hsl(204,80%,16%)]`) that clashed with the active theme; switched to the `bg-card` token (#312).
- Shared `/console/...` deep links bounced to the board on a cold load: routing fired before the async auth check resolved (`isAuthenticated=false` → `/login` → `/`). Routing now waits for the first auth check, so single and grid console links survive a cold load. The "Copy console URL" badge's hardcoded slate colors were also moved to the `info` theme token (#314).

### Documentation

- New concept page for the Mission Control workspace; rmux Live Console docs updated with the `APP_RMUX_ENABLED` setting; roadmap "Recently shipped" updated (#311).

## 3.6.26 - 2026-06-11

### Added

- **Opt-in direct-API-key auth via `AIFACTORY_ALLOW_API_KEY`.** AIFactory is OAuth-only by default (Claude subscription): any `ANTHROPIC_API_KEY` in the environment is scrubbed from agents so it can never silently bill the Anthropic API. Operators whose intended billing model *is* a direct API key (no subscription) now set `AIFACTORY_ALLOW_API_KEY=1` — then `ANTHROPIC_API_KEY` is accepted as an auth token (after OAuth), passed through to agents, and no longer scrubbed. Default off preserves the billing-safe behavior; the flag is the single switch (`core.auth.api_key_auth_enabled()`) honored by token resolution, the agent env scrub, and the SDK passthrough.

## 3.6.25 - 2026-06-11

### Fixed

- Trusted-plan tasks now carry their signed Task Contract to TFactory so it tests the **declared** ACs instead of inferring (#71 Phase 3). `ingest_trusted_plan` installed the contract as `implementation_plan.json`, but the executor rewrites that file into AIFactory's runtime format during the build (adding `planStatus`/`status`/`reviewReason`, dropping the contract's `tfactory`/`contract_version`/`approval` blocks) — so by the time the AIFactory→TFactory handoff fires on completion, the RFC-0002 metadata (incl. the `tfactory` test profile: lanes/frameworks/`ac_to_code_map`) was gone and `load_task_contract` returned `{}`. Fix: ingest now stashes the full signed contract in the build-safe `context/task_contract.json`, and `tfactory_client.load_task_contract` reads that first (falling back to `implementation_plan.json` for older specs). Verified end-to-end: a contract-carrying handoff makes TFactory persist the contract and the planner emit a test plan matching the declared profile (e.g. a `browser`/`playwright` lane that inference would never produce for an API service).

## 3.6.24 - 2026-06-10

### Fixed

- Sequential auto-merges no longer dead-end on a stale branch (#71): when an earlier auto-merge advances `main`, a later PR is "behind" and `gh pr merge` fails. `merge_pr` now runs `gh pr update-branch` once and retries — resolving the common case where tasks touch different files. A true line-level conflict (update-branch fails) is still left for a human; never force-merged.

## 3.6.23 - 2026-06-10

### Fixed

- Auto-feedback loop race (#71 Phase B): the fix loop ran the QA-fixer fire-and-forget (`apply_correction` → `_default_fixer` schedules a background task and returns), so it pushed + re-reviewed the **un-fixed** code and re-read the stale `changes_requested` verdict. `_fix_fn` now runs the fixer **to completion** (awaits `_run_fixer_bg` via a `fixer_fn`) before pushing, and waits for the re-review's `reviewedAt` to advance before returning so the loop reads the fresh verdict. Push failure fails the cycle.

## 3.6.22 - 2026-06-10

### Added

- PR-endgame **auto-feedback loop** (#71 Phase B): when the pre-merge reviewer requests changes, the findings are routed to the QA-fixer, the fix is pushed to the PR branch, and the PR is re-reviewed — bounded (≤2 cycles), merging only once it passes; after the budget it hands to a human (`needs_human_after_fixes`). Also verified the `aifactory` reviewer **live** (no Copilot credits — engine verdict `ready_to_merge`) and aligned `verdict_from_review_result` to the engine's `MergeVerdict` vocabulary (`ready_to_merge`⇒approve; `merge_with_changes`/`needs_revision`/`blocked`⇒changes; non-empty `blockers`⇒changes). 25 tests.

## 3.6.21 - 2026-06-10

### Added

- The pre-merge reviewer is now **configurable** (`AIFACTORY_PR_REVIEWER` = `aifactory` | `copilot` | `any`, project setting + env, default `aifactory`) so the merge gate no longer depends on GitHub Copilot **code review** credits (#71 Phase A). With `aifactory`, AIFactory's own review engine reviews the PR with the project's provider (Claude/Ollama) and the merge is gated on **its verdict** (read from `review_{pr}.json`) — GitHub forbids self-approving the PR AIFactory opened, so the engine verdict is the gate, not a GitHub review event. A "Pre-merge reviewer" selector is in Project Settings → General. `copilot`/`any` modes keep gating on GitHub review state. On changes-requested the PR is human-stopped (the auto-feedback fix loop is the next increment).

## 3.6.20 - 2026-06-10

### Added

- PR endgame is now toggleable **per project from the Settings UI** (Project Settings → General → *Auto-open a PR* / *Auto-merge after Copilot approves*) — persisted to the project's `.aifactory/.env` as `AIFACTORY_AUTO_PR`/`AIFACTORY_AUTO_MERGE`; the per-project setting wins over the global env default (#71). `pr_endgame` resolves the flags per-project (the completion hook runs in the web-server process, so it reads the project's `.env`, not just `os.environ`).

### Changed

- Auto-merge now **requires GitHub Copilot's review**: it merges only after Copilot posts an `APPROVED` review (`require_copilot` default). Copilot `CHANGES_REQUESTED`, any reviewer's changes-requested, or no-Copilot-review-yet → human-stop (PR left open); a human-only approval does not satisfy the gate. `read_review_verdict` is now Copilot-aware (`ReviewState`). Copilot's findings are never bypassed.

## 3.6.19 - 2026-06-10

### Documentation

- Added `guides/pr-endgame.md` documenting the PR-endgame feature (#71 Phase 4): the `AIFACTORY_AUTO_PR` / `AIFACTORY_AUTO_MERGE` opt-in flags (default OFF), the create-PR → Copilot-review → merge → re-test flow, the human-stop safety properties, prerequisites, the known limitation (GitHub Copilot *code review* must be enabled on the repo for the review→auto-merge leg to run), and the single-replica deploy-downtime note.

## 3.6.18 - 2026-06-10

### Fixed

- Completion side-effects (TFactory auto-handoff #496, PR endgame #71 Phase 4) now fire on `COMPLETED` regardless of `emit_events`. They were gated on `if emit_events and phase_enum == COMPLETED`, but the real terminal path (`_monitor_process`) calls `_update_plan_status(emit_events=False)` (Issue #14 suppresses WS double-emission, not side-effects) — so **neither ever fired on a real completion** (no task had ever written `tfactory_handoff.json` or a PR-endgame marker). Now gated on `COMPLETED` only, wrapped in a fire-once `.terminal_side_effects_done` marker so the two completion call paths can't double-fire (no duplicate PRs/handoffs).

## 3.6.17 - 2026-06-10

### Fixed

- PR endgame: `create_pr` now runs `gh auth setup-git` before pushing (#71 Phase 4). In the deployed pod a raw `git push` failed with "could not read Username for https://github.com" despite gh being authenticated via `GITHUB_TOKEN`, so an auto-PR never opened. Configuring gh as git's credential helper (idempotent, best-effort) fixes the push.

## 3.6.16 - 2026-06-10

### Fixed

- `copy_spec_to_worktree` is now idempotent — a resume/concurrent `setup_workspace` no longer crashes the build with `FileExistsError` right after worktree creation (#71 follow-up). It guarded with `exists()`+`rmtree` but then called `shutil.copytree` without `dirs_exist_ok`, so when the target spec dir reappeared between the check and the copy, `copytree`'s `makedirs` raised and the trusted-plan build exited 1 before any code was written. The Phase 0 `spec.md` fix unmasked this (trusted tasks now reach `setup_workspace` instead of dying at `find_spec`). Fixed with `dirs_exist_ok=True`.

## 3.6.15 - 2026-06-10

### Added

- PR endgame: on a clean build, auto-open a PR → request a Copilot review → merge on approval → re-test (#71 Phase 4). The new `server/services/pr_endgame.py` orchestrates the finish of the closed PARR loop: when `AIFACTORY_AUTO_PR` is set, a `COMPLETED` build opens a PR from the worktree branch, requests a GitHub Copilot review, and watches (bounded) for the verdict; on `APPROVED` and only when `AIFACTORY_AUTO_MERGE` is set, it merges and re-runs TFactory against the result. `CHANGES_REQUESTED`, a review timeout, or a merge conflict is a **human-stop** — the PR is left open, nothing is force-merged. Both flags default OFF (inert until enabled). Every git/gh call is behind an injectable runner (16 unit tests, no network). Wired into the agent_service completion hook (best-effort, never blocks completion).

## 3.6.14 - 2026-06-10

### Changed

- AIFactory→TFactory handoff now carries the signed Task Contract so TFactory tests the DECLARED acceptance criteria instead of inferring (#71 Phase 3). The auto-handoff sent only `{project_id, spec_id, spec_text}` to `/api/specs/ingest`, discarding PFactory's `tfactory` block (lanes/frameworks/`ac_to_code_map`). `build_ingest_payload` now attaches the full contract (the installed `implementation_plan.json`) as `contract` when it carries RFC-0002 markers; TFactory persists it to `context/task_contract.json` and uses it as the authoritative test profile. Create-and-run plans (no markers) attach nothing → TFactory still infers (backward compatible). Requires the paired TFactory change (≥ v0.9.2).

## 3.6.13 - 2026-06-10

### Fixed

- Trusted-plan ingest now synthesizes `spec.md` so the build can actually code (#483). A signed plan ingested via `/api/tasks/from-plan` installed `implementation_plan.json` and marked the review approved, but never wrote `spec.md` — and the executor's spec resolution (`cli.utils.find_spec`) plus `validate_environment` both require it. So `run.py` couldn't find the spec, dumped the AVAILABLE SPECS banner, marked planning "failed", and coding never started (the plan installed but never coded — observed on PARR task 009). `ingest_trusted_plan` now renders a minimal `spec.md` from the contract (feature, acceptance criteria, phase/subtask outline); `implementation_plan.json` remains authoritative. This completes the trusted-plan keystone end-to-end.

## 3.6.12 - 2026-06-10

### Fixed

- Trusted-plan handoff: per-subtask file footprints are now OPTIONAL (#517). AIFactory's `from-plan` completeness check hard-required `files_to_create`/`files_to_modify`, which PFactory can't know pre-code — so every signed PFactory contract was rejected and silently fell back to full re-planning, discarding the contract (incl. the TFactory test plan). Footprints present → parallel waves; absent → serial. This is what lets PFactory actually save AIFactory planning time.

## 3.6.11 - 2026-06-10

### Fixed

- AIFactory→TFactory auto-handoff now works end-to-end (#517). Beyond the endpoint/auth/project-resolution fixes, `build_ingest_payload` now normalizes acceptance criteria into a parseable `## Acceptance Criteria` section (from requirements / the spec's Success-Criteria bullets), so TFactory's spec parser no longer 400s. With the paired TFactory fix (ingest resolves the project by id-or-name), a completed AIFactory task hands off to TFactory and creates a test task.

## 3.6.10 - 2026-06-10

### Added

- The kanban board now **auto-surfaces newly-started tasks** without a manual browser reload. A task created outside the tab — via the API or a PFactory→AIFactory handoff — previously didn't appear until you reloaded (the WS `task:*` handler only updated existing cards). The `/ws/events` handler now refetches the selected project's tasks when a `task:*` event references a task not yet in the store (scoped to the selected project; fires once per new task) (#516).

### Fixed

- API Tokens settings: revoke now uses an in-app confirm dialog instead of the native `window.confirm`, and the token list shows last-used (#479).

## 3.6.9 - 2026-06-09

### Fixed

- Agent bash no longer breaks under the OS sandbox on k3d/Kind. v3.6.8 installed `bubblewrap`, but the SDK's bwrap sandbox **cannot mount `/proc`** when the node is itself a container (k3d/Kind) — even with `CAP_SYS_ADMIN` / `procMount=Unmasked` — so every agent `Bash` command failed (`bwrap: Can't mount proc … Operation not permitted`). The bash sandbox is now gated behind **`AIFACTORY_BASH_SANDBOX`** (default `true`, preserving behaviour where bwrap works). Set it `false` on runtimes that can't host bwrap: bash works and the "WITHOUT sandboxing" warning is gone — isolation rests on the K8s pod boundary (non-root, zero-caps, workspace-restricted FS) + the command allowlist until a gVisor-capable runtime lands. Tracked as the real syscall-sandbox fix on #363.

## 3.6.8 - 2026-06-09

### Security

- Agent command sandbox now actually engages. The Chainguard runtime image omitted `bubblewrap` (`bwrap`) and `socat`, so the Claude Agent SDK logged *"Sandbox disabled: … bubblewrap (bwrap) not installed"* and ran agent bash commands with **no** filesystem/network enforcement — unacceptable for enterprise deployments. Both are now installed in the runtime `apk` layer. Verified on the cluster: the node allows unprivileged user namespaces, so `bwrap` creates a real sandbox (not just silencing the warning) (#363).

## 3.6.7 - 2026-06-09

### Added

- **"Send to TFactory for testing when done"** toggle in the New Task wizard — the UI last-mile for #496. Ticking it sets `auto_handover_tfactory`, which bridges through `projects.py` → `task_metadata.json` so the backend hands the finished build to TFactory on success. Best-effort; no-op unless `TFACTORY_BASE_URL` is set (#503).

### Fixed

- Complexity assessor no longer under-classifies multi-file features. A multi-endpoint / multi-layer feature (models + routes + wiring + tests) was collapsing to BMad Level 1 / Quick Flow — so the BMad story-planner never engaged — because a low-level keyword like "add" matched first. An *additive* structural floor now raises the level (never lowers it) on strong breadth: ≥2 HTTP endpoints, ≥4 architectural layers (or ≥3 with an endpoint), or ≥5 requirement deliverables / ≥2 services → Standard; ≥3 services / very broad surface → Complex. Requirements (acceptance criteria, services) are threaded into BMad detection, and a calibration test set guards against trivial-task regressions (#504).

### Changed

- Pre-commit pytest gate no longer requires `--no-verify`: `gvisor_live` live-cluster smoke tests are skipped unless explicitly selected (`-m gvisor_live` / `GVISOR_LIVE=1`), the suite defaults `APP_DISABLE_AUTH=true` to match CI (route tests no longer 401), and the worktree tests that shell out to `git worktree` are excluded from the fast gate (they run in full CI) (#508).

## 3.6.6 - 2026-06-09

### Added

- Opt-in **auto-handover to TFactory** for testing: a task created with `auto_handover_tfactory` hands its finished build (spec + requirements + PFactory/Task-Contract meta + mutation-ledger evidence) to TFactory's `/api/handoff` on successful completion. Best-effort, never blocks completion; no-op unless opted in and `TFACTORY_BASE_URL` is set. `StartTaskRequest.auto_handover_tfactory` flag + completion hook (#496, #501).

## 3.6.5 - 2026-06-09

### Fixed

- Spec creation now surfaces a provider **authentication failure** (e.g. an expired Claude OAuth token → 401) with an actionable "re-provision the credential (`claude setup-token`)" error, instead of `MAX_RETRIES` silent retries that collapsed into a generic "Agent did not create spec.md" (#483).
- `--merge` no longer aborts with a bogus "Merge conflict" when the build touched `.gitignore`: `merge_worktree` stashes uncommitted base-tree changes (the worktree's artifact `.gitignore`) before checkout+merge — dropped on success, restored on failure (#485).

### Added

- The main REST API now authenticates per-user **`acw_` API keys** (minted in Settings → API Keys), not just JWT + the legacy token — so a personal token works for direct programmatic access as well as the MCP proxy path (#479).

## 3.6.4 - 2026-06-09

### Fixed

- stdio-MCP / `/handover` `task_create_and_run` (write-path) no longer 500s: the proxy built a `StartTaskRequest`, but the handler takes `CreateAndRunRequest` (adds `provenance`, #332) and reads `request.provenance` → `AttributeError`. The proxy now passes `CreateAndRunRequest`, completing the handover end-to-end fix (with #488/#490 for the read-path) (#494).
- stdio-MCP `task_get_logs` no longer 500s: the proxy forwarded a `tail=` argument that `get_task_logs` doesn't accept (`TypeError`). `tail` is accepted for client compatibility but no longer forwarded (#494).

## 3.6.3 - 2026-06-09

### Fixed

- stdio-MCP / `/handover` `project_list` now returns the full project list. #488 stopped the 500 but, for the M2M acw/legacy principal, the org-scoped `list_projects` returned an empty list — so handover still found no project. The proxy now returns all registered projects (M2M service-principal behaviour) (#490).
- Antigravity/Gemini tasks no longer fail with `ModelNotFoundError: models/antigravity`: the bare provider-selector `antigravity` was passed literally as `--model antigravity` (not a real Gemini model) → CLI exit 1 → build failed with no completed subtask. Bare selectors now resolve to a real model; an explicit empty model still omits `--model` (#491, #492).

### Changed

- Antigravity/Gemini default model bumped to **`gemini-3.5-flash`** (newest validated on the CLI; `gemini-3.5-pro` not yet available) (#492).

## 3.6.2 - 2026-06-09

### Fixed

- Autonomous tasks no longer hang at 0% in "planning": when a build couldn't find the requested spec, the CLI dropped into an interactive `input()` prompt that blocks forever in a headless run (no TTY, stdin never EOFs). It now detects a non-interactive context and fails fast (#482).
- Default-org seeding is idempotent on the `organizations.slug` UNIQUE constraint — startup no longer crashes with `IntegrityError` when a "default"-slug org already exists under a different id (#484).
- The plan-based allowlist auto-grant now reads the simple/quick-spec plan's `verification.run` command, so a from-scratch build's toolchain (e.g. `go`) is granted and its verification actually runs instead of being blocked (#486).
- stdio-MCP / `/handover` `project_list` no longer returns HTTP 500: the proxy now forwards `request` + `db` to `list_projects` (required since org-scoped visibility), unblocking the handover project-lookup step (#488).

### Added

- Operator override for the command allowlist via `AIFACTORY_EXTRA_ALLOWED_COMMANDS` (comma/whitespace separated), merged into the enforced allowlist — backend hook for a Settings "additional allowed commands" field (#487).

## 3.6.1 - 2026-06-09

### Fixed

- SPA history-fallback so portal deep-links resolve instead of 404. The static mount served `index.html` only for `/`; any client-side route (e.g. `/console/<project_id>/<spec_id>`) returned `{"detail":"Not Found"}` on a deep-link or hard refresh because `StaticFiles` has no file at that path. `SPAStaticFiles.get_response` now catches the 404 and serves `index.html` for genuine SPA navigations (a GET whose final path segment has no file extension); real missing assets (`/assets/*.js`) keep their 404 and API routes are unaffected. This is the backend complement to the #314 cold-load routing fix (#480).

### Changed

- AIFactory MCP server + `/handover` skill now point at the deployment (`https://aifactory.freundcloud.org.uk`) instead of `localhost:3101`; token sourced from `~/.aifactory/.token-deployed`. The handover skill's track URL was corrected from the non-existent `/tasks/<id>` route to the real `/console/<project_id>/<task_id>`, with the board URL as a fallback (#480).

## 3.6.0 - 2026-06-08

### Added

- **Act-loop reliability hardening** (Hermes-inspired, all flag-gated default-off):
  - Anti-loop / no-progress guardrail — a tool-call-signature controller (repeated-exact-failure → block, same-tool-failure → halt, idempotent-no-progress → block) wired via PreToolUse/PostToolUse hooks; the coder and QA loops break/escalate early on halt and the typed reason rides into the RFC-0001 completion event (`halt_reason`). `AIFACTORY_ACT_GUARDRAIL` (#474).
  - Budgeted context summary at the SDK `PreCompact` boundary — a deterministic structured 9-section "active task" summary persisted for post-compaction re-anchor, with token budgeting, an anti-thrash guard, and a deterministic fallback. `AIFACTORY_CONTEXT_SUMMARY` (#475).
  - Checkpoint-before-mutation + per-turn mutation ledger — a cheap git checkpoint before each Write/Edit/Bash, a `.aifactory/mutations.jsonl` ledger, turn-end claimed-vs-actual verification, rollback, and the ledger carried into the TFactory handoff as evidence. `AIFACTORY_MUTATION_LEDGER` (#476).

## 3.5.1 - 2026-06-08

### Added

- **Reliable completion-event delivery** (epic #468): additive RFC-0001/CloudEvents envelope upgrade — per-event `id` (idempotency), CloudEvents-core fields (`specversion`/`source`/`type`/`time`) and W3C `traceparent`; a transactional outbox + retrying relay for at-least-once delivery (behind `AIFACTORY_COMPLETION_OUTBOX`); and typed handback triage validation + an assertion-pinning guard before the QA fixer (#465, #466, #467).
- Running server version shown on the login screen (#470).

### Notes

- Ships as 3.5.1 because the 3.5.0 release build failed its CHANGELOG gate before tagging; 3.5.0 was never published.

## 3.4.3 - 2026-06-07

### Added

- GitHub Agentic Integration (#456): GitHub Models as a first-class provider (`github-models/<publisher>/<model>` model strings, zero-cost inference via `GITHUB_TOKEN`); Copilot cloud agent dispatch (`copilot:delegate` label routes a task to `copilot-swe-agent[bot]`, polls for PR, transitions status to `copilot_running` / `copilot_pr_opened`); AIFactory MCP server (`POST /mcp`, 6 tools, Bearer auth) so the Copilot agent can read specs/plans and write discoveries; three GitHub Actions workflows (`aifactory-task.yml`, `copilot-pr-review.yml`, `pr-review.yml`); frontend GitHub Models picker in the agent profile selector and Copilot dispatch toggle in the task creation wizard.

### Fixed

- Parallel wave subtasks using non-Claude providers (antigravity, copilot, opencode, ollama) now surface the real provider error in the portal instead of a hardcoded `"agent session error"` placeholder, and write an error entry to the canonical task log (child-worktree logs were never synced back, leaving the portal with zero entries) (#455).

## 3.4.2 - 2026-06-02

### Fixed

- Release pipeline now publishes again: the multi-arch container image build failed on the `linux/arm64` leg (the frontend `vite build` dies under QEMU emulation), which blocked image/SBOM publishing for v3.4.0 and v3.4.1. The release image is built `linux/amd64`-only for now; native arm64 builds will be restored via a dedicated runner.

## 3.4.1 - 2026-06-02

### Fixed

- Worktree management now heals a stray `core.bare=true` on the primary checkout (self-heal on `WorktreeManager` init + per-invocation guard), so a bare-marked repo no longer breaks worktree creation, status checks, and host-side git.
- Project-context state reconciled on load: the kanban board, sidebar, and project dropdown no longer diverge when the persisted active tab and last-selected project disagree (the dropdown could previously get stuck on the wrong project).

## 3.4.0 - 2026-06-02

### Added

- Inter-agent inbox messaging with between-turn delivery, plus `#task` / `@agent` mention parsing and inbox routing.
- Solo mode — a single self-directed agent that writes and works its own plan (token-saving for small jobs; default off).
- Per-task observability panels in the task detail (token usage by category, live CPU/RAM, send-to-agent box) and per-category token monitoring with post-compact context recovery.
- OpenCode CLI runtime provider; Antigravity CLI provider (renamed from Gemini) with portal-driven install/update.
- Rate-limit auto-resume after cooldown; ports-and-adapters Feature Architecture Standard enforced by lint.

### Fixed

- Control-plane / task state isolated from agent worktree sync (no longer clobbered by builds).
- Peer-review obligations enforced with delivery proof + strict review cycles.
- Codex builds on ChatGPT-account logins via an account-default model (no forced `--model`).
- openai-compatible Bedrock/Azure/Vertex routing through the LiteLLM gateway (the agentic provider now honors the gateway + auth).
- All-failed builds now surface as failed instead of being masked as `human_review`/"completed".
- Token usage `totalTokens` emitted and the token panel aligned to the API response.

### Security

- `.envrc` untracked and gitignored so local API keys can't be committed.

## 3.3.0 - 2026-05-30

### Documentation

- Themed the documentation site to match the skill_pool terminal aesthetic (phosphor green on black, JetBrains Mono, CRT scanlines).
- Repositioned the docs site on the open-source, self-hostable, auditable message — new tagline, hero, feature cards, intro, and a new "Why AIFactory" page; roadmap "Direction" preamble; GTM strategy memo under `docs/plans/`.
- Enabled the built-in Docusaurus blog at `/blog` (RSS/Atom feeds, tags, reading time) with an authors file and the first post, "Why we can't use Cursor at a bank".
- Added an additive retro CRT layer (a sweeping refresh beam + scanline flicker) on the terminal theme; both disabled under `prefers-reduced-motion`.

### Added — GCP MCP catalog entry (#168, Epic #100)

- **GCP catalog entry (`transport="http"`)** — closes the last gap in the default MCP server catalog (Epic #100). Google Cloud AI Companion MCP went GA in March 2026 and uses a remote-first HTTP transport rather than a local subprocess. The entry is auto-enabled when a project has GCP markers (`gcp/`, `app.yaml`, `cloudbuild.yaml`) and `GOOGLE_APPLICATION_CREDENTIALS` is set. See `apps/backend/agents/tools_pkg/mcp_catalog.py`.
- **HTTP transport in `MCPCatalogEntry`** — the dataclass now supports `transport="http"` with `http_endpoint`. `build_server_config()` routes to `_build_http_config()` for HTTP entries, producing `{"type": "http", "url": "..."}` (the shape the Claude Agent SDK's HTTP MCP client expects). V1/V1.5 stdio entries are byte-for-byte unchanged.
- **`GCP_MCP_ENDPOINT` env override** — the default GA endpoint (`https://cloudaicompanion.googleapis.com/v1/extensions/default/mcp`) is overrideable at call time via `GCP_MCP_ENDPOINT`. Useful for VPC Service Controls perimeters, staging projects, and future GCP MCP servers (BigQuery, Cloud Run, etc.).
- **Helm slot `mcpCredentials.gcp`** — new sub-block under `mcpCredentials` with `secretName` (GCP-dedicated Secret, overrides the shared `secretName` for the SA JSON mount) and `endpointOverride` (wires `GCP_MCP_ENDPOINT` into the pod). The existing `mcpCredentials.providers.gcp` file mount + env var is unchanged; the new block is additive.
- **Docs** — GCP section added to `docs/docs/concepts/mcp-credentials.md` covering Workload Identity (preferred on GKE), service-account JSON setup, endpoint override, and troubleshooting.
- **Tests** — `tests/test_mcp_catalog_gcp.py` (25 unit tests for catalog shape, marker detection, endpoint override, HTTP config shape, backward-compat) and `tests/helm/test_mcp_credentials_gcp.py` (8 Helm acceptance tests for secret mount, secretName override, and endpointOverride rendering).

### Added — v1.2 per-tenant audit-chain anchor (#208)

- **Per-tenant HMAC-SHA256 signing keys** — the tenant reconciler issues one 32-byte KMS-wrapped key per isolated org (Option A: one chain + one key per tenant, ISO 27001 A.12.4.2/A.12.4.3 compliant). Keys are stored in `audit_signing_keys` with `org_id` set and mirrored to the tenant's Vault path for auditor handover.
- **Per-tenant chain genesis** — each isolated tenant's first `audit_logs` row uses `prev_hash='GENESIS-T-<org-uuid>'`, separating the per-tenant chain from the shared chain. `tenant_audit_state` table tracks the cutover boundary, current chain head, and lifecycle ('active'|'sealed').
- **Per-tenant daily anchor** — the cron iterates over isolated orgs (in id order, batch-committed) and emits one `audit_anchors` row per org per day, signed with the org's key. One tenant's KMS failure does NOT block others (failure-safe per-tenant loop).
- **Per-tenant verifier** — `verify_tenant_anchored_export(ndjson, signing_keys, org_id)` validates a per-tenant export's chain. Asserts cross-tenant replay is rejected (design finding #6). `compute_tenant_hash` and `verify_tenant_chain` in `audit_chain.py` provide the domain-separated chain helpers.
- **Helm opt-in** — `audit.anchor.perTenant: false` (default). `perTenantOptions.keyCacheSize`, `batchSize`, `retentionDays`, `metrics.perOrgLabels` operator-tunable. Validator rejects `perTenant=true` without `tenant.isolationEnabled=true`.
- **Export endpoint change (v1.2 behavior change)**: `?org_id=...&include_anchors=true` now accepted for isolated tenants when `perTenant=true` (was 400 in v1.1 — the per-tenant chain CAN verify a per-tenant export). Shared-chain deployments unchanged.
- **ISO 27001 evidence** — A.12.4.2, A.12.4.3, A.18.1.3, A.18.2.2 entries updated with per-tenant chain contribution (v1.2+; v1.1 entries preserved). Closes the v1.1 gap where "protection of log information" was evidenced at deployment granularity only.
- **Schema migration** (`c3d7e8f1a2b4`): `audit_anchors.org_id` (nullable FK, ON DELETE SET NULL), `audit_signing_keys.org_id` (nullable FK, ON DELETE CASCADE), `tenant_audit_state` table, composite index `audit_logs(org_id, created_at)`.
- Refs: #208 / Epic #204. Design doc: `docs/plans/2026-05-29-per-tenant-audit-anchor-design.md`.

---

## 3.2.0 - 2026-05-29

- Interim release on the Enterprise v1.1 line. See the [v3.2.0 release](https://github.com/olafkfreund/AIFactory/releases/tag/v3.2.0) and git history for the full commit list.

---

## 3.1.0 - 2026-05-29

**Enterprise v1.1: Multi-tenant isolation, observability, audit hardening, and legacy-IdP federation**

AIFactory ships 7 major features for regulated deployments (banks, healthcare, fintech). Epic #35 closed all 9 child issues. All features opt-in via Helm values.

### Added — Epic #35 (Enterprise v1.1)

#### Identity & Access (SAML 2.0 + SCIM 2.0) — #41
- **PR #177 (PR-1a)**: SAML security foundation — OneLogin SDK wrapper (`strict=True`, `wantAssertionsSigned=True`, RSA-SHA256), HMAC-signed RelayState (CSRF defence), SCIM Pydantic schemas (RFC 7643), min-viable filter parser (eq-only on `userName`/`externalId`/`active`), Bearer-token middleware with constant-time compare. 48 unit tests.
- **PR #178 (PR-1b1)**: `external_identities` table (kind + subject + FK CASCADE) for multi-IdP linkage; auto-backfill of `users.oidc_sub` as `kind='oidc:legacy'`. 4 Postgres tests.
- **PR #195 (PR-1b2)**: SAML SP routes (/login, /acs, /metadata) with HMAC-bound RelayState, per-assertion-TTL replay cache, XSW defence. Cross-IdP collision guard (same-email-different-IdP → 409).
- **PR #198 (PR-1b3)**: SCIM 2.0 CRUD on Users + Groups with array-append PATCH (Azure AD compat), soft-delete + 404-on-GET.
- **PR #199 (PR-2)**: Helm `saml:` + `scim:` blocks with required-when-enabled validators. E2E tests. Concept doc: [saml-scim](docs/docs/concepts/saml-scim.md).

#### Tenant Isolation Mode — #36
- **PR #192 (PR-1)**: Per-tenant K8s Namespace + ServiceAccount + NetworkPolicy + IAM (IRSA) + Vault path schema skeleton.
- **PR #200 (PR-2)**: Full K8s/IAM/Vault writes. Redis SETNX leader election with Lua check-and-delete release. Agent spawner routing to per-tenant pods.
- **PR #201 (PR-2 continued)**: Tenant dry-run reconciler. OPA Gatekeeper sample policies (namespace-prefix + RoleBinding-tenant-scope). Daily teardown CronJob with 24-hour dry-run window.
- **PR #206 (PR-3)**: Helm pre-install CNI probe (Calico/Cilium hard-fail for FQDN policy support). Helm `tenant.isolationEnabled` block. Concept doc: [tenant-isolation](docs/docs/concepts/tenant-isolation.md).

#### LiteLLM Gateway — #38
- **PR #193 (PR-1)**: LiteLLM gateway env redirect for HTTP providers (OpenAI-compatible routing).
- **PR #194 (PR-2a)**: `organizations.allowed_models` JSON column for per-org LLM allowlist.
- **PR #202 (PR-3)**: LiteLLM sub-chart deployment. Per-org budget + rate-limit + PII-redacted audit hooks. Grafana dashboards (7 panels). Concept doc: [litellm-gateway](docs/docs/concepts/litellm-gateway.md).
- **PR #203 (PR-2b)**: Audit hook + PII redactor (mask CC#, API keys). Per-org virtual-key lifecycle.
- **Scope caveat:** Claude calls (via Agent SDK) bypass gateway in v1.1 — SDK speaks Anthropic-format `/v1/messages`, wire-incompatible with LiteLLM's OpenAI endpoint. Closes v3.0 limitation #6; v1.2 adds Claude enforcement wrapper.

#### Cloud LLM Routing (Bedrock + Vertex) — #39
- **PR #197**: Bedrock + Vertex AI routing via LiteLLM gateway. Concept doc: [cloud-llm-routing](docs/docs/concepts/cloud-llm-routing.md).

#### OpenTelemetry Distributed Tracing — #42
- **PR #175**: In-process tracer + auto-instrumentation (FastAPI, SQLAlchemy, asyncpg, httpx, Redis). Per-phase manual `task:phase:*` spans. W3C `traceparent` injected into Redis envelopes + subprocess env. CorrelationIdMiddleware sources `request_id` from active `trace_id`. 14 unit tests.
- **PR #176**: Helm `otel:` block (samplingRatio, headersSecretName for vendor auth). Agent-subprocess `tracing_bootstrap.py`. 23 tests (12 helm + 8 bootstrap + 3 e2e). Concept doc: [observability-tracing](docs/docs/concepts/observability-tracing.md).
- Closes v3.0 limitation #4.

#### ISO 27001 Evidence + Signed Audit-Chain Anchor — #43
- **PR #180**: Design doc with 8 brainstorm decisions + reviewer findings baked in.
- **PR #181**: Schema migration adding `audit_anchors`, `audit_signing_keys`, `audit_logs.classification`, `users.last_login_at`. UTC-day unique index. 5 Postgres tests.
- **PR #182**: `audit_anchor.py` signer/verifier. `_SigningKey` newtype, leak-safe `__repr__`, HMAC sign/verify, KMS-wrapped key versioning. 15 unit tests.
- **PR #183**: `audit_anchor_cron.py` — daily 00:00 UTC tick, startup backfill, first-anchor semantics, idempotency. Classification-window hashing closes "flip confidential→public to leak" attack. 8 unit tests.
- **PR #184**: `audit_export.py` interleaves anchors into NDJSON. `verify_anchored_export()` offline verifier for auditors. Fixed chain-head semantic bug. 7 unit tests.
- **PR #185**: `GET /api/admin/access-review` endpoint (SOC2 CC6.2 + ISO A.9.2.5 quarterly reviews). 6 unit tests.
- **PR #186**: Helm `audit.anchor:` block (Kubernetes CronJob or in-process scheduler). ISO 27001 Annex A evidence map at `guides/compliance/iso27001-evidence.md` (30 controls directly evidenced). Concept doc: [audit-anchor](docs/docs/concepts/audit-anchor.md). 10 helm tests.
- Closes v3.0 limitation #1.

#### gVisor RuntimeClass — #37
- **PR #169**: Agent pods opt-in to `runtimeClassName: gvisor` for kernel-level isolation.

#### Multi-Replica Support (S3 + Redis) — #40, #154
- **PR #171**: Cross-replica event bus via Redis pub/sub.
- **PR #172**: Helm Redis pub/sub chart wiring + multi-replica docs.
- **PR #173**: WorkspaceStore module + fsspec-based S3 snapshots (AWS / MinIO / GCS / Azure).
- **PR #174**: Helm storage block + agent snapshot hook + MinIO CI.
- Closes v3.0 limitation #5.

### Migration & Upgrade

**All v1.1 features are off by default.** Enable in your Helm values:
```yaml
saml:
  enabled: true
scim:
  enabled: true
tenant:
  isolationEnabled: true
litellm:
  enabled: true
otel:
  enabled: true
audit:
  anchor:
    enabled: true
workspaces:
  storage:
    enabled: true
redis:
  enabled: true
```

**Backwards-compatible:** No schema-breaking migrations. Operators upgrade by bumping chart version + applying `helm upgrade`. The `alembic upgrade head` migration runs automatically on web-pod start (auto-apply default).

### Out of scope (v1.2 tracking issue #204)
- Claude-on-LiteLLM enforcement wrapper (scope caveat above)
- Per-tenant audit-chain anchor
- SAML Single Logout
- PII bundle: Luhn-checked CC pattern + scrubBeforeSend mode

### Contributors

Shipped across 32 merged PRs (#169–#199, #200–#206) in a single coordinated session. Epic #35 closed all 9 child issues.

### ⚖️ Licensing

- **Relicensed from AGPL-3.0 → dual MIT OR GPL-3.0.** AIFactory is now
  available under the recipient's choice of either license. See
  `LICENSE`, `LICENSE-MIT`, and `LICENSE-GPL`. SPDX identifier:
  `MIT OR GPL-3.0-only`. The `dataseek.team` enterprise-licensing
  contact line (which referenced a non-existent email) was removed.

### 🏷️ Branding

- **Rebrand `dataseeek` → `olafkfreund`.** The `dataseeek` GitHub org
  doesn't exist; every reference in non-archive files was rewritten to
  point at the actual repo location (`olafkfreund/AIFactory`) and the
  actual GitHub Pages URL (`olafkfreund.github.io/AIFactory`). Affects
  README badges, docusaurus config, package.json URLs, demo repo path,
  cosign verify identity in image-mirroring drills, and ghcr.io image
  paths in the Helm chart docs.

### 📚 Documentation

- **Full docs rewrite + GitHub Pages site.** The `guides/` directory was
  archived to `docs-archive/2026-05-26/guides/` (git history preserved).
  A fresh Docusaurus site at `docs/` is published to
  <https://olafkfreund.github.io/AIFactory/> via a new
  `.github/workflows/docs.yml` workflow. Includes 18 reorganized pages:
  Getting Started, Demo, Concepts (3), Architecture (3 with Mermaid
  diagrams), Wiki (FAQ/Troubleshooting/Glossary), Showcase, Compliance
  (SOC2/GDPR), Contributing, Roadmap. The legacy `guides/` content is
  unchanged in archive form and still searchable via `git log --follow`.

- **README.md slimmed from 557 to 115 lines.** Hero + tagline + 60-second
  quickstart + demo callout + screenshot grid + prominent docs links.
  Everything operational moved to the docs site.

### CI / Testing

- **Added live-cluster gVisor CI smoke test (closes #170).** New workflow
  `.github/workflows/gvisor-smoke.yml` brings up a Kind cluster on a
  GitHub-hosted runner, installs `runsc` via the official gVisor apt repo,
  registers a `gvisor` RuntimeClass, deploys AIFactory with
  `sandbox.gvisor.enabled=true`, and runs the new
  `tests/helm/test_live_gvisor.py` live-cluster suite. The suite validates
  every "works" row in the gVisor compatibility table
  (`docs/docs/concepts/gvisor-sandbox.md`): RuntimeClass on live pods,
  `git clone`, `curl` HTTPS egress, workspace PVC read/write, and the bash
  allowlist command set. Jobs are gated on `push: [dev, main]` and
  `pull_request: [dev]`. Runs Option 2 (gVisor-with-kind on GitHub-hosted
  runners) — no self-hosted runner required.

### 🏛️ Enterprise v1.1 (Epic #35)

- **#42 — OpenTelemetry distributed tracing** ✅ (closed)
  - PR #175: in-process tracer + auto-instrumentation (FastAPI, SQLAlchemy,
    asyncpg, httpx, Redis), per-phase manual `task:phase:*` spans for the
    agent lifecycle, W3C `traceparent` injected into Redis envelopes for
    cross-replica trace continuity, `TRACEPARENT` env injected into
    subprocess for cross-process continuity, `CorrelationIdMiddleware`
    sources `request_id` from active `trace_id` (client header still wins
    for back-compat), full failure-safe contract (broken collector never
    crashes the app), 14 unit tests with `InMemorySpanExporter`.
  - PR #176: Helm `otel:` block (typed config, `samplingRatio ∈ [0.0, 1.0]`,
    `headersSecretName` for vendor-auth), two operator-misconfig validators
    that fail `helm template` loud, agent-subprocess `tracing_bootstrap.py`
    that re-attaches the parent context from `TRACEPARENT`, concept doc
    at [`/concepts/observability-tracing`](https://olafkfreund.github.io/AIFactory/concepts/observability-tracing),
    23 new tests (12 helm + 8 bootstrap + 3 e2e).
  - Closes v3.0 limitation #4 ("No built-in OpenTelemetry distributed tracing").
- **#43 — ISO 27001 evidence + signed audit-chain anchor** ✅ (closed)
  - PR #180: design doc with 8 brainstorm decisions + 5 critical
    reviewer findings baked in.
  - PR #181: schema migration adding `audit_anchors`, `audit_signing_keys`,
    `audit_logs.classification`, `users.last_login_at`. Postgres
    UTC-day unique index makes daily anchoring idempotent. 5 Postgres
    acceptance tests.
  - PR #182: `audit_anchor.py` signer/verifier service. `_SigningKey`
    newtype with leak-safe `__repr__`, HMAC sign/verify, versioned
    KMS-wrapped key storage so root-key rotation doesn't invalidate
    prior anchors. KMS-decrypt contract enforced (raises on wrong
    type or wrong length so cloud backends can't silently produce
    wrong HMACs). 15 unit tests including log-safety verification.
  - PR #183: `audit_anchor_cron.py` — daily 00:00 UTC tick, startup
    backfill of missed days, zero-row-day handling, first-anchor
    semantics, idempotency. Classification-window hashing closes the
    "flip confidential→public to leak past export filter" attack
    surface — design decision #5 honest revision (the original
    `_canonical()` extension was mathematically incompatible with
    pre-#43 chain re-verification, so classification protection moved
    to the anchor layer). 8 unit tests.
  - PR #184: `audit_export.py` interleaves anchors deterministically
    into NDJSON. `verify_anchored_export()` is the offline verifier
    helper auditors use. Caught + fixed a real semantic bug in #183
    where the cron stored raw `prev_hash` instead of the outgoing
    chain head. 7 unit tests.
  - PR #185: `GET /api/admin/access-review` endpoint for SOC2 CC6.2 +
    ISO 27001 A.9.2.5 quarterly access reviews. `users.last_login_at`
    stamped on every successful OIDC login. 6 unit tests.
  - PR #186: Helm `audit.anchor:` block with Kubernetes CronJob OR
    in-process scheduler. ISO 27001 Annex A evidence map at
    `guides/compliance/iso27001-evidence.md` (~30 controls directly
    evidenced; remainder marked as operator responsibility). Concept
    doc at [/concepts/audit-anchor](https://olafkfreund.github.io/AIFactory/concepts/audit-anchor).
    10 helm acceptance tests covering toggle, scheduler enum,
    cron-knob flow-through, security-context match with web pod.
  - Closes v3.0 limitation #1 ("Audit chain has no signed external
    anchor").
- **#41 — SAML 2.0 + SCIM 2.0 for legacy-IdP banks** ✅ (closed)
  - PR #177 (PR-1a): Security-foundation modules. SAML replay cache with
    per-assertion TTL (not blanket-LRU — the reviewer-flagged trap), OneLogin
    SDK wrapper with `strict=True` + `wantAssertionsSigned=True` + RSA-SHA256
    hard-coded, HMAC-signed RelayState (CSRF defence with constant-time
    verify), SCIM Pydantic schemas per RFC 7643, minimum-viable filter
    parser (eq-only on `userName`/`externalId`/`active`), Bearer-token
    middleware with 503-loud-on-misconfig + constant-time compare. 48 unit
    tests, all the security-critical surface where bugs become security holes.
  - PR #178 (PR-1b1): `external_identities` table (kind + subject + FK
    CASCADE) for multi-IdP linkage; backfills existing `users.oidc_sub`
    rows as `kind='oidc:legacy'`. 4 Postgres-marked tests.
  - PR #195 (PR-1b2): SAML SP routes `/login` + `/acs` + `/metadata` with
    HMAC RelayState verification, per-assertion-TTL replay defence, and
    the cross-IdP collision guard (decision #4 — auto-link only when no
    other-kind identity exists; otherwise 409).
  - PR #198 (PR-1b3): full SCIM CRUD on `/api/scim/v2/Users` + `/Groups`
    per RFC 7644: array-append PATCH semantics (the Azure-AD-relies-on-it
    bug), soft-delete + 404-on-GET (the Azure-AD-resync-loop trap), `If-Match`
    accepted as advisory only.
  - PR #196 (PR-1b4): merged identity-provider discovery for the login
    page (`GET /api/auth/identity-providers`); SAML + OIDC routers
    auto-mount on enable. SAML session extended into `auth.py`'s
    `current_user_dependency` (cookie shape matches OIDC).
  - PR-2 (this PR): Helm `saml:` + `scim:` blocks with 4 schema validators
    (operator misconfigs fail at `helm install`, not first pod start), env
    + Secret-mount wiring, SP cert rotation via projected-volume optional
    source. Concept doc at [/concepts/saml-scim](https://olafkfreund.github.io/AIFactory/concepts/saml-scim)
    with Okta / Azure AD / Keycloak IdP preset recipes + SP cert rotation
    runbook + decision-#11 local-logout note. 30 helm-toggle tests
    covering all 4 validators + env wiring + coexistence with v1.1 toggles.
    New `saml-scim (P8 acceptance)` CI job.
  - Closes v3.0 limitation #2 ("SSO is OIDC-only").
- **#37 — gVisor RuntimeClass** ✅ (closed previously this cycle): agent
  pods can opt in to `runtimeClassName: gvisor` for kernel-level isolation.
- **#40 — S3 workspace storage + Redis pub/sub** ✅ (closed previously this
  cycle): workspaces snapshot to fsspec-backed S3 (AWS / MinIO / GCS /
  Azure); Redis pub/sub fans out WebSocket events across replicas.
  Closes v3.0 limitation #5 ("Single-replica only").
- **#154 — Scoped MCP API keys** ✅ (closed previously this cycle):
  per-developer `acw_` keys with scope-gated mutating routes.

### ✨ Added

- **`scripts/demo.sh`** — end-to-end demo runner (Bash + jq + gh).
  Seeds `olafkfreund/aifactory-demo` with 3 issues, registers the repo
  with your portal, imports the issues as backlog tasks, prompts you
  to drive Claude Code from the terminal, then kicks off an autonomous
  build. Flags: `--yolo`, `--no-reset`, `--portal=URL`.

- **`scripts/capture-screenshots.ts`** — Playwright headless Chromium
  driver that captures 14 named PNGs of the marquee portal views to
  `docs/static/img/screenshots/`. Reproducible — anyone can refresh
  the gallery with `npm -w apps/frontend-web run capture-screenshots`.

- **`Justfile`** — canonical command index. `just --list` shows
  `install`, `backend`, `frontend`, `docs-dev`, `demo`, `screenshots`,
  `test-backend`, `test-frontend`, `test-postgres`, `test-all`.

- **Root `package.json` scripts**: `docs:install`, `docs:dev`,
  `docs:build`, `demo`, `screenshots`.

---

## 3.0.2 - 2026-05-26

Patch release fixing two leftover wiring + branding bugs from v3.0.0.

### 🛠️ Fixed

- **P6 observability never wired into `main.py`**. The
  `server/observability/` package shipped in v3.0.0 (Epic #26 P6)
  but `main.create_app()` never called `install_metrics(app)`,
  `configure_structlog()`, or `app.add_middleware(CorrelationIdMiddleware)`.
  As a result the production portal exposed neither `/metrics` nor
  structured JSON logs nor correlation IDs — despite all P6 unit
  tests passing (they built their own minimal FastAPI app and called
  the functions directly, bypassing main.py). v3.0.2 wires the three
  calls in the correct order:
  - `configure_structlog()` at the top of `create_app()` so
    boot-time logs are already JSON.
  - `CorrelationIdMiddleware` added LAST so it's the outermost
    layer (sets X-Request-ID before TokenAuth runs; 401 responses
    still carry the ID — auditors rely on this).
  - `install_metrics(app)` after all routers are mounted so the
    Prometheus instrumentator can derive cardinality-capped
    `handler` labels from the route table.

  Regression test added at `tests/obs/test_p6_main_wiring.py`:
  imports `main.create_app()` and asserts `/metrics` returns 200 +
  CorrelationIdMiddleware echoes back `X-Request-ID` + the FastAPI
  app title is AIFactory + `app.version` matches the package version.
  Gates every PR forward.

- **Leftover Magestic branding in `main.py`**. The v3.0.0 rebrand
  missed three string constants:
  - `title="Magestic AI Web API"` → `"AIFactory Web API"`
  - `description="Web API for Magestic AI autonomous coding framework"`
    → `"Web API for AIFactory — self-hosted AI task management +
    agent orchestration"`
  - Root-route message `"Magestic AI Web Server"` →
    `"AIFactory Web Server"`

  Plus the hardcoded `version="1.0.0"` on the FastAPI app + on
  `/api/health` was a drift hazard. v3.0.2 reads the canonical
  version from `apps/backend/__init__.py` at startup (the file
  `bump-version.js` already updates on every release), via a tiny
  `_read_app_version()` helper. No more silent version-skew.

### Upgrade notes

- Backwards-compatible patch: `helm upgrade aifactory --version 3.0.2`
  picks up both fixes with no schema or config changes.
- Operators who deployed v3.0.1 had a non-functional `/metrics`
  endpoint. After upgrading, configure your Prometheus scrape job
  against the now-live endpoint (see `docs-archive/2026-05-26/guides/operations/observability.md`).

## 3.0.1 - 2026-05-26

Patch release with two operator-visible fixes.

### 🛠️ Fixed

- **SQLite migration crash on fresh install**. The P2.3
  `encrypt_credentials` migration (`c6e3b2d4a8f0`) used a direct
  `op.alter_column(nullable=False)` to re-apply the NOT NULL
  constraint on `email_accounts.access_token` after the encrypted-
  column swap. SQLite doesn't support `ALTER TABLE ... ALTER
  COLUMN ... SET NOT NULL` — backends booting against a fresh
  SQLite (`autoApply=true` in the Helm chart's POC path; default
  local-dev path) crashed during `alembic upgrade head`. Wrapped
  the step in `op.batch_alter_table`, mirroring P3.3's
  `d8f1a3c5e7b9` migration. Postgres deployments are unaffected
  (their behavior was correct via the same native ALTER).
  Regression test added at `tests/secrets/test_p2_sqlite_migration.py`
  that runs `alembic upgrade head` against a temp SQLite file —
  gates every PR going forward.

- **AIFactory logo not displaying in the sidebar/loading screen/
  onboarding**. The new logo + favicon assets were stashed before
  P1 work began and never restored to the main release. Bundle
  contains the updated `logo.png` (547 KB, full-res AIFactory
  brand), `favicon.ico` (15 KB), `apple-touch-icon.png` (43 KB),
  and 16/32 px favicon variants. The sidebar `<img src="/logo.png">`
  reference is unchanged — the new files just slot in.

### Upgrade notes

- **Operators on v3.0.0**: this is a backwards-compatible patch.
  `helm upgrade` to v3.0.1 picks up both fixes.
- **Operators who already migrated** (the SQLite migration crash
  blocked them from getting that far on v3.0.0): no special
  handling needed — fresh install + `helm install aifactory --version 3.0.1`
  works end-to-end.

## 3.0.0 - 2026-05-26

The AIFactory **enterprise GA** release (Epic #26). Self-hosted Helm
chart with PSS-restricted defaults, encrypted-at-rest secrets backed
by 5 KMS backends, OIDC SSO, tamper-evident audit chain, GDPR
right-to-erasure, structured-JSON observability + Prometheus
metrics, and a full SOC 2 / GDPR / STRIDE evidence pack with three
ship-readiness drill scripts.

### ⚠ Breaking changes

- **Forward-only schema migration** `c6e3b2d4a8f0_encrypt_credentials`:
  `email_accounts.access_token`, `email_accounts.refresh_token`, and
  `llm_endpoints.api_key` columns convert from plaintext `Text` to
  encrypted `LargeBinary`. The migration is **forward-only** — there
  is no downgrade path. Operators MUST take a `pg_dump` backup before
  upgrading from any v2.x install.
- **Required Postgres backend for production**: SQLite remains
  supported for dev/POC, but `kms_data_keys` + the audit chain
  expect Postgres semantics for indexed lookups.
- **Container runs as non-root uid 65532** with read-only root
  filesystem and dropped capabilities. Operators with custom
  init-containers writing to `/` must mount tmpfs/emptyDir.

### ✨ Added — Epic #26 phases

- **P0 — Container hygiene**: Chainguard distroless base
  (digest-pinned), Trivy CVE scan, Syft SBOM, cosign keyless
  signing via GitHub OIDC, multi-arch (amd64+arm64) manifest
  inspection.
- **P1 — Postgres backend**: `asyncpg` driver, Alembic migrations,
  optional `APP_MIGRATIONS_AUTO_APPLY=false` for Helm Job mode,
  bank-grade privilege model (no SUPERUSER, no CREATE EXTENSION).
- **P2 — Encrypted secrets at rest**: `EncryptedString`
  `TypeDecorator` over `LargeBinary`, per-org `kms_data_keys` with
  LRU cache, 5 KMS backends (`fernet` for dev, `aws_kms`,
  `vault_transit`, `azure_kv`, `gcp_kms`), root-key rotation CLI
  (`python -m server.crypto rotate-root`), forward-only column
  migration with KMS-aware backfill.
- **P3 — OIDC SSO**: `authlib`-based Authorization Code + PKCE +
  state + nonce, JIT user/`OrganizationMember` provisioning with
  claim-mapped roles (`APP_OIDC_GROUP_TO_ROLE`), 15-minute access
  TTL + 8-hour refresh, IdP-validated refresh path with userinfo
  caching, logout redirect to IdP `end_session_endpoint`. Presets
  for Keycloak, Okta, Azure AD.
- **P4 — Helm chart**: `charts/aifactory/` with PSS-restricted
  security contexts, default-deny NetworkPolicy + 443 egress
  allowlist, ExternalSecret templates for 4 backends, optional
  bundled Postgres `StatefulSet` for POC mode, `customCABundle`
  for TLS-intercepting corporate proxies, schema-validated
  `values.yaml`.
- **P5 — Audit hardening**: SHA-256 hash chain on every audit-log
  write, NDJSON + CSV streaming export at `/api/audit/export`,
  air-gappable external verifier (`python -m server.audit
  verify-chain`), GDPR Art. 17 erasure that re-hashes the chain so
  `verify-chain` continues to pass, daily retention job (default
  13 months = SOC 2 12 + buffer).
- **P6 — Observability**: `structlog` JSON-to-stdout with
  ISO-8601 timestamps + `request_id` binding, correlation-ID
  middleware (`X-Request-ID`) with `httpx` propagation, Prometheus
  `/metrics` with cardinality-capped `handler` labels (route
  templates, not raw paths), optional `METRICS_SCRAPE_TOKEN`
  bearer gate, Helm `ServiceMonitor` template, pre-built Grafana
  dashboard JSON (7 panels).
- **P7 — Evidence + ship-readiness drills**: SOC 2 evidence pack
  (CC1-CC9 + A1 + C1), GDPR DPIA + data-flow diagram, STRIDE
  threat model, 4-cloud-path deployment runbook (EKS+RDS / AKS+
  Azure Postgres / GKE+Cloud SQL / vanilla K8s+Vault), v0.x → v3.0
  upgrade guide, three executable drill scripts
  (`backup-restore.sh`, `upgrade-in-place.sh`, `image-mirroring.sh`)
  with `--dry-run` modes.

### 📚 Documentation

New operator runbooks under `guides/`:
- `guides/operations/audit-trail.md`
- `guides/operations/encrypted-secrets-dr.md`
- `guides/operations/image-mirroring.md`
- `guides/operations/kms-rotation-runbook.md`
- `guides/operations/observability.md`
- `guides/operations/oidc-setup.md`
- `guides/deployment/helm-install.md`
- `guides/deployment/runbook.md`
- `guides/deployment/upgrade.md`
- `guides/compliance/soc2-evidence.md`
- `guides/compliance/dpia-data-flow.md`
- `guides/security/threat-model.md`
- `guides/observability/grafana-aifactory.json`

### 🧪 CI

11 acceptance jobs gate every PR (≈2000 tests total):
`backend (ruff + pytest)`, `docker (P0)`, `postgres (P1) × {15, 16}`,
`secrets (P2)`, `oidc (P3)`, `helm (P4)`, `audit (P5)`, `obs (P6)`,
`evidence (P7)`, `frontend (typecheck)`.

### ⚠ Documented v3.0 limitations (v3.1 follow-ups)

Tracked in `guides/compliance/soc2-evidence.md § Documented
limitations`. Each maps to a v3.1 Epic #35 issue:

1. ~~Audit chain has no signed external anchor.~~ ✅ **Closed by
   Epic #35 #43** — daily HMAC-signed anchor + KMS-wrapped key
   rotation + offline verifier helper. See
   [audit-anchor concept doc](https://olafkfreund.github.io/AIFactory/concepts/audit-anchor)
   and `guides/compliance/iso27001-evidence.md` for ISO 27001 Annex A mapping.
2. Revocation latency bounded by 15-minute access-token TTL (back-
   channel logout deferred).
3. FIPS 140-2/3 modules not validated.
4. ~~No built-in OpenTelemetry distributed tracing.~~ ✅ **Closed by
   Epic #35 #42** — see `## Unreleased § Enterprise v1.1`.
5. ~~Single-replica only (multi-replica via Redis pub/sub deferred).~~
   ✅ **Closed by Epic #35 #40** — multi-replica works with
   `redis.enabled=true` + `workspaces.storage.enabled=true`.
6. ~~LLM-call audit deferred to v3.1 LiteLLM gateway.~~ ✅ **Closed by
   Epic #35 #38** — operator opts in via `litellm.enabled=true`; the
   sub-chart deploys a per-tenant budget / rate-limit / allowlist
   gateway with PII-redacted `audit_logs` rows + Grafana dashboards.
   **Scope caveat:** Claude calls (via the Claude Agent SDK) bypass
   the gateway in v1.1 — the SDK speaks Anthropic-format `/v1/messages`
   which is wire-incompatible with LiteLLM's OpenAI endpoint.
   Claude retains its existing chain-audit coverage (`claude.session.*`
   events signed by the daily anchor from #43); v1.2 closes the
   enforcement gap via an in-process SDK wrapper. See the
   [LiteLLM gateway concept doc](https://olafkfreund.github.io/AIFactory/concepts/litellm-gateway)
   for the full operator walkthrough.

### ✨ Added
- **GitHub PR Review Integration**: End-to-end support for PR reviews including listing, fetching, posting reviews, checking new commits, and viewing logs via dedicated API endpoints.
- **PR Review WebSocket Events**: Real-time progress, completion, and error events via WebSocket for live feedback during PR reviews.
- **PR Action Endpoints**: Support for posting reviews, commenting, merging, assigning, and canceling PRs through backend API.
- **AI-Powered Conflict Resolution**: Enhanced "Fix Conflicts with AI" functionality with real git merge and AI resolution of conflict markers.
- **Task from Chat Feature**: Button in Insights chat to convert conversation into a structured task (title + PRD description) with editable preview.
- **Open in Browser**: New "Open in Browser" button in EditorPage that serves files with correct MIME types and asset URL rewriting.
- **QA Fixer Phase**: Added separate `qa_fixer` phase in phase configuration, allowing independent model and thinking settings.
- **Phase-Scaled Progress**: Monotonically increasing progress percentages across phases (planning 0–20%, coding 20–80%, QA 80–95%, complete 95–100%).
- **Terminal Persistence**: TerminalGrid now remains mounted across view switches to prevent stuck terminals and lost PTY connections.
- **Model & Token Metrics**: Display assistant model name on chat messages and show tokens/sec metrics after each response across all providers.
- **Dark Theme & UI Improvements**: Enhanced folder navigation, keyboard support (Enter/Backspace), HTML preview, progress labels, and overall dark theme consistency.

### 🛠️ Fixed
- **GitHub PR Connection Detection**: Fixed incorrect endpoint call (`window.API.github.checkGitHubConnection` → `window.API.checkGitHubConnection`).
- **AI Merge Conflict Resolution**: Fixed syntax error in `github.py` caused by AI-generated extra closing brace.
- **requireReviewBeforeCoding Sync**: Ensured field is written to `task_metadata.json` when editing tasks.
- **Email Notifications**: Fixed silent failure under legacy token auth by populating default user context.
- **Build Progress & Subtask Status**: Added fallback in `post_session_processing` to detect new commits and force-update status.
- **File Serving 404s**: Resolved `404` errors for `/api/files/serve` by properly staging the endpoint and enabling public access with path-traversal protection.
- **Model Config Loss**: Fixed `UpdateModelConfigRequest` to preserve all fields (provider, profileId, model, thinkingLevel, temperature).
- **Issue-to-Task Creation**: Fixed backend `TaskMetadata` model to include `githubIssueNumber`, `affectedFiles`, and `acceptanceCriteria`.
- **Sidebar Layout**: Restored proper layout and spacing in sidebar components.

### 🔧 Changed
- **Project Renaming**: Renamed from "Claude Code Manager Web" to **AIFactory** across UI, navigation, and documentation.
- **MCP Template Filtering**: Removed redundant and duplicate quick templates (filesystem, fetch, github, gitlab) that conflict with native tools.
- **Hardcoded Model Values**: Replaced inline model/thinking defaults with shared constants to ensure user-configured settings take effect.
- **Git Ignore Safety**: Added `.aifactory-security.json` and `.aifactory-status` to `.gitignore` during project init and unstage during merges.
- **CLI Detection Optimization**: Improved speed using `shutil.which` and `npm package.json` parsing instead of slow Node.js startup (~4s → <50ms).

### 📦 Updated
- **README.md**: Updated project documentation with fixed GitHub URL, removed non-existent files, and added Docker deployment guide.
- **Phase Progress Logic**: Refactored progress logic to prevent backward jumps between phases using defined phase ranges.