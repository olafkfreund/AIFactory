---
status: approved
issue: 1858
spec: spec/2026-10-24-1858-correction-triage-gate.md
---

# Plan: optionally require a triage envelope on apply-correction

Approved decisions (from spec):

- New setting `REQUIRE_CORRECTION_TRIAGE: bool = False` on `Settings`
  (env `APP_REQUIRE_CORRECTION_TRIAGE`). Default **off**, because TFactory skills
  still post markdown-only. olafkfreund/TFactory#1430 tracks fixing that, and
  flipping the default is a later change, not this one.
- The gate lives in the REST route only, not in `qa.correction.apply_correction`.
  In-process PR-endgame callers (`completion_orchestration.py:396,486`) must stay
  ungated.
- With the flag on and `request.triage is None`, raise `HTTPException(422)` before
  anything else (before `_resolve_task`), for previews as well as confirms. Nothing
  is written, audited or receipted.
- A request with `triage` is untouched. `correction.py:260` still validates it, and
  a malformed triage still returns the existing 200 `rejected: True` body.
- No new validation code. Requiring `triage` is the whole envelope check.

## Steps

1. `apps/web-server/server/config.py:237` (after `RMUX_ENABLED: bool = False`):
   add a 3-4 line comment (#1858: when true, apply-correction refuses a hand-back
   with no `triage` block. Off until TFactory#1430 sends triage on every path.
   Override: APP_REQUIRE_CORRECTION_TRIAGE.) and `REQUIRE_CORRECTION_TRIAGE: bool = False`.
   → verify: `cd apps/web-server && python -c "from server.config import settings; print(settings.REQUIRE_CORRECTION_TRIAGE)"` prints `False`.
   Traps: `model_config` sits right below at `:248`, so put the field above it. Do
   not touch `.env.example`. Other optional flags (`SCOPED_SERVICE_TOKENS_ENABLED`)
   are not listed there either.

2. `apps/web-server/server/routes/execution.py`:
   - imports (`:42` block): add `from ..config import get_settings`, matching
     `routes/search.py:20`.
   - `apply_task_correction` (`:1553`), the first statement after the docstring
     (before `*_, spec_dir = _resolve_task(task_id)`):
     ```python
     if get_settings().REQUIRE_CORRECTION_TRIAGE and request.triage is None:
         raise HTTPException(
             status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
             detail="A triage report is required (APP_REQUIRE_CORRECTION_TRIAGE); "
             "send the structured 'triage' block with the hand-back.",
         )
     ```
   - add one docstring line saying the flag requires `triage`.
   → verify: `python -c "import ast,sys; ast.parse(open('apps/web-server/server/routes/execution.py').read())"` and step 3's tests.
   Traps: call `get_settings()` at request time, not at import time, so tests can
   patch it. `status` and `HTTPException` are already imported (`:16`). Do not
   change `ApplyCorrectionRequest` (`:102`): `triage` must stay optional in the
   schema so the flag-off path is byte-identical. Do not hand-edit
   `apps/web-server/openapi.yaml`, because CI regenerates it
   (`.github/workflows/techdocs.yml`).

3. `tests/audit/test_task_action_audit.py` (append after `:279`): add tests that
   reuse the module's `execution`, `USER` (`:31`), `_run` (`:46`) and the `rows`
   fixture, with the same monkeypatches as
   `test_apply_correction_audits_only_a_confirmed_success` (`_resolve_task`,
   `apply_correction` as a fake that records calls, `record_handback_receipt`).
   Toggle the flag with
   `monkeypatch.setattr(server.config.settings, "REQUIRE_CORRECTION_TRIAGE", <bool>)`
   (`import server.config`).
   - flag on, `ApplyCorrectionRequest(fix_request_md="# fix", confirm=True)` →
     `pytest.raises(HTTPException)` with `status_code == 422`, fake not called,
     no audit rows, no receipts.
   - flag off, same request → fake called once (today's behaviour).
   - parametrize flag on/off with `triage={"contract_version": ...}` (any dict) →
     fake called once, and `triage` passed through.
   → verify: `pytest tests/audit/test_task_action_audit.py -q` passes.
   Traps: `settings` is a module singleton, so patch the attribute. Setting
   `APP_REQUIRE_CORRECTION_TRIAGE` after import has no effect. Import
   `HTTPException` from `fastapi`.

4. `CHANGELOG.md`: add one line under Unreleased in the existing format: `apply-correction:
   optional APP_REQUIRE_CORRECTION_TRIAGE rejects markdown-only hand-backs (422)
   (#1858)`.
   → verify: `git diff CHANGELOG.md` shows one added line.
   Traps: none.

## Tests

```
pytest tests/audit/test_task_action_audit.py tests/test_qa_correction.py tests/test_handback_contract.py tests/test_mcp_task_control.py -q
```
All pass. The existing tests run with the flag off and must not change.

## Rollback

Revert the commit. The flag defaults off, so leaving it unset already restores
today's behaviour without a deploy.

## Deviation (CI)

- `get_settings` is imported absolutely (`from server.config import ...`) to avoid a new
  strict-ruff TID252 relative import. `apps/web-server/openapi.yaml` regenerated for the docstring line.
