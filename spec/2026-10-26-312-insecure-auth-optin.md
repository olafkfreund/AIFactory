---
status: approved
issue: olafkfreund/Factory#312
intent: intent/2026-10-26-312-insecure-auth-optin.md
---

# Spec: DISABLE_AUTH off loopback needs its own opt-in, not APP_DEBUG

## Design

Mirror TFactory (`ALLOW_INSECURE_AUTH` setting, same env name).

- `apps/web-server/server/config.py`:
  - `Settings`: new field `ALLOW_INSECURE_AUTH: bool = False` next to
    `DISABLE_AUTH` (env `APP_ALLOW_INSECURE_AUTH` via the existing `APP_` prefix).
  - `enforce_disable_auth_safety`: replace `not settings.DEBUG` with
    `not settings.ALLOW_INSECURE_AUTH`; the refusal message names
    `APP_ALLOW_INSECURE_AUTH` and no longer suggests `APP_DEBUG`. Docstring updated.
  - `enforce_cors_safety` is untouched.
  - Review fix (MEDIUM): remove `""` from `_LOOPBACK_HOSTS`. uvicorn/asyncio bind an
    empty host to all interfaces, so `APP_HOST=` is exposed, not loopback.
  - Review fix (LOW): when the opt-in lets a non-loopback boot through, log one distinct
    warning: `auth disabled on network host <host> because APP_ALLOW_INSECURE_AUTH=true`.
- `tests/test_auth_hardening.py` (`TestEnforceDisableAuthSafety`): base settings gain
  `ALLOW_INSECURE_AUTH=False`; tests cover debug-on-no-opt-in refuses off loopback,
  opt-in boots, loopback unchanged (with and without debug), and that `Settings`
  reads `APP_ALLOW_INSECURE_AUTH`. `""` moves from the loopback cases to the exposed
  cases of `TestHostIsLoopback`; an empty host refuses without the opt-in; the opt-in
  path logs the distinct warning (caplog).
- `apps/web-server/.env.example` and `docs/docs/environment-reference.md`: document
  the new flag; `APP_DEBUG` no longer described as relaxing the auth guard.
- PFactory and TFactory: no change; they already use `APP_ALLOW_INSECURE_AUTH` and
  ignore debug.
- Nix dev shell / docker-compose: no change; neither sets `APP_DISABLE_AUTH`.

## Alternatives rejected

- **Accept either DEBUG or the new flag** (additive, flag defaults to today). Keeps the
  defect: debug alone still turns auth off on the network.
- **Drop the off-loopback escape entirely.** Breaks isolated-runner dev setups that
  PFactory/TFactory already support via the same flag; three conventions become two.
- **Read the env var directly with `os.environ`** (PFactory style). A Settings field is
  what the rest of AIFactory config uses and is testable with SimpleNamespace.

## Risks

- `APP_HOST=` (empty) with `APP_DISABLE_AUTH=true` now refuses to boot. That is the
  intended fix: it bound every interface. No manifest sets an empty host.

- A local developer relying on `APP_DEBUG=true` + `APP_DISABLE_AUTH=true` on
  `0.0.0.0` gets a boot refusal that names the fix.
- Live cluster: none; no manifest sets either variable (checked read-only).

## Verification

- `pytest tests/test_auth_hardening.py`: the debug-on-no-opt-in test fails on the old
  code and passes on the new.
- ruff on touched files; mypy ratchet `scripts/ratchet_lint.py` if present.
