---
status: draft
issue: olafkfreund/Factory#312
spec: spec/2026-10-26-312-insecure-auth-optin.md
---

# Plan: DISABLE_AUTH off loopback needs its own opt-in, not APP_DEBUG

Decisions (from the approved spec): AIFactory gets a `Settings.ALLOW_INSECURE_AUTH: bool = False`
field (env `APP_ALLOW_INSECURE_AUTH`, same name as PFactory/TFactory). `enforce_disable_auth_safety`
allows `DISABLE_AUTH` on a non-loopback host only when that flag is true; `DEBUG` no longer matters
to this guard. `enforce_cors_safety` is not changed. PFactory/TFactory, nix dev shell, docker-compose
and gitops need no change (verified on origin/dev and factory-gitops origin/main, 2026-10-26).

## Steps

1. `apps/web-server/server/config.py`: add `ALLOW_INSECURE_AUTH: bool = False` under
   `DISABLE_AUTH` (~line 116) with a one-line comment; in `enforce_disable_auth_safety`
   (~lines 39-57) replace `not settings.DEBUG` with `not settings.ALLOW_INSECURE_AUTH`, update
   docstring and refusal message to name `APP_ALLOW_INSECURE_AUTH` (not `APP_DEBUG`).
   `tests/test_auth_hardening.py` `TestEnforceDisableAuthSafety`: base adds
   `ALLOW_INSECURE_AUTH=False`; replace the debug-override test with: debug on + no opt-in on
   `0.0.0.0` raises SystemExit; opt-in on `0.0.0.0` passes; loopback passes with debug on and off;
   `Settings()` with env `APP_ALLOW_INSECURE_AUTH=true` sets the field. Update module docstring.
   → verify: `cd apps/web-server && .venv/bin/pytest ../../tests/test_auth_hardening.py -q`
   (from repo root as the repo's pytest config expects) and `ruff check` on both files.
   Traps: the guard runs only from `main.__main__`; pytest suite sets `APP_DISABLE_AUTH=true` and
   HOST 0.0.0.0, so do not call the guard from app startup. Do not touch `mcp_stdio` files.
2. `apps/web-server/.env.example` (~line 69) and `docs/docs/environment-reference.md`
   (`APP_DEBUG` and `APP_DISABLE_AUTH` rows, ~lines 52-54): say the guard needs
   `APP_ALLOW_INSECURE_AUTH`; add a commented `# APP_ALLOW_INSECURE_AUTH=false` line and a
   table row for it. → verify by reading the diff.
   Traps: none.

3. Review fixes on #1895. `apps/web-server/server/config.py:21`: drop `""` from
   `_LOOPBACK_HOSTS`. `config.py:49-57`: after the refusal branch, if the host is not loopback
   (so the opt-in let it through) `logger.warning("auth disabled on network host %s because
   APP_ALLOW_INSECURE_AUTH=true", host)`. `tests/test_auth_hardening.py:69`: move `""` from
   `test_loopback_hosts` to `test_exposed_hosts`; add an empty-host-refuses test and a caplog test
   for the opt-in warning. → verify: `apps/web-server/.venv/bin/pytest tests/test_auth_hardening.py -q`,
   `ruff check` + `ruff format --check` on both files.
   Traps: CI runs `ruff format --check`; `git add` named paths only; no force-push.

## Tests

`pytest tests/test_auth_hardening.py` green; the debug-on-no-opt-in case fails on origin/dev.

## Rollback

Revert the PR; behaviour returns to DEBUG relaxing the guard.
