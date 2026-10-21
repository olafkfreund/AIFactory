---
status: approved
issue: 1454
intent: intent/2026-09-18-1454-security-gate-never-runs.md
---

# Spec: Make the pre-merge security gate honest

## Design

Recommended answers to the intent's open questions:
**(1) leave the gate off in production for now and say so plainly**, and
**(2) when it is enabled, fail closed**. A scan that did not happen is not a pass.

Why keep it off: `AIFACTORY_SELF_HEAL` does not only enable this gate. It also turns on
review-tier gating and plan-artifact emission (`agents/self_heal_integration.py`, items
2 and 4). Flipping it in `factory-gitops` changes four behaviours at once and restarts pods.
That is a separate, deliberate rollout. This change makes the gate *correct* so that
turning it on later is a real control.

Changes:

1. `agents/self_heal_integration.py`
   - `security_pre_merge_gate_sync` and `security_pre_merge_gate`: on a scanner exception,
     return `GateDecision(blocked=True, threshold=…, summary="security scan failed: <exc>; merge not scanned")`
     and `logger.error(…)`, instead of `gate_decision([])`.
   - Add a `diff_ok: bool = True` parameter. When enabled and `diff_ok` is false, return a
     blocked decision "diff unavailable; merge not scanned". An empty diff is still a no-op
     only when the diff command succeeded.
2. `core/worktree.py:986-1005`
   - Move the import out of the `try` (an in-repo import failing is a bug that should be
     loud).
   - Pass `diff_ok=_diff.returncode == 0`.
   - Replace `except Exception: pass` with `logger.exception(…)` and `return False` *only
     when the gate is enabled* (`is_self_heal_enabled()`). When disabled, behaviour is
     unchanged.
3. `docs/docs/environment-reference.md:212` and `apps/backend/.env.example:401`: state that
   the gate is **off in every deployment today**, that the flag also enables items 2 and 4,
   and that when on, it blocks on a high finding *or* on a scan that could not run.

## Alternatives rejected

- **Enable in production now.** It bundles three other behaviours and a pod restart into
  a bug fix. Deferred to a gitops change with its own review.
- **Fail open loudly (merge, then log "unscanned").** Nobody reads the log at merge time,
  so it reproduces the current problem with a log line added.
- **Delete the gate.** It is tested, and the only thing wrong is its failure handling.

## Risks

- With the flag on, a flaky `git diff` now blocks merges. That is intended, and the
  message says why. With the flag off (all deployments), there is no behaviour change.
- Tests in `tests/test_self_heal_integration.py` that assert "scanner failure → not
  blocked" will change meaning and must be updated, not deleted.

## Verification

- New unit tests: scanner raises → `blocked=True` with a "not scanned" summary; diff
  command fails → blocked; flag off → `None` in every case.
- `worktree` merge test with the flag on and the scanner patched to raise → merge
  returns `False`. The same test with the flag off → merge proceeds.
- `apps/backend/.venv/bin/pytest tests/test_self_heal_integration.py tests/test_security_reviewer.py -v` passes.
