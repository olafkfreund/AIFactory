---
status: approved
issue: 1948
intent: intent/2026-10-30-1948-indirect-exec-boundary.md
---

# Spec: indirect execution through agent-written scripts and runners

## Design

The per-command layer (the allowlist, the validators and the egress check)
cannot contain this class. Any base shell, interpreter, test runner, build
script or package lifecycle hook executes agent-written files (see the intent).
Containment has to come from an OS or pod boundary. The design is layered.

### 1. Recommended next slice: turn on the agent OS sandbox (gitops, owner)

Set `AIFACTORY_AGENT_SANDBOX=fs` on the AIFactory Deployment in factory-gitops.
This is the #363 AC1 sandbox in `apps/web-server/server/services/sandbox.py`.
It is not the SDK bash sandbox (`AIFACTORY_BASH_SANDBOX`, which cannot run on
k3d).

- `build_sandboxed_command` wraps the **whole agent subprocess** in bwrap.
  The worktree is the only read-write path. `/usr /bin /sbin /lib /lib64 /etc
  /opt` and `/proc` are read-only, and `/tmp` is a private tmpfs. Nothing else
  is mounted, `$HOME` included. It does not mount a fresh `/proc`, so it runs
  unprivileged in-pod (k3d included).
- Callers on origin/dev: `agent_service.py` ~1272 (in-pod subprocess builds)
  and `agent_spec_creation.py` ~289 (spec creation, which runs in-pod on
  **every** backend).
- The image is ready. The root `Dockerfile` installs `bubblewrap` (~255), and
  kubejob Jobs run the same aifactory image (`build_backend.py` resolves
  `AIFACTORY_BUILD_IMAGE`, then `AIFACTORY_IMAGE`).
- State today: the chart defaults to `fs` (`charts/aifactory/values.yaml:92-103`
  and `templates/deployment.yaml:187`). factory-gitops never sets the variable,
  and the code default is `off`, so production runs without it.

**What it buys against this class:**
- An agent-written script runs inside the same jail as the agent. It can write
  only the worktree and cannot read `~/.aifactory/.token` or `.jwt_secret`,
  because `$HOME` is not mounted. That covers #1931's file-read leg and
  #1932's write leg, for in-pod builds.

**What it does not buy:**
- **Network.** `fs` keeps the network. `strict` needs an outbound proxy.
- **`/proc/<pid>/environ` of same-uid processes.** The PID namespace stays
  shared unless `AIFACTORY_AGENT_SANDBOX_PIDNS=1`, which needs privilege.
- **Spec creation's control-plane state.** For spec creation the "worktree"
  root is the main project, so its `.aifactory/` stays writable.
- **Fails open.** When bwrap cannot create a namespace, `_bwrap_works` logs a
  warning and passes through. Verification must read that log line.

### 2. Follow-up code slice: sandbox the kubejob build

The kubejob Job (the deployed build backend, `AIFACTORY_BUILD_BACKEND=kubejob`)
runs `bash -c "<run.py argv>"` from `build_backend.py` / `core/job_dispatch.py`.
It does **not** call `build_sandboxed_command`, so step 1 does not cover
kubejob builds. The slice wraps the Job's run.py with the same function. The
image already has bwrap. Unprivileged user namespaces under
`capabilities.drop: [ALL]` and `allowPrivilegeEscalation: false` must be
verified on the node, and the fail-open probe will say. The value is lower than
in-pod: the Job pod is already its own boundary, and the main gain is that
`$HOME` and the rest of the Job's filesystem drop out of the agent's reach.

### 3. Document the guarantee (this task's plan)

- In `SECURITY.md` (threat model) and the `security/hooks.py` docstring, state
  that the allowlist, validators and `AIFACTORY_EGRESS_POLICY` are
  **guardrails** against accidental and reflexive misuse, not containment.
  `bash x.sh` with `curl` inside passes all of them.
- Name the boundaries:
  - the agent OS sandbox (steps 1 and 2);
  - the pod;
  - the NetworkPolicies already applied: factory-gitops
    `apps/factory-namespace/manifests/networkpolicy-sandbox-jobs.yaml`
    (Factory#462) and the chart's `templates/networkpolicy-tasks.yaml`;
  - #1932 for control-plane credentials.
- Record that `./<name>` is already refused: the parser passes the basename,
  so the `./` branch in `project/__init__.py:93` is never reached.
  #1948's original `./evil.sh` premise was therefore wrong, and the
  `./<name>` entries `_detect_shell_scripts` adds are dead. Delete them in a
  follow-up (separate issue). Removing them changes nothing that is allowed.

### 4. Pin the residual with a test (this task's plan)

`validate_command` allows `bash x.sh`, `make test` and `npm test` for a
repo with a Makefile and a package.json. A change that claims to close the
class must flip these deliberately.

## Alternatives rejected

- **A. Allow only scripts and targets present at the approved plan's base
  ref.** It misses `bash x.sh`/`sh x.sh` (base commands), interpreters, test
  runners executing agent-written tests and conftest, package lifecycle hooks,
  and an agent editing the recipe of a tracked target. Pinning recipe content
  too would refuse every build that legitimately adds a target. That is a lot
  of code for false assurance.
- **B. Refuse indirect execution when `AIFACTORY_BASH_SANDBOX` is off.** That
  flag is off on every deployed factory (k3d cannot host the SDK sandbox,
  #363), so this would refuse `make`, `npm test` and `bash` in production.
  It also cannot catch interpreters and test runners. Gating on
  `AIFACTORY_AGENT_SANDBOX` instead adds nothing: once the agent runs inside
  that jail, so does everything it starts.
- **Count `AIFACTORY_EGRESS_POLICY=allowlist` as containment.** It is a
  command-layer check that a script bypasses. It stays a guardrail (step 3).
- **Recipe inspection** (parse Makefile and package.json scripts). Rejected
  for the same reason as A: recipes call other files, so the surface is
  unbounded.

## Risks

- **HTTPS exfiltration stays possible.** Both applied NetworkPolicies allow
  443/tcp to any public address. A script can therefore send the worktree, or
  any env it can read, over HTTPS. This lasts until #1932 removes
  control-plane credentials from the agent's reach and egress goes through a
  proxy or FQDN policy. Steps 1 and 2 do not change this.
- **Step 1 fails open.** If bwrap cannot create a user namespace on the node,
  the sandbox passes through with a warning. The gitops change must be
  verified from that log line, not assumed.
- **Step 1 could break a tool** that needs a path outside the bound set (for
  example a cache under `$HOME`). Roll back by unsetting the variable.
- **The doc may read as a waiver.** It names the containing boundaries and the
  tracking issues, and the pinned test fails if the behaviour changes silently.

## Verification

- **Step 1 (owner, gitops):**
  - After rollout, the web-server log has no "bwrap ... cannot create a
    namespace" or "probe failed" warning.
  - A build's spawn argv starts with `bwrap` (`stamp_spawn` records the real
    argv).
  - From inside a build, `ls ~/.aifactory` fails.
- **Step 2:** a Job manifest test shows the run.py command wrapped, plus one
  cluster run with the probe result logged.
- **Steps 3 and 4:**
  - `SECURITY.md` and the `hooks.py` docstring carry the text above.
  - A new test asserts `bash x.sh`, `make test` and `npm test` stay allowed.
  - The existing hook tests stay green.
