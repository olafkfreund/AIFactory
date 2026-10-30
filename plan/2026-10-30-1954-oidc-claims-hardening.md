---
status: approved
issue: 1954
spec: spec/2026-10-30-1954-oidc-claims-hardening.md
coders:
  1: codex
  2: codex
---

# Plan: make the #324 OIDC ID-token hardening actually apply

Decisions (from the spec):
- Enforce on each call, not at `register()`. Pin the algs in the loaded
  `server_metadata` and pass `claims_options` to `authorize_access_token`.
- `iss` values are `[issuer, issuer + "/"]`, `aud` is `[client_id]`, `exp` is essential.
- Algs are RS/ES/PS 256/384/512 only. Remove the dead register-time kwargs.
- The pattern matches PFactory #953 and TFactory #1460. Codex implements both steps.

## Steps

1. `apps/web-server/server/oidc/client.py`: add `ID_TOKEN_SIGNING_ALGS` and the typed
   helper (`Request` imported under `TYPE_CHECKING`). Remove `claims_options` and the
   alg list from `register()`, and keep PKCE. Export the helper from
   `server/oidc/__init__.py`. In `routes/oidc_routes.py::oidc_callback`, call
   `await authorize_access_token(oauth.oidc, request)` and keep the except block. In
   `tests/test_oidc_claims_options.py`, drop the two kwargs tests and keep PKCE.
   → verify: `apps/backend/.venv/bin/pytest tests/test_oidc_claims_options.py tests/oidc -q`
   Traps: never `git add -A`; never stage the `.venv` symlink; the pre-commit hook may
   ruff-fix staged files.
2. Add `tests/test_oidc_id_token_hardening.py` (same `sys.path` setup as
   `tests/test_oidc_claims_options.py`) with the spec's Verification cases, typed for
   `mypy --strict`.
   → verify: the new test passes on authlib 1.9.1 (scratch venv) and on the repo venv.
   Run `scripts/ratchet_lint.py --base origin/dev --mypy` if present.

## Tests

`apps/backend/.venv/bin/pytest tests/test_oidc_*.py tests/oidc -q`, plus ruff on
touched files.

## Rollback

Revert the PR.
