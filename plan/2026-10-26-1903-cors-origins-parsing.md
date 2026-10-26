---
status: draft
issue: 1903
spec: spec/2026-10-26-1903-cors-origins-parsing.md
---

# Plan: APP_CORS_ORIGINS comma form must parse; a `null` origin must not keep credentials

Decisions: `CORS_ORIGINS` gets `Annotated[list[str], NoDecode]`; the validator `json.loads` a
`[`-prefixed string and otherwise comma-splits with stripping. `cors_allows_credentials` returns
False for `*` or `null` (stripped, case-insensitive); the boot guard follows it. Docs match.

## Steps

1. `apps/web-server/server/config.py`: imports (`json`, `typing.Annotated`,
   `pydantic_settings.NoDecode`); `CORS_ORIGINS` annotation (~line 175); `_parse_cors_origins`
   (~183-192) as above; `cors_allows_credentials` (~60-71) treats `null` like `*` and its docstring
   says why; the `enforce_cors_safety` warning line (~87) says `'*' or 'null'` (leave the refusal
   message lines alone to avoid conflicting with #1902). `tests/test_auth_hardening.py`: new class
   `TestCorsOriginsParsing` with monkeypatched env + `Settings()` for `*`, `https://a.com,*`,
   ` * `, `https://a.com, https://b.com`, `["https://a.com"]`; in `TestCorsSafety`, `null` and
   `["https://a.com", "null"]` drop credentials and `enforce_cors_safety` raises SystemExit.
   `apps/web-server/.env.example` (~13-18) and `docs/docs/environment-reference.md` (`APP_CORS_ORIGINS`
   row ~66): both forms parse; `*` or `null` disables credentialed CORS.
   → verify: `apps/web-server/.venv/bin/pytest tests/test_auth_hardening.py -q`; `ruff check` +
   `ruff format --check` on both Python files.
   Traps: CI runs `ruff format --check`; `git add` named paths only; `Settings()` in tests must
   use monkeypatch.setenv so env does not leak.

## Tests

`pytest tests/test_auth_hardening.py` green; parse and null tests fail on origin/dev.

## Rollback

Revert the PR.
