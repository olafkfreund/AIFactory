---
status: approved
issue: 1948
intent: intent/2026-10-30-1948-indirect-exec-boundary.md
---

# Spec: indirect execution through agent-written scripts and runners

## Design

**Chosen: accept the residual at the command layer, document it, and contain
it at the pod boundary (#1932).**

1. Record in `SECURITY.md` (threat model section) and in the docstring of
   `security/hooks.py` that the allowlist and per-command validators are a
   guardrail against accidental and reflexive misuse. They are not an
   execution boundary. Any allowed interpreter, runner, test tool or build tool
   executes agent-written files without per-command validation.
2. Name the boundaries that do contain it: the bwrap sandbox where it runs
   (`AIFACTORY_BASH_SANDBOX`, on by default for local dev), and in clusters
   the pod plus default-deny egress NetworkPolicy. #1932 removes control-plane
   credentials from the agent's reach and is the work that makes the pod
   boundary real. Link #1948 to #1932 as a dependent residual.
3. Pin the residual with a test: `validate_command("bash x.sh")` is allowed
   for a base profile. A later change that claims to close this class must
   flip it deliberately, not by accident.
4. Keep `_detect_shell_scripts` as it is. Removing `./<name>` alone gives no
   containment while `bash <name>` is a base command, and it would break
   tracked repo scripts.

Follow-up work, in order (separate issues, not this task): #1932 slices;
default-deny egress NetworkPolicy for agent pods where not already applied;
consider `AIFACTORY_EGRESS_POLICY=allowlist` as the in-process compensating
control in clusters.

## Alternatives rejected

- **A. Allow only scripts and targets present at the approved plan's base ref**
  (`git ls-files` at build start, as #1948 first suggested). Rejected. It does
  not cover `bash x.sh`/`sh x.sh` (base commands), interpreters
  (`node x.js`, `python x.py` where the stack allows them), test runners
  executing agent-written tests and conftest, package lifecycle hooks, or an
  agent editing the recipe of a base-tracked target. Pinning recipe content
  too would refuse every build that legitimately adds a target or script.
  The result is a lot of code that gives false assurance.
- **B. Run indirect execution only inside bwrap and refuse it when the sandbox
  is off.** Rejected as the primary fix. The sandbox is off on every deployed
  factory (`AIFACTORY_BASH_SANDBOX=false` in the gitops manifests, because
  k3d cannot mount /proc for bwrap, #363). B would therefore refuse
  `make`/`npm test`/`bash` in production and break builds. It still would
  not cover interpreters and test runners, which are not "indirect" by
  syntax. Revisit when a gVisor-capable runtime makes bwrap available in
  clusters (#363). At that point the sandbox already contains the class, and
  B adds nothing beyond turning it on.
- **Classify runner commands with recipe inspection** (parse Makefile recipes,
  package.json scripts). Rejected for the same reason as A. Recipes call other
  files, and the parser would chase an unbounded language surface.

## Risks

- Accepting the residual leaves an agent able to run any command its pod can
  run until #1932 lands. That is already true through `bash x.sh` and test
  runners. The doc makes it explicit instead of implying the deny list stops it.
- A reader may treat the doc as a waiver. Mitigation: it names the containing
  boundary and the tracking issue, and the pinned test fails if the behaviour
  changes silently.

## Verification

- `SECURITY.md` and the `security/hooks.py` docstring state the guarantee and
  the boundary, and link #1948, #1932 and #363.
- A new test (for example `tests/test_command_sandbox.py`) asserts the residual:
  `bash x.sh` is allowed for a base profile.
- No change to allowed or denied commands, so the existing hook tests stay green.
