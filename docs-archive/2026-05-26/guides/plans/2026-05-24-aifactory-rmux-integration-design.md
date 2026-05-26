# AIFactory rmux Integration — v1 Design Spec

> Created: 2026-05-24
> Status: Approved (pending Epic creation)
> Authors: olafkfreund
> Approval gate: super-brainstorm interview, 4 decisions locked
> Related Epics: #26 (v1.0 enterprise pilot), #35 (v1.1 enterprise hardening)

## 1. Summary

Integrate [rmux](https://github.com/Helvesec/rmux) as an **opt-in dev/demo capability** in AIFactory, providing per-task Live Agent Console mirroring (read-only by default, explicit Attach mode with audit), plus Playwright-style E2E tests of AIFactory itself driven through rmux. v1 is intentionally narrow: F1 + F7 from the candidate menu, deferring F2/F3/F4/F5 to a future production-promotion track.

Why now: AIFactory operators (and demo audiences) cannot today *see* what an agent is doing in real time — only after-the-fact logs. rmux's `web-claude-demo` reference shows browser ↔ pane mirroring works in ~649 lines of Rust; we adapt the pattern in Python and integrate into the existing `TaskDetail` UI.

Why opt-in: rmux is at v0.3.0, published 8 days before this spec. README explicitly warns "fresh public preview, bugs are expected." Putting a fresh dependency in the critical path of the v1.0 bank pilot (Epic #26) is unwarranted risk. The opt-in posture ships the visible demo win without exposing pilot delivery to the supply-chain risk.

## 2. Locked decisions

| # | Decision | Value |
|---|---|---|
| 1 | Production-readiness posture | Dev/demo-only opt-in for v1; revisit promotion to production-default for v1.1 (Epic #35) if rmux matures to v0.4+ |
| 2 | Timing relative to v1.0 pilot | Scheduled AFTER v1.0 ships (Epic #26 has no buffer to spare). rmux Epic runs in the v1.0→v1.1 gap or interleaves with v1.1 |
| 3 | Scope cut from F1–F7 candidate menu | F1 (Live Agent Console) + F7 (Playwright E2E); F2/F3/F4/F5 deferred; F6 (Windows) falls out free as part of F1 |
| 4 | Mirroring mode | Read-only default; explicit Attach button + `POST /attach` with `audit.action=console.attach` audit row; not interactive-by-default |

**Implicit defaults (rolled into design rather than interview):**

- Bridge implementation: **pure Python in-process** using `rmux pipe-pane` → Unix FIFO → WebSocket → xterm.js. No Rust sidecar.
- rmux installation: **bundled static binary** (~10 MB) at `/usr/local/bin/rmux` in the runtime Docker image, pinned by SHA-256 against the v0.3.0 GitHub Release.
- Failure mode: feature flag `AIFACTORY_RMUX_ENABLED` (default `false`). When off, existing `pty.openpty()` path is unchanged. When on but daemon unreachable, fall back to existing path and surface a UI banner.
- Session naming: `aifactory-task-<spec_id>`; cwd = task's worktree path; reaped on task completion/discard.
- WebSocket auth: existing `TokenAuthMiddleware` + task-access check (org membership + project access).

## 3. Architecture

```
                                 ┌────────────────────┐
                                 │  Browser           │
                                 │  TaskDetail page   │
                                 │  ┌──────────────┐  │
                                 │  │ Live Console │  │
                                 │  │ tab (xterm)  │  │
                                 │  └──────┬───────┘  │
                                 └─────────┼──────────┘
                                  WS, auth-gated, per-task
                                           │
                                           ▼
                  ┌──────────────────────────────────────────────┐
                  │  aifactory-web (FastAPI)                     │
                  │  GET  /api/tasks/{id}/agent-console/ws       │
                  │  POST /api/tasks/{id}/agent-console/attach   │
                  │                                              │
                  │  apps/web-server/server/rmux/                │
                  │   ├─ wrapper.py    (subprocess rmux CLI)     │
                  │   ├─ session.py    (per-task lifecycle)      │
                  │   └─ bridge.py     (FIFO ↔ WebSocket)        │
                  └────────────────┬─────────────────────────────┘
                                   │ subprocess: rmux <cmd>
                                   ▼
                  ┌──────────────────────────────────────────────┐
                  │  rmux daemon  (bundled static binary)        │
                  │  socket: /var/run/aifactory/rmux/sock 0600   │
                  │  ┌──────────────────────────────────────────┐│
                  │  │ session: aifactory-task-<spec_id>        ││
                  │  │   pane 0: agent process (cwd=worktree)   ││
                  │  │   pipe-pane -o → /var/run/aifactory/     ││
                  │  │                  panes/<spec_id>.fifo    ││
                  │  └──────────────────────────────────────────┘│
                  └──────────────────────────────────────────────┘
```

### 3.1 New Python modules (`apps/web-server/server/rmux/`)

#### `wrapper.py` (~150 LOC)

Async subprocess wrapper around the `rmux` CLI. One class `RmuxWrapper` with methods:

- `async def ensure_daemon() -> None` — start the daemon if not running; idempotent
- `async def new_session(name: str, cwd: str, cmd: list[str]) -> None` — `rmux new-session -d -s <name> -c <cwd> <cmd...>`
- `async def kill_session(name: str) -> None` — `rmux kill-session -t <name>`
- `async def send_keys(name: str, keys: str) -> None` — `rmux send-keys -t <name>:0 <keys>`
- `async def send_text(name: str, text: str) -> None` — `rmux send-keys -t <name>:0 -l <text>` (literal text, no key parsing)
- `async def list_sessions() -> list[str]` — `rmux list-sessions -F '#{session_name}'`
- `async def pipe_pane(name: str, fifo_path: str) -> None` — `rmux pipe-pane -t <name>:0.0 -o "cat >> <fifo_path>"`
- `async def capture_pane(name: str) -> str` — `rmux capture-pane -t <name>:0.0 -p` (fallback for pipe-pane if it doesn't behave)

Errors: distinguish "daemon not running" (recoverable, return `RmuxDaemonError`), "session not found" (`RmuxSessionError`), "rmux binary not installed" (`RmuxNotInstalledError`). Caller decides what to do.

#### `session.py` (~80 LOC)

Per-task lifecycle. Two methods on a module-level singleton:

- `async def create_for_task(spec_id: str, worktree_path: str, agent_cmd: list[str]) -> Path` — creates the rmux session, sets up the FIFO at `/var/run/aifactory/panes/<spec_id>.fifo`, starts `pipe-pane`, returns the FIFO path
- `async def reap_for_task(spec_id: str) -> None` — kills the rmux session and removes the FIFO

Holds a thread-safe registry `task_id -> (session_name, fifo_path)`. Called from `agent_service.py` when a task starts/ends.

#### `bridge.py` (~150 LOC)

FastAPI WebSocket route and helpers. Two endpoints:

- `WEBSOCKET /api/tasks/{task_id}/agent-console/ws` — opens a one-way byte stream from the FIFO to the browser. Reads FIFO in chunks (4 KiB), forwards bytes to WS. On client message, if the session is in attach mode, forward bytes to `wrapper.send_keys`; if not, drop with a warning.
- `POST /api/tasks/{task_id}/agent-console/attach` — flips the session into attach mode. Writes one `AuditLog` row with `action=console.attach`, `user_id`, `org_id`, `resource_type=task`, `resource_id=task_id`, `ip`. Subsequent WS messages from that connection now accept input.

Attach mode is per-WebSocket-connection, not per-session — multiple concurrent viewers can all be read-only while one is attached. **For v1: at most one attached connection per session at any time.**

**Concrete enforcement (avoids the obvious race):**

1. When a client opens the WS, the server generates a `connection_id` (UUID v4) and sends it as the first message
2. `POST /attach` requires `{"connection_id": "..."}` in the body
3. `session.py` holds an `asyncio.Lock` per `(session_name)`. The POST handler acquires the lock, checks `session.attached_connection_id is None`, sets it to the request's `connection_id`, writes the audit row, releases the lock. A second concurrent POST waits on the lock, finds `attached_connection_id` already set, returns `409 Conflict`
4. The WS task polls `session.attached_connection_id == self.connection_id` to decide whether to accept inbound bytes; the WS itself doesn't receive a separate "flip" message — the WS sees the flag change atomically via the session state
5. On WS disconnect or `POST /detach`, the handler clears `attached_connection_id` (under the same lock) and writes the `console.detach` audit row

The lock + connection_id binding eliminates the race the reviewer flagged: there's no window where a second `POST /attach` can succeed before the first attaches.

**Key translation for attach mode:** browser keypresses (arrow keys, Ctrl-C, Ctrl-D, paste, etc.) are sent over the WS as binary frames containing the raw byte sequences xterm.js produces (e.g., `\x1b[A` for up-arrow, `\x03` for Ctrl-C). The bridge forwards these to `rmux send-keys -t <name>:0` with the `-l` flag stripped for control bytes (rmux's `send-keys` accepts raw byte input without `-l`). Plain printable text uses `send-keys -l <text>`. The bridge inspects the first byte to decide the mode.

### 3.2 `agent_service.py` integration

The 4 existing `pty.openpty()` call sites (lines 815, 898, 2359, 2580) get wrapped:

```python
if os.environ.get("AIFACTORY_RMUX_ENABLED") == "true":
    try:
        await rmux_session.create_for_task(spec_id, worktree_path, agent_cmd)
        # ... agent runs inside the rmux pane; we still need a way to capture
        # output for the existing log-stream path, which pipe-pane gives us
    except RmuxNotInstalledError, RmuxDaemonError:
        logger.warning("rmux requested but unavailable; falling back to pty.openpty")
        # existing pty.openpty path
else:
    # existing pty.openpty path
```

The existing log-stream WebSocket path is preserved. Live Console is *additional*, not replacement: the same agent process feeds both the existing log stream and the rmux pane.

**How both streams stay in sync when rmux is on:**

When rmux owns the agent's PTY, the agent's stdout/stderr is conflated into a single ANSI-rich byte stream available via `pipe-pane`. To keep the existing log-stream WebSocket's contract unchanged (it consumes plain text, line-oriented, no ANSI), the bridge does the following:

1. The FIFO produces the raw ANSI byte stream → fed directly to the **Live Console WS** (xterm.js renders ANSI natively)
2. A second consumer of the same FIFO (`session.py` runs a tee task) runs the bytes through an ANSI-stripping filter and a line-buffer, then emits to the **existing log-stream emit path** with the same `(timestamp, stream="agent", line)` shape it has today
3. stderr/stdout demultiplexing is **lost** when rmux is on — the existing log-stream marks all lines as `stream="agent"` rather than separating `stderr`/`stdout`. This is the only user-visible format drift; documented as a known limitation of the rmux-enabled path

**Mid-task daemon failure policy (v1):** if the rmux daemon crashes after a task has started, the agent's PTY (owned by the daemon) is destroyed and the agent process is killed. v1 does NOT attempt to recover the agent in-flight — the task is marked failed, a banner surfaces in the UI, and the user can re-run the task. Re-run starts fresh; if the daemon is still unreachable, the fresh task falls back to `pty.openpty()` as described above. Watchdog-based recovery is explicitly deferred to v1.1 or later.

Total `agent_service.py` diff: ~30 lines added, no removals.

### 3.3 Frontend addition (`apps/frontend-web/src/components/task-detail/AgentConsole.tsx`)

One new React component using the existing `Terminal` xterm.js wrapper used by the user-shell feature. Connects to `/api/tasks/{id}/agent-console/ws`. Top bar shows:

- "READ-ONLY" badge by default
- "Attach Interactive Shell" button → confirms with a modal ("This will write an audit log entry and let you type into the agent's worktree shell. Continue?") → POST to `/attach` → switch component into bidirectional mode → badge changes to "ATTACHED (auditor: your-name)"

The tab is added to `TaskDetail`'s tab list alongside Logs / Plan / QA. Tab is only rendered when `AIFACTORY_RMUX_ENABLED=true` is reported by `/api/health` (capability discovery). Total: ~120 LOC + small `TaskDetail.tsx` edit.

### 3.4 Bundled rmux binary

`Dockerfile` adds — **gated by build-arg so the bank pilot image (`WITH_RMUX=false`) contains no rmux binary at all**:

```dockerfile
ARG WITH_RMUX=false
ARG RMUX_VERSION=0.3.0
ARG RMUX_SHA256_AMD64=<pinned>
ARG RMUX_SHA256_ARM64=<pinned>
RUN if [ "$WITH_RMUX" = "true" ]; then \
      arch="$(dpkg --print-architecture)"; \
      case "$arch" in \
        amd64) target="x86_64-unknown-linux-musl"; sha="${RMUX_SHA256_AMD64}" ;; \
        arm64) target="aarch64-unknown-linux-musl"; sha="${RMUX_SHA256_ARM64}" ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
      esac; \
      curl -fsSL "https://github.com/Helvesec/rmux/releases/download/v${RMUX_VERSION}/rmux-${target}.tar.gz" -o /tmp/rmux.tar.gz; \
      echo "${sha}  /tmp/rmux.tar.gz" | sha256sum -c -; \
      tar -xzf /tmp/rmux.tar.gz -C /usr/local/bin/ rmux; \
      chmod +x /usr/local/bin/rmux; \
      rm /tmp/rmux.tar.gz; \
    else \
      echo "rmux integration not built (WITH_RMUX=false)"; \
    fi
```

CI builds two images per release: `aifactory:vX` (default, `WITH_RMUX=false`, used by bank pilot) and `aifactory:vX-rmux` (`WITH_RMUX=true`, used by dev/demo). Bank-pilot image's Trivy report and SBOM contain no rmux components. Both images are signed by cosign. Documented in the SOC2 third-party-component inventory (Epic #26 issue #34).

The Helm chart adds `values.yaml: rmux.enabled` (default `false`) which sets `AIFACTORY_RMUX_ENABLED=true` env var and mounts an `emptyDir` at `/var/run/aifactory/{rmux,panes}` for the daemon socket and FIFOs. Setting `rmux.enabled=true` against a `WITH_RMUX=false` image causes the pod to log a warning and silently leave the feature disabled — defensive when operators point the wrong image at the toggle.

**Multi-replica caveat (v1 limitation):** when `rmux.enabled=true`, the chart pins `replicas: 1` for the web Deployment regardless of HPA settings. Each replica would get its own daemon and FIFOs, with no cross-replica session visibility — a user routed to a different replica would see an empty Live Console. Multi-replica rmux requires v1.1's Redis-pub/sub work (#40) and a shared state store; out of scope here.

## 4. Phase plan (R0–R4, ~1.5–2 engineer-weeks)

| Week | Phase | Deliverable | Verification |
|---|---|---|---|
| 1 (d1) | **R0a** pipe-pane go/no-go spike | 1-hour smoke test: does `rmux pipe-pane -o` produce a continuous byte stream usable for live mirroring? | **Hard gate.** PASS → continue R0b. FAIL → pause Epic, escalate, decide between (a) wait for upstream, (b) commit to capture-pane polling redesign as a separate 1-week task, (c) close Epic |
| 1 (d1–2) | **R0b** Python rmux-CLI wrapper | `wrapper.py` with full async API + pytest against a real rmux daemon | Round-trip test: new-session → send-text → capture-pane → kill-session passes |
| 1 (d3–5) | **R1** Per-task session lifecycle + bridge | `session.py` + `bridge.py`; `agent_service.py` hook; FIFO + WS plumbing | Agent task with rmux on produces bytes streamed to a WS client; flag-off regression test verifies existing behavior unchanged |
| 2 (d1–3) | **R2** Frontend Live Console tab + Attach | `AgentConsole.tsx`; Attach button + confirmation modal + `POST /attach` audit row | End-to-end click-through: run a Claude-planner + Ollama-coder task → watch coder pane live → Attach → type `echo test` → see it execute |
| 2 (d4) | **R3** Bundle rmux binary + Helm toggle | Dockerfile update with SHA-256 pin; chart `values.yaml: rmux.enabled` toggle; Trivy scan still clean | Image builds; `rmux --version` works inside container; flag off by default; flag on works in kind |
| 2 (d5) | **R4** Playwright E2E (F7) | Three scenarios in `tests/e2e/rmux/`: (a) session lifecycle, (b) read-only WS stream, (c) Attach round-trip | All three pass headless in CI; flaky-test budget = zero |

Buffer: none assumed. If R1 or R2 slips, R4 drops to a v1.x follow-up issue (the F7 work doesn't gate F1's demo value).

## 5. Security & threat model

The integration introduces three new surfaces:

### 5.1 Shell-access-via-WebSocket

The attach mode is literally "this authenticated user can type into the agent's worktree shell, executing as the same OS user as the agent." Mitigations:

- WebSocket goes through the existing `TokenAuthMiddleware`; unauthenticated connections refused
- Explicit task-access check: caller's `org_id` must match the task's `org_id`, and they must be a member of the task's organization with `console:attach` permission (new permission; default-granted to admins, opt-in for regular users via RBAC)
- Read-only is the **default**; attach requires a deliberate `POST /attach` that the frontend gates behind a confirmation modal
- Each attach event writes an `AuditLog` row (`action=console.attach`, `user_id`, `org_id`, `resource_type=task`, `resource_id`, `ip`, `user_agent` in `details_json`)
- Detach is also audited (`action=console.detach`)
- One attach at a time per session — second attempt gets `409 Conflict`

### 5.2 rmux daemon as a new attack surface

The daemon listens on a Unix socket. Mitigations:

- Socket path `/var/run/aifactory/rmux/sock`, mode `0600`, owned by the same uid as the web-server process
- No TCP listener; no Windows Named Pipe (we're targeting Linux containers; Windows isn't in v1 scope despite F6 falling out free)
- Daemon spawned only when `AIFACTORY_RMUX_ENABLED=true`; not present in flag-off installs
- Daemon process subject to the same PSS-restricted security context as the web-server pod (no extra capabilities)
- `unsafe-check.sh`-style audit of the rmux binary at adoption time (rmux ships with one in `scripts/`)

### 5.3 Bundled binary supply chain

A 10 MB precompiled Rust binary in the runtime image. Mitigations:

- Pinned to v0.3.0 release tag + SHA-256 verified at build time (build fails if checksum mismatches)
- Trivy continues to scan the full image; HIGH/CRITICAL fails the build
- rmux binary listed in the SOC2 third-party-component inventory (Epic #26 issue #34: P7 evidence + docs)
- Renovate config updated to monitor new rmux releases; upgrade decisions deliberate (not auto-merged)

### 5.4 What this integration does NOT change

- v1.0 (Epic #26) attack surface: rmux flag is **off by default**, including in the bank pilot deployment. Bank pilot ships v1.0 with rmux binary *not even bundled* (the Dockerfile addition is gated by a build-arg `WITH_RMUX=false` for the pilot image).
- v1.0 SOC2 evidence: rmux's absence in the pilot image is documented; promotion to default-on is a v1.1 decision (Epic #35) requiring its own threat-model review.

## 6. Risks & open questions

### Risks

| Risk | Mitigation |
|---|---|
| rmux v0.3.x daemon crashes / hangs / leaks corrupt an agent task | Feature is opt-in. Production runs on existing PTY path. *Task-start* failure → fallback to `pty.openpty()` and UI banner. *Mid-task* failure → task marked failed, banner surfaces, user re-runs (§3.2 mid-task policy). No silent in-flight recovery in v1 |
| `pipe-pane` semantics differ from tmux | **Hard go/no-go gate at end of R0 day 1:** if the pipe-pane smoke test fails (no continuous byte stream, or unusable buffering), the Epic is **paused** and escalated before R1 starts. The previously-listed "fall back to `capture-pane` polling" is NOT a drop-in replacement — `capture-pane` returns visible-pane snapshots, not byte streams, and would require a synthetic-diff bridge design + scrollback-loss acceptance. If pipe-pane fails, the team takes a deliberate decision: (a) wait for upstream fix, (b) commit to polling-mode redesign as a separate ~1-week task, or (c) close the Epic |
| Bundle adds ~10 MB to a Chainguard-minimized image | Acceptable for opt-in dev image. Bank pilot image built with `WITH_RMUX=false` skips the bundle |
| ANSI from Ollama agents has weird control sequences xterm.js mishandles | xterm.js handles all common sequences. If a specific sequence breaks rendering, sanitize at the bridge (drop OSC 52 clipboard escapes, etc.) |
| Attach mode used to bypass agent's planned actions, breaking task semantics | Acceptable for dev tooling; audit row preserves what the human did. If problematic, lock attach behind a separate role (`console:attach`) — already designed in §5.1 |
| rmux upstream breaking changes between releases | Renovate alerts; deliberate upgrades; CI runs against pinned version |

### Open questions (resolve at start of R0)

- Does `rmux pipe-pane` work as a continuous byte stream like tmux's, including across detach/reattach? → **1-hour spike, day 1 of R0 — hard go/no-go gate**
- Does `rmux pipe-pane -o` use line buffering or unbuffered output? Line buffering would hide partial-line agent output (progress bars, spinners) until newline — degrading the "live" promise. → verify in spike, document the result
- Does rmux daemon survive `SIGTERM` to the web-server process? → verify in R0; add explicit lifecycle management if not
- Does the existing xterm.js terminal component accept a new WebSocket source cleanly (so we reuse it rather than vendor a second instance)? → read `apps/web-server/server/websockets/terminal.py` consumer + adapt, R1
- What's the actual rmux v0.3.0 musl-linux release URL + SHA-256 for both amd64 and arm64? → fetch at R3 start
- Does running rmux daemon as a non-root user uid 65532 (matching the Chainguard runtime) work? → smoke test in R3
- Does the existing RBAC schema accommodate a new `console:attach` permission without a migration, or is a schema change needed? → check at R2 start; if migration needed, lift into R0/R1 timeline (RBAC migrations are higher-risk than frontend work)

## 7. Acceptance criteria (Epic close gate)

- [ ] `AIFACTORY_RMUX_ENABLED=true` enables the feature; `false` (default) leaves existing behavior byte-for-byte unchanged (regression test in CI compares log-stream output against a recorded baseline for a deterministic task)
- [ ] `rmux` binary bundled at `/usr/local/bin/rmux` only when built with `WITH_RMUX=true`; SHA-256 verified at build time; build fails on checksum mismatch
- [ ] Bank-pilot image (`WITH_RMUX=false`) contains no rmux binary and no rmux-related dependencies: `ls /usr/local/bin/rmux` returns non-zero; Syft SBOM contains no rmux entries; image size delta vs. baseline ≤ 100 KiB (allows for the Python `rmux/` module bytecode only)
- [ ] Starting a task with `AIFACTORY_RMUX_ENABLED=true` creates an rmux session `aifactory-task-<spec_id>` with cwd=worktree
- [ ] WebSocket `/api/tasks/{id}/agent-console/ws` streams ANSI bytes from the pane in read-only mode; **bytes appear in browser within 200 ms of pane write under nominal load**
- [ ] With rmux on, the existing `/api/tasks/{id}/logs/ws` endpoint shows the same human-readable content as with rmux off (ANSI stripped, line-buffered, no format drift) — regression test compares byte-equivalent log output for a deterministic task between flag-on and flag-off runs
- [ ] `POST /api/tasks/{id}/agent-console/attach` with `{"connection_id": "..."}` succeeds, writes one `AuditLog` row with `action=console.attach`, and switches *that connection's* WS into bidirectional mode
- [ ] Race test: 1000 concurrent `POST /attach` calls against an unattached session result in exactly one 200 OK + 999 409 Conflict
- [ ] WS disconnect or `POST /detach` writes `AuditLog` row with `action=console.detach` and clears the attached connection
- [ ] Browser can type after attach; control bytes (arrow keys, Ctrl-C, paste) arrive in the agent shell intact; **input round-trip latency ≤ 100 ms** (manual measurement)
- [ ] Task completion / discard reaps the rmux session (`rmux list-sessions` shows no orphans after a CI test run)
- [ ] **Task-start daemon failure:** task runs via existing PTY path; UI banner explains
- [ ] **Mid-task daemon failure:** task is marked failed; banner surfaces; re-run starts cleanly
- [ ] Frontend Live Console tab is hidden when `/api/health` reports rmux capability as disabled (no broken UI surface in flag-off mode)
- [ ] Playwright E2E tests for read-only stream + attach round-trip + session-lifecycle + race-test pass headless in CI
- [ ] Trivy scan of the `aifactory:vX-rmux` image: no HIGH/CRITICAL introduced over baseline
- [ ] rmux binary documented in the SOC2 third-party-component inventory (cross-references Epic #26 issue #34) — entry distinguishes which AIFactory image variants include it
- [ ] Helm chart with `rmux.enabled=true` against a `WITH_RMUX=false` image logs a startup warning and leaves the feature off (defensive against operator misconfiguration)

## 8. References

- rmux repo: <https://github.com/Helvesec/rmux> (v0.3.0)
- Reference demo: <https://github.com/Helvesec/rmux-demos/tree/main/web-claude-demo>
- Orchestration reference (for future F3 if ever pursued): <https://github.com/Helvesec/rmux-demos/tree/main/demo-orchestration>
- Existing PTY infra: `apps/web-server/server/pty/{manager,session}.py`, `apps/web-server/server/websockets/terminal.py`
- Agent PTY call sites: `apps/web-server/server/services/agent_service.py:815,898,2359,2580`
- Related Epics: #26 (v1.0 enterprise pilot), #35 (v1.1 enterprise hardening)
- Spec for v1.0: `guides/plans/2026-05-24-aifactory-enterprise-v1-design.md`
