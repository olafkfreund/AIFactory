---
status: approved
issue: olafkfreund/Factory#312
intent: intent/2026-10-26-312-wildcard-cors-optin.md
---

# Spec: the wildcard-CORS boot guard needs its own opt-in, not APP_DEBUG

## Design

- `apps/web-server/server/config.py`: `Settings.ALLOW_WILDCARD_CORS: bool = False`
  next to `CORS_ORIGINS` (env `APP_ALLOW_WILDCARD_CORS`). `enforce_cors_safety`
  raises unless `settings.ALLOW_WILDCARD_CORS`; refusal message and docstring name the
  new flag, not `APP_DEBUG`. The warning and credential drop are unchanged.
  Review fix (MEDIUM, #1902): when the opt-in lets a wildcard through, log one distinct
  warning: `wildcard CORS origin allowed because APP_ALLOW_WILDCARD_CORS=true; credentials disabled`.
- `tests/test_auth_hardening.py` `TestCorsSafety`: debug on + wildcard + no opt-in
  refuses; opt-in + wildcard boots (credentials still dropped); explicit allowlist
  unchanged with debug on/off; `Settings` reads `APP_ALLOW_WILDCARD_CORS`; caplog test for
  the opt-in warning (and its absence for an allowlist).
- `apps/web-server/.env.example`, `docs/docs/environment-reference.md`: document it.

## Alternatives rejected

- **Accept DEBUG or the new flag.** Debug alone would still relax the guard.
- **Strip `*` silently, as PFactory does.** Hides a misconfiguration the guard was
  added to surface (#555), and changes the runtime CORS behaviour, not just boot.
- **Reuse `APP_ALLOW_INSECURE_AUTH`.** Different risk; one flag per guard is clearer.

## Risks

- A developer running with `APP_CORS_ORIGINS=*` and `APP_DEBUG=true` gets a boot
  refusal naming the fix. No live deploy affected.
- Conflicts with PR #1895 on the `APP_DEBUG` row of the environment reference and in
  `tests/test_auth_hardening.py` imports; trivial to resolve.

## Verification

`pytest tests/test_auth_hardening.py`: the debug-no-opt-in test fails on origin/dev.
ruff check and ruff format --check on touched files.
