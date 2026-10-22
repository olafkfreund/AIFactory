---
status: draft
issue: 1724
spec: spec/2026-10-22-1724-kubejob-isolated-host.md
---

# Plan: kubejob is the supported isolated host for trusted plans

Approved decisions (from the spec):

- Owner decision B: `host_isolated()` stays
  `selected_backend() == "kubejob" and store_enabled()`. No logic change.
- The PID-namespaced sandbox is not isolated: same uid, so the per-call
  GitHub token in `hosts.yml` is readable via `/proc/<child>` (#1724).
- A separate-uid design is out of scope and needs its own spec.
- `test_pid_namespaced_sandbox_host_is_not_isolated` is kept unchanged.

## Steps

1. `apps/web-server/server/services/trusted_contract.py:34-40` (comment in
   `host_isolated()`): replace with a policy statement linking #1760 and #1724.
   → verify by `ruff check` on the file and
   `pytest apps/web-server/tests/test_trusted_contract_merge.py -q`.
   Traps: comment only; keep the `noqa` imports.
2. `docs/docs/task-contract.md`, after section 4 "Backward compatibility": add
   section "Supported isolated host" (renumber Status to 6).
   → verify by `grep -n 'Supported isolated host' docs/docs/task-contract.md`.
   Traps: no IPs or names; keep the `---` separators.

## Tests

`pytest apps/web-server/tests/test_trusted_contract_merge.py -q` passes.

## Rollback

Revert the commit; no runtime effect.
