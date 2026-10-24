---
status: draft
issue: 1858
intent: intent/2026-10-24-1858-correction-triage-gate.md
---

# Spec: optionally require a triage envelope on apply-correction

## Design

1. `Settings` (`apps/web-server/server/config.py:101`, `env_prefix="APP_"`)
   gains `REQUIRE_CORRECTION_TRIAGE: bool = False`, with a comment in the style
   of `SCOPED_SERVICE_TOKENS_ENABLED`. Env var: `APP_REQUIRE_CORRECTION_TRIAGE`.
2. The route `apply_task_correction` (`apps/web-server/server/routes/execution.py:1553`)
   checks first, before `_resolve_task`: if
   `get_settings().REQUIRE_CORRECTION_TRIAGE and request.triage is None`, raise
   `HTTPException(422, detail=...)`. The detail names the setting and says to send
   the `triage` block. Nothing is written or audited. This applies to previews
   (`confirm=false`) too, so a caller finds out before it confirms.
3. A request that has `triage` goes to `apply_correction` unchanged, and
   `correction.py:260` validates it there as today. A malformed triage still
   returns the existing `rejected: True` body.
4. Default **off** (decided, see intent): TFactory's skills post markdown-only
   through AIFactory's MCP tool `task_apply_correction`. olafkfreund/TFactory#1430
   tracks sending triage everywhere. Flipping the default is a follow-up and is
   not part of this change.

Only the route is gated. The PR-endgame callers in `completion_orchestration.py`
call `qa.correction.apply_correction` in-process with AIFactory's own findings.
They are not external hand-backs, so they stay as they are.

## Alternatives rejected

- Gate inside `qa.correction.apply_correction`: this would also block the internal
  PR-endgame auto-fix callers, which have no triage. That is the wrong boundary.
- Return 200 with `success: false` like a malformed triage does: the brief asks
  for a 4xx, and a 4xx makes `default_sender` in TFactory raise, so the failure
  shows up instead of passing silently.
- Also require `manifest_hash` / `correlation_key` at the top level: the triage
  validation already extracts both, so requiring `triage` is enough.
- Default on: this would break the TFactory skill paths today.
- Adding `triage` passthrough to the AIFactory MCP tool now: that is out of
  scope. It is noted on TFactory#1430 as one way to close that issue.

## Risks

- Turning the flag on before TFactory#1430 lands breaks skill-driven hand-backs
  with a 422. It is opt-in, and the detail message says why.
- `settings` is a module-level singleton, so tests must monkeypatch the attribute
  and not the env var.

## Verification

New tests in `tests/audit/test_task_action_audit.py` next to the existing
apply-correction route test (same `execution` import and monkeypatches):

- flag on + markdown-only request → `HTTPException` 422, `apply_correction` not called;
- flag off + markdown-only request → `apply_correction` called (today's behaviour);
- triage request → `apply_correction` called with the flag on and with it off.

Run: `pytest tests/audit/test_task_action_audit.py tests/test_qa_correction.py tests/test_handback_contract.py -q`.
