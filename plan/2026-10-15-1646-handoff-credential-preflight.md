---
status: approved
issue: 1646
spec: spec/2026-10-15-1646-handoff-credential-preflight.md
---

# Plan: probe gemini credentials, and preflight the TFactory handoff

Approved decisions carried from the spec:

- Reuse `apps/backend/core/auth_preflight.py` and its modes: `off`, `warn`
  (default, never blocks), `enforce` (blocks only on `auth_failed`).
  `inconclusive` never blocks in any mode.
- New `probe_google`: key from `GEMINI_API_KEY`, else `GOOGLE_API_KEY`; none →
  `skipped`. `GET https://generativelanguage.googleapis.com/v1beta/models?pageSize=1`
  with header `x-goog-api-key`. 200 → `ok`; 401/403 → `auth_failed`; 400 with
  `API_KEY_INVALID` in the body → `auth_failed` (Google's answer to an invalid
  key); anything else or transport error → `inconclusive`. Detail names the env
  var, never the value; the key is never in the URL.
- `gemini*` model names map to provider `google`.
- `send_handoff` (`apps/backend/pfactory/tfactory_client.py:741`) runs the
  preflight on `payload["contract"]["execution"]["phase_models"]` values, off
  the event loop, after the `not_configured` return and before the POST. In
  `enforce`, an `auth_failed` result returns
  `{"sent": False, "reason": "auth_preflight_failed", "provider", "detail"}`
  without posting. A preflight exception is logged and the POST proceeds.
- Scope: AIFactory only. TFactory's verify-planner preflight is out of scope.

## Steps

