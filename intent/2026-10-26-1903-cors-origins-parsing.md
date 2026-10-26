---
status: draft
issue: 1903
author: olafkfreund
---

# Intent: APP_CORS_ORIGINS comma form must parse; a `null` origin must not keep credentials

## Problem

Issue #1903, found in the #1902 review, part of olafkfreund/Factory#312.

1. `Settings.CORS_ORIGINS` is `list[str]`. pydantic-settings JSON-decodes it from env
   before the `_parse_cors_origins` validator runs, so every comma-separated value
   (`*`, `https://a.com,*`, ` * `, and the documented `https://a.com,https://b.com`)
   crashes boot with `SettingsError`. Only the JSON-list form works. A wildcard
   therefore gives a pydantic traceback, not the `enforce_cors_safety` message.
2. `cors_allows_credentials` only looks for `*`. With `"null"` in the list, credentials
   stay on and Starlette allows `Origin: null`, which sandboxed iframes and `data:`
   URLs send, so any page can use it.

## Proposed outcome

Both forms parse: JSON list, or a comma list with whitespace stripped. A `null` origin
is treated like `*`: credentials are dropped, and the boot guard refuses it unless its
opt-in is set. Docs describe what parses.

## Affected users and systems

- AIFactory `server/config.py`, `tests/test_auth_hardening.py`, the env reference doc,
  `.env.example`.
- Live cluster: none. factory-gitops `apps/*/manifests` sets no CORS variable (checked
  read-only 2026-10-26). No dev shell, compose file, chart or CI job sets it.

## Constraints

- No gitops/cluster/credential change, no deploy.
- Off origin/dev, independent of PR #1902 (that PR changes the guard's opt-in; this one
  touches the parser and `cors_allows_credentials`).

## Open questions

None.
