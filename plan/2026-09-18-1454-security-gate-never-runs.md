---
status: approved
issue: 1454
spec: spec/2026-09-18-1454-security-gate-never-runs.md
---

# Plan: Make the pre-merge security gate honest

## Decisions (carried from the approved spec)

- `AIFACTORY_SELF_HEAL` stays **off** in every deployment. No gitops change in this task.
  Enabling it is a separate rollout, because the flag also enables review-tier gating and
  plan-artifact emission.
- When the flag is **on**, a scan that did not happen **blocks** the merge (fail closed):
  a scanner exception, or a `git diff` that failed.
- When the flag is **off**, merge behaviour is byte-for-byte unchanged.
- The docs say plainly that the gate is off today and what enabling it does.

## Steps

1. `apps/backend/agents/self_heal_integration.py`: add `import logging` and
   `logger = logging.getLogger(__name__)`. Add a helper
   `_not_scanned(threshold, reason) -> GateDecision` returning
   `GateDecision(blocked=True, threshold=threshold, summary=f"security scan did not run ({reason}); merge not scanned")`.
   → verify: module imports (`python -c "import agents.self_heal_integration"` from `apps/backend`).
2. Same file, `security_pre_merge_gate_sync(diff_text, *, threshold="high", diff_ok=True)`:
   - flag off → `None` (unchanged);
   - flag on and `not diff_ok` → `_not_scanned(threshold, "diff unavailable")`;
   - flag on and empty diff with `diff_ok` → `None` (unchanged);
   - scanner exception → `logger.error(..., exc_info=True)` and `_not_scanned(threshold, "scanner failed")`.
   → verify by the new tests in step 5.
3. Same file, async `security_pre_merge_gate`: the same exception change (step 2's last
   bullet). No `diff_ok`: its callers pass text they already hold.
   → verify by step 5.
4. `apps/backend/core/worktree.py:986-1005`:
   - Import `security_pre_merge_gate_sync` and `is_self_heal_enabled` **outside** the `try`.
   - Call `security_pre_merge_gate_sync(_diff.stdout or "", diff_ok=_diff.returncode == 0)`.
   - Replace `except Exception: pass` with
     `logger.exception("Security pre-merge gate crashed for spec %r", spec_name)`, then
     `if is_self_heal_enabled(): return False`.
   - Keep the comment accurate: "default-off; when on, an unscanned merge is refused".
   → verify by step 6.
5. `tests/test_self_heal_integration.py`: add
   `test_security_gate_sync_scanner_failure_blocks`, `test_security_gate_async_scanner_failure_blocks`
   (monkeypatch `scan_diff_static` / `review_diff` to raise),
   `test_security_gate_sync_diff_unavailable_blocks`, and
   `test_security_gate_sync_failure_noop_when_disabled`. Update any existing assertion that
   a scanner failure is unblocked (search `blocked is False` near a raising scanner).
   → verify: `apps/backend/.venv/bin/pytest tests/test_self_heal_integration.py -v` passes.
6. Merge-path test (in the existing worktree merge tests, or a new
   `tests/test_worktree_security_gate.py`): flag on + gate patched to raise → `merge_worktree`
   returns `False`; flag off + same patch → merge proceeds.
   → verify: the new test passes.
7. `docs/docs/environment-reference.md:212` and `apps/backend/.env.example:401`: reword to
   "off in all deployments; when on, blocks a merge with a high-severity finding **or** one
   that could not be scanned; also enables review-tier gating and plan-artifact emission".
   → verify: `git diff` shows only these lines.

## Tests

```bash
apps/backend/.venv/bin/pytest tests/test_self_heal_integration.py tests/test_security_reviewer.py tests/test_worktree_security_gate.py -v
apps/backend/.venv/bin/pytest tests/ -k "worktree or merge" -q
```
Expected: all pass. There is no behaviour change with the flag unset.

## Rollback

Revert the single implementation commit. No config or data changes to undo.
