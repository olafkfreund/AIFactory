---
status: draft
issue: 1759
author: olafkfreund
---

# Intent: guard that no credential name joins an env passthrough list unreviewed

Not fixed on dev. Checked at `c89cffd8`: the only related guard is
`tests/test_build_backend_kubejob.py:1983` (`PRIVATE_KEY` never on
`_PASSTHROUGH_BUILD_ENV`). Follow-up from #1713 (spec
`spec/2026-10-11-1713-agent-env-scrub-gaps.md`, section 6).

## Problem

Child processes get a scrubbed env: `core.auth.is_denied_env_key`
(`apps/backend/core/auth.py:173`) and the `*_KEY`/`*_TOKEN` rule
(`apps/backend/core/child_env.py:32`) drop credentials. Named passthrough
lists then put some back:

- `GITHUB_KEEP` (`apps/backend/core/child_env.py:26`)
- `RUNNER_KEEP` (`apps/web-server/server/utils/subprocess_env.py:47`)
- `_PASSTHROUGH_BUILD_ENV` (`apps/web-server/server/services/build_backend.py:259`)
- `_AGENT_ENV_KEEP` (`apps/backend/core/auth.py:110`)
- `_GEMINI_KEEP` (`apps/backend/providers/antigravity.py:65`)

Adding one name to these lists silently undoes the scrub for it. Nothing
fails today when that happens.

## Proposed outcome

A test fails when a credential-shaped name is on one of these lists and is
not in an explicit, reviewed exemption set kept next to the test. Adding a
credential to a list then needs a second, visible edit in the test.

## Affected users and systems

Developers and reviewers only. No runtime change.

## Constraints

- Test only; no production code change.
- Use the repo's own credential definitions (`is_denied_env_key`,
  `_CREDENTIAL_NAME`), not a new pattern.
- #1757 (allowlist instead of deny list) is out of scope.

## Open questions

- Inline `keep=(...)` tuples at call sites (e.g. `providers/codex.py:193`)
  are not named lists. Proposed: out of scope; they are reviewed at the call.
