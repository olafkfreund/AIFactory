---
status: approved
issue: 1984
intent: intent/2026-10-31-1984-job-pushback-trust.md
---

# Spec: refuse gated tasks on subprocess, and never trust what a build Job pushes back

Decision recorded (owner, 2026-10-31, on #1932): option B, agents run only in
Jobs and the subprocess backend is local-dev only. Rejected by that decision:
a custom seccomp profile for bwrap, gVisor, and isolating in-pod agents under a
separate uid as the main fix (a separate uid stays slice D, for anything left
in-pod).

## Design

### Part 1: slice B, refuse gated tasks on subprocess

`apps/web-server/server/services/build_backend.py`:

```python
class GatedTaskRefusedError(InputRejectedError): ...

def subprocess_gate_refusal(spec_dir: Path | None, mode: str | None = None) -> str | None
```

Returns `None` unless the raw backend is `subprocess` (unset counts as
subprocess; kubejob and unknown values are handled by `backend_config_error`).
On subprocess it returns the refusal sentence when any of these hold, read from
the control plane's `task_metadata.json` before any agent session:

- the `mode` argument is `"quick"`;
- `requireReviewBeforeCoding` is true, or `mode == "quick"` in the metadata
  (the same rule as `review.state.requires_review_before_coding`);
- `reviewTier == "blocking"`.

A metadata file that exists but cannot be parsed is refused too (fail closed).
A missing file, or `spec_dir is None`, means no gates: allowed.

Message: `"This task is review- or tier-gated, so this task needs the kubejob
backend: in-pod agents can rewrite its gate state (#1932). Set
AIFACTORY_BUILD_BACKEND=kubejob, or clear the gate for local dev."`
`GatedTaskRefusedError` subclasses `InputRejectedError`, so the routes'
existing `client_error(...)` returns that sentence verbatim (they catch
`Exception` and map to 500; no route change).

Call sites:

1. `AgentService.start_task_execution` (`services/agent_service.py` ~807),
   after the running/queued guards and before admission. Every build entry
   (start, plan approval, from-issue, auto-fix, queue drains, which inherit what
   was admitted) funnels through it, and refusing before the queue means a
   gated task is never parked and dropped later.
2. `start_spec_creation` (`services/agent_spec_creation.py` ~86), next to the
   #1942 config check. On subprocess, spec_runner chains the build in-pod.

### Part 2: #1984, every artifact the build Job hands back

Ground truth: on the packed path the Job pushes these keys to object storage
from `cli/main.py` ~530-565 (`role="build"`, keyed by `spec_id` only), and the
Job's store credential can write the key of ANY spec. On the co-mount path the
Job writes the PVC subtree `.aifactory/worktrees/builds/<spec>`; the control
plane copies single files back from the task worktree spec dir. So the fix is
at the control-plane READERS and decisions, which covers both paths and any
writer.

| # | Artifact | How it reaches the control plane | Control-plane use | Decision |
|---|---|---|---|---|
| 1 | review pause (`status: human_review` + `reviewReason` `plan_review`/`injection_scan` in the plan) | `maybe_fetch_plan` in `_kubejob_review_reason` | Skips completion, TFactory handoff and PR endgame; writes `task_control` `human_review` (which drops a plan approval) | **Recompute.** The claim is necessary, never sufficient. `plan_review` counts only when the control plane's own `task_metadata.json` requires review (`requires_review_before_coding`), the task is not solo (`is_solo_mode_enabled_for_spec`), and the control plane's `review_state.json` has no valid approval for the plan. `injection_scan` counts only when the control plane re-runs `security.content_scan.run_scan_gate` on its spec dir and the build worktree (`.aifactory/worktrees/builds/<spec>` if present, else the project) and it blocks, and there is no valid approval. Otherwise the claim is ignored (logged) and the run goes through the normal completion, whose evidence gate (#1070) fails a run with no commit. A dropped claim lands there too, so a real pause can never become a handoff. |
| 2 | `memory.tar.gz` | `maybe_fetch_memory` (completion, completion_orchestration) | Extracted into the spec dir, then pooled into project memory | **Ignore everything outside `memory/`.** Extract only members named `memory` or under `memory/` that are regular files or directories (no links, devices or other paths), still with `filter="data"`. A forged archive can no longer write `task_control.json`, `review_state.json`, `task_metadata.json` or `context/task_contract.json`. Memory content stays untrusted prompt context, like the code itself. |
| 3 | `implementation_plan.json` as a TFactory / merge contract | `maybe_fetch_plan` overwrites the control-plane plan; `load_task_contract` falls back to it | Legacy (unsigned) tasks: the `tfactory` / `contract_version` / `approval` blocks become the contract sent to TFactory and read by `pr_endgame` | **Ignore.** `load_task_contract` reads only `context/task_contract.json`, which the trusted-plan ingest writes on the control plane and no Job push writes (after row 2). The plan fallback was for plans installed before that stash existed. |
| 4 | `implementation_plan.json` subtasks, status, `qa_signoff` | same | Board progress and status; merger PR body "QA sign-off" line; the plan a human reviews; approval hash (#1924) | **Display only, labelled.** No decision reads subtask counts on the kubejob path (the evidence gate counts commits on the branch). The merger body says the QA sign-off and gate evidence are "reported by the build, not verified by the control plane". The plan a human reviews is the agent's output by design; the approval binds its hash, so a later change invalidates it. |
| 5 | `.trailing_gates_done` | `maybe_fetch_gate_marker` (packed), `sync_spec_file_from_worktree` (co-mount) | Merger PR body "Gate evidence" line only | **Display only, labelled** (row 4's label). It gates no status, handoff or merge; PR CI is the real check. |
| 6 | `token_usage.json` | `maybe_fetch_usage` | Usage in the completion event / usage snapshot (CFactory spend, soft budget alert) | **Self-reported telemetry, not authority.** Already bounded: at most 1 MiB, a JSON object, and accepted only when its total is higher than the local copy. Nothing decides on it. No change. |
| 7 | `task_logs.json` | `maybe_fetch_task_logs`, only when the control plane has no copy | Board phase status; orphan reaper input (which also needs no live job row and a stale spec dir) | **Ignored when the control plane has its own.** The control plane creates `task_logs.json` at dispatch (`_kubejob_log_writer`), so the fetch is a no-op in the normal case, and the terminal status is written by the control plane (`_record_kubejob_terminal`, `task_control`). No change. |
| 8 | build branch | `git push` to origin | Evidence gate (commit count), TFactory handoff, PR | **The product, not authority.** Verified by TFactory and PR CI. Commit trailers are #1907/#1918. No change. |
| 9 | `qa_review_cycle.json`, `injection_scan.json` | co-mount / subprocess single-file copy-back from the task worktree | Review re-drive nudges/escalations; completion-event stamp | **Not a gate.** They can trigger or skip a nudge, or change a stamp; they cannot approve, hand off or merge. Row 1 recomputes the scan instead of reading `injection_scan.json`. No change here; slice D covers in-pod writers. |

No HMAC or signature is added: the Job holds every key it could use.

## Alternatives rejected

- **HMAC the pushed artifacts.** The key would be in the Job, readable by the
  agent (#1932). Ruled out by the task.
- **Stop fetching the plan.** Breaks board progress and the #852 fix, and the
  readers would still trust whatever lands in the spec dir by another route.
- **Refuse gated tasks in `_start_build_unit`.** It also covers drains, but a
  queued task would be accepted and then silently dropped at drain time. The
  front door refuses before anything is queued.
- **Pure recompute for the pause, ignoring the claim.** It would pause builds
  that never paused when the control plane's scan inputs or env differ from the
  Job's. Requiring both keeps today's behaviour for honest Jobs.

## Risks

- A gated task that a local-dev user ran on subprocess is now refused. The
  message names the fix.
- `injection_scan` recompute uses the control plane's
  `AIFACTORY_INJECTION_SCAN` and files. If they differ from the Job's, a real
  pause can be ignored; the run then fails the evidence gate (no commit) and
  goes to human review as `errors`, not to a handoff.
- Legacy tasks whose only contract is in `implementation_plan.json` lose it
  and TFactory infers instead. Those predate the context stash.
- Residual: the Job's store credential can write other specs' keys. After this
  change those bytes only reach display fields.

## Verification

- Regression tests that fail on `origin/dev`:
  - subprocess + `requireReviewBeforeCoding` / quick / `reviewTier: blocking`
    is refused in `start_task_execution` and `start_spec_creation`; ungated and
    kubejob are not.
  - a forged `plan_review` / `injection_scan` pause with no control-plane
    requirement runs the completion instead of pausing; existing pause tests
    still pause once the control-plane state requires it.
  - a forged memory archive with `task_control.json`, `review_state.json`,
    `context/task_contract.json` and a symlink under `memory/` writes none of
    them; a normal `memory/` tree still lands.
  - a forged `tfactory` block in the plan is not returned by
    `load_task_contract`.
  - the merger body labels gate evidence and QA sign-off as build-reported.
- Rows 6-7: tests show that a forged usage object with a lower total or not a
  JSON object, and a forged `task_logs.json` over the control plane's copy,
  are not written (existing tests where they already cover it, new ones
  otherwise).
- Rows 8-9: no change, no new test; the branch is checked by TFactory and CI.
- `ruff`, `scripts/ratchet_lint.py`, targeted pytest, and CI.
