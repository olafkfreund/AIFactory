---
status: approved
issue: 1724
intent: intent/2026-10-22-1724-kubejob-isolated-host.md
---

# Spec: kubejob is the supported isolated host for trusted plans

## Design

Chosen (owner decision B): kubejob with the durable job store
(`selected_backend() == "kubejob" and store_enabled()`) remains the only host
where `host_isolated()` is true. A kubejob build runs the agent in its own Job
pod, so it cannot reach the web server's `/proc` or the per-call `hosts.yml`.
On every other host (subprocess, sandbox, PID-namespaced sandbox) the agent
shares the uid with the process holding the token, so the token is readable
through `/proc/<child>`; those hosts stay non-isolated and trusted plans there
keep the existing hold/empty-profile behaviour.

Changes:

1. `trusted_contract.py` `host_isolated()`: replace the "widen once ..." comment
   with a statement that this is the decided policy (#1760, #1724), and why.
2. `docs/docs/task-contract.md`: add a short "Supported isolated host" section
   saying the above, what happens on other hosts, and that a separate-uid
   design needs its own spec.

`_ISOLATED_STAMPS` still lists `sandbox-pidns`; it is unreachable as a pass
because `host_isolated()` gates every caller, so it is left alone.

## Alternatives rejected

- A: count the PID-namespaced sandbox as isolated. Rejected: run.py and the
  agent share uid and namespace there, so the token stays readable.
- C: run agents/children under a separate uid. Rejected for now: it needs
  privilege changes in the chart and runtime; it would need its own spec.

## Risks

None at runtime; comment and docs only.

## Verification

- `pytest apps/web-server/tests/test_trusted_contract_merge.py` passes
  unchanged (including `test_pid_namespaced_sandbox_host_is_not_isolated`).
- `ruff check` on the touched Python file.
- Docs build in CI.
