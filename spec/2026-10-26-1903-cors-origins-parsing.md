---
status: draft
issue: 1903
intent: intent/2026-10-26-1903-cors-origins-parsing.md
---

# Spec: APP_CORS_ORIGINS comma form must parse; a `null` origin must not keep credentials

## Design

- `apps/web-server/server/config.py`:
  - `CORS_ORIGINS: Annotated[list[str], NoDecode] = [...]` (`NoDecode` from
    `pydantic_settings`, available in the installed 2.14.1) so the env string reaches the
    validator unparsed.
  - `_parse_cors_origins`: a string starting with `[` → `json.loads` (pydantic no longer
    decodes it); otherwise split on `,`, strip, drop empties. Non-strings pass through.
  - `cors_allows_credentials`: return False when any origin, stripped and lowercased, is
    `*` or `null`. The boot guard already keys off this function, so `null` refuses
    to boot like `*`. The guard's warning text names both.
- Tests in `tests/test_auth_hardening.py`: env `*`, `https://a.com,*`, ` * `, the
  comma allowlist and a JSON list all load into the expected list; `null` and
  `https://a.com,null` drop credentials and make the guard refuse.
- Docs: `.env.example` and `docs/docs/environment-reference.md` say both forms parse and
  that `*` or `null` disables credentialed CORS.

## Alternatives rejected

- **Reject `null` at load.** A validation error crashes boot with a pydantic traceback;
  routing it through the existing guard gives one clear message and one opt-in.
- **Switch the field to `str` and split at use sites.** Touches every consumer
  (`main.py`, `routes/email.py`); `NoDecode` is the library's own fix.
- **Custom env source.** More code for the same result.

## Risks

- Anyone whose deploy relied on a comma value crashing: none (it never booted).
- Conflicts with #1902 only near the guard's warning text; trivial.

## Verification

`pytest tests/test_auth_hardening.py`: the new parse and `null` tests fail on origin/dev.
ruff check and ruff format --check on touched files.
