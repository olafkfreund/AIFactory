---
status: draft
issue: olafkfreund/Factory#312
spec: spec/2026-10-26-312-wildcard-cors-optin.md
---

# Plan: the wildcard-CORS boot guard needs its own opt-in, not APP_DEBUG

Decisions: new `Settings.ALLOW_WILDCARD_CORS: bool = False` (env `APP_ALLOW_WILDCARD_CORS`).
`enforce_cors_safety` refuses a wildcard origin unless that flag is true; `DEBUG` no longer
matters. Warning and `cors_allows_credentials` unchanged. No gitops, dev shell, compose or chart
change (none set CORS).

## Steps

1. `apps/web-server/server/config.py`: field under `CORS_ORIGINS` (~line 175) with a one-line
   comment; `enforce_cors_safety` (~lines 74-98): `if not settings.DEBUG` →
   `if not settings.ALLOW_WILDCARD_CORS`, docstring and message name `APP_ALLOW_WILDCARD_CORS`.
   `tests/test_auth_hardening.py` `TestCorsSafety`: base adds `ALLOW_WILDCARD_CORS=False`;
   wildcard refuses with debug on and off (match the flag name); opt-in boots; allowlist no-op with
   debug on and off; `Settings()` reads `APP_ALLOW_WILDCARD_CORS=true`.
   `apps/web-server/.env.example` (CORS block ~line 18): add commented
   `# APP_ALLOW_WILDCARD_CORS=false` line. `docs/docs/environment-reference.md` rows
   `APP_DEBUG` (~52) and `APP_CORS_ORIGINS` (~66): name the new flag; add its row.
   → verify: `apps/web-server/.venv/bin/pytest tests/test_auth_hardening.py -q`,
   `ruff check` and `ruff format --check` on the two Python files.
   Traps: guard runs only from `main.__main__`; CI runs `ruff format --check`; do not touch
   `mcp_stdio` files; `git add` named paths only.

2. Review fix on #1902. `apps/web-server/server/config.py` `enforce_cors_safety` (~line 99): after
   the refusal branch, `logger.warning("wildcard CORS origin allowed because
   APP_ALLOW_WILDCARD_CORS=true; credentials disabled")`. `tests/test_auth_hardening.py`
   `TestCorsSafety`: caplog test that the opt-in logs it and an allowlist does not.
   → verify: pytest on the file, `ruff check` + `ruff format --check`.
   Traps: `git add` named paths only; no force-push.

## Tests

`pytest tests/test_auth_hardening.py` green; debug-no-opt-in fails on origin/dev.

## Rollback

Revert the PR.