1. `apps/backend/core/auth_preflight.py` + `tests/test_auth_preflight.py`
   - Add `_GOOGLE_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1"`
     next to `_ANTHROPIC_MODELS_URL` (`:33`).
   - Add `_http_get(url, headers) -> tuple[int, str]`: same as `_http_status`
     (`:68-76`) but also returns `resp.read()` / `exc.read()` decoded
     (`errors="replace"`), first 2000 chars. Rewrite `_http_status` as
     `return _http_get(url, headers)[0]` so the anthropic tests that
     monkeypatch `_http_status` keep working.
   - Add `probe_google(env=None) -> PreflightResult` after `probe_anthropic`,
     with the classification above; `credential_kind="api_key"`; catch
     `(urllib.error.URLError, TimeoutError, OSError)` as `inconclusive`.
   - `providers_for_models` (`:133-140`): also map `name.startswith("gemini")`
     to `"google"` (dedupe like `anthropic`). Update its comment (`:130-132`).
   - `_PROBES` (`:143`): add `"google": probe_google`. Update the module
     docstring only if it claims anthropic-only.
   - Tests (monkeypatch `ap._http_get`, never the network): 200 ok; 401 and
     403 auth_failed; 400 + `API_KEY_INVALID` body auth_failed with
     `GEMINI_API_KEY` in detail; plain 400 inconclusive; 503 inconclusive;
     `URLError` inconclusive; no key skipped; `GOOGLE_API_KEY` used when
     `GEMINI_API_KEY` is absent; the key value is in neither the detail nor the
     URL passed to `_http_get` and is sent as `x-goog-api-key`;
     `providers_for_models(["gemini", "claude-x", "gemini-2.5-pro"]) == ["google", "anthropic"]`.
   - Replace `test_run_auth_preflight_empty_for_non_probeable` (`:135-136`),
     which asserts gemini is unprobed, with a non-probeable name such as
     `"gpt-4o"`.
   → verify: `cd apps/backend && .venv/bin/python -m pytest ../../tests/test_auth_preflight.py -q`
   (or the repo's usual pytest invocation), `ruff check` and
   `ruff format --check` on both files.
   Traps: do not log or embed the key; keep `_http_status`'s signature; do not
   reformat untouched code.

2. `apps/backend/pfactory/tfactory_client.py` + `tests/test_pfactory_tfactory_client.py`
   - Add `import asyncio` to the imports (`:20-31`).
   - Add a helper above `send_handoff`:
     `_handoff_preflight_block(payload: dict) -> dict | None`. It calls
     `auth_preflight.preflight_mode()` / `auth_preflight.run_auth_preflight()`
     through a top-level `from core import auth_preflight` (deviation: the plan
     first said a lazy import, but the repo's ruff ratchet rejects a new
     PLC0415, and `core.child_env` is already imported at top level, so the
     lazy import bought nothing). Returns `None` when mode is `off` or there are no models. Models:
     `((payload.get("contract") or {}).get("execution") or {}).get("phase_models")`,
     string values only, guarding each level with `isinstance(..., dict)`.
     Logs each non-`ok`, non-`skipped` result with `_log.warning` (provider and
     detail). Returns the block dict for the first `auth_failed` when mode is
     `enforce`, else `None`.
   - In `send_handoff`, after the `not_configured` return (`:755-756`):
     ```python
     try:
         blocked = await asyncio.to_thread(_handoff_preflight_block, payload)
     except Exception as exc:  # noqa: BLE001 — a preflight bug must not stop a handoff
         _log.warning("TFactory handoff auth preflight failed: %s", exc)
         blocked = None
     if blocked:
         return blocked
     ```
     Add `auth_preflight_failed` to the docstring's list of reasons.
   - Tests (monkeypatch `core.auth_preflight.run_auth_preflight` or the module
     attribute the helper resolves; set `AIFACTORY_AUTH_PREFLIGHT` via
     monkeypatch): payload
     `{"contract": {"execution": {"phase_models": {"planning": "gemini"}}}}`.
     (a) `enforce` + `auth_failed` → `sent False`, `reason == "auth_preflight_failed"`,
     `provider == "google"`, poster not called.
     (b) `warn` + `auth_failed` → poster called, `sent True`.
     (c) `enforce` + `inconclusive` → poster called.
     (d) `off` → `run_auth_preflight` not called.
   → verify: pytest on `tests/test_pfactory_tfactory_client.py` and
   `tests/test_tfactory_auto_handoff.py`; `ruff check`, `ruff format --check`.
   Prove (a) fails on unfixed `tfactory_client.py` (copy the old file from
   `origin/dev` in temporarily).
   Traps: `send_handoff` must never raise; call through the module
   (`auth_preflight.run_auth_preflight`) so tests can monkeypatch it; the probe is blocking, so it must run in `asyncio.to_thread`.

## Tests

```
cd <worktree> && PATH=<main clone>/apps/backend/.venv/bin:$PATH \
  python -m pytest tests/test_auth_preflight.py tests/test_pfactory_tfactory_client.py \
  tests/test_tfactory_auto_handoff.py tests/test_pfactory_routing.py -q
ruff check <touched files>; ruff format --check <touched files>
```
All pass; the new enforce-handoff test fails on unfixed dev.

## Rollback

Revert the two implementation commits. Operators can also set
`AIFACTORY_AUTH_PREFLIGHT=off` to disable every probe without a deploy of code.

## Step 3 (added after PR review, deviation recorded in spec/)

`apps/backend/core/auth_preflight.py`, `apps/backend/pfactory/tfactory_client.py`
and both test files:

- `_google_key_invalid(body)` parses the JSON; `_classify_google` returns
  `auth_failed` only for 400 + `API_KEY_INVALID` in `error.details[].reason`.
- `probe_google` reads `GEMINI_API_KEY` only; `skipped` when
  `GOOGLE_GENAI_USE_VERTEXAI` or `GOOGLE_APPLICATION_CREDENTIALS` is set.
- `_http_status` no longer reads the body (`read_body=False`); `_http_get` reads it through
  `_read_body`, which returns `""` on `OSError` / `http.client.HTTPException`.
- `_cached(provider, secret, probe)`: module dict, key
  `(provider, sha256(secret))`, TTL 300 s, stores `ok`/`auth_failed` only.
- Probes and `run_auth_preflight` take `timeout` (default 10 s);
  `_handoff_preflight_block` passes `_HANDOFF_PREFLIGHT_TIMEOUT_S = 3`.
- Both probes catch `ValueError` → `inconclusive`, detail
  "credential is not a valid header"; `send_handoff` logs
  `type(exc).__name__` only.
- Tests: 403 `SERVICE_DISABLED` is inconclusive (so `enforce` does not block);
  `GOOGLE_API_KEY`-only, Vertex and ADC are skipped; `IncompleteRead` on the
  body gives `(400, "")`; CR/LF keys are inconclusive and not echoed; ok is
  cached and inconclusive is not; the handoff passes `timeout=3`. An autouse
  fixture clears the cache between tests.
→ verify: pytest on `tests/test_auth_preflight.py`,
`tests/test_pfactory_tfactory_client.py`, `tests/test_tfactory_auto_handoff.py`,
`tests/test_pfactory_routing.py`, `tests/audit/test_task_action_audit.py`;
ruff check / format; ruff + mypy ratchets (pre-commit).
Traps: the existing test mocks of `_http_status`, `_http_get` and
`run_auth_preflight` must accept the new `timeout` keyword.
Note: `_http_status` calls `_http_get(..., read_body=False)` instead of
building its own `Request`; a second `Request` call trips the S310 ratchet.
