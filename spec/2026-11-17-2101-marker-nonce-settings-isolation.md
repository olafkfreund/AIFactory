---
status: draft
issue: 2101
intent: intent/2026-11-17-2101-marker-nonce-settings-isolation.md
---

# Spec: isolate project settings in the dispatch test

## Design

In the test, monkeypatch `server.services.project_settings_store.get_settings`
to an async function returning `{}` (the exact pattern in
`tests/test_build_backend_kubejob.py`), so `mcp_spawn_env` never opens the
default engine. Nothing else changes; the assertions on the Job env stay.

## Alternatives rejected

- Point the engine at a temp DB: the engine is a module-level singleton created
  at import; re-pointing it is invasive and affects other tests.
- Autouse fixture for the whole file: other tests in the file already stub what
  they need; a file-wide stub would hide real reads. Narrowest fix wins.
- Change `dispatch` or the decrypt path: behaviour is intentional (fail closed).

## Risks

None for production. The stub means this test no longer exercises the real
settings read; that is covered in the project_settings_store tests.

## Verification

Before: test fails alone with the decrypt error on this machine (real DB holds a
row for that path). After: it passes under the same polluted DB, the whole file
passes, and ruff check/format are clean.
