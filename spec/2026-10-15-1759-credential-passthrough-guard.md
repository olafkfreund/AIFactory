---
status: draft
issue: 1759
intent: intent/2026-10-15-1759-credential-passthrough-guard.md
---

# Spec: guard that no credential name joins an env passthrough list unreviewed

Line numbers are at `c89cffd8`.

## Design

New file `tests/test_env_passthrough_credentials.py`, one parametrized test
over the five named lists from the intent.

- A name is **credential-shaped** when `is_denied_env_key(name)`
  (`apps/backend/core/auth.py:173`) or `_CREDENTIAL_NAME.search(name)`
  (`apps/backend/core/child_env.py:32`) is true. These are exactly the two
  rules `child_env()` uses to drop a name, so "credential" means "a name the
  scrub would otherwise remove".
- `EXEMPT: dict[str, frozenset[str]]` in the test file pins, per list, the
  credential-shaped names it carries today, each group with a one-line why
  (the reader that needs it, the issue that decided it):

  | list | exempt today |
  |---|---|
  | `GITHUB_KEEP` | `GITHUB_TOKEN`, `GH_TOKEN` |
  | `RUNNER_KEEP` | the two GitHub tokens, `OPENAI_API_KEY`, `OPENAI_COMPATIBLE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`, `VOYAGE_API_KEY`, `OLLAMA_API_KEY` |
  | `_PASSTHROUGH_BUILD_ENV` | `ANTHROPIC_AUTH_TOKEN`, the two GitHub tokens, `OPENAI_API_KEY`, `OPENAI_COMPATIBLE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `OLLAMA_API_KEY`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` |
  | `_AGENT_ENV_KEEP` | `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN` |
  | `_GEMINI_KEEP` | `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS` |

- Assertion: the set of credential-shaped names on the list **equals** the
  exemption. Equality, not subset: a name removed from a list must also be
  removed from `EXEMPT`, so the exemption never grows stale and cannot hide a
  later re-add. The failure message names the list and the unexpected or
  missing names and says to add a reviewed exemption.

## Alternatives rejected

- **No credential may be on any list.** Every list carries credentials by
  design (the runner, Jobs and providers read them); the test could not pass.
- **Subset check.** A stale exemption would let a removed key come back
  without review.
- **Scan inline `keep=(...)` tuples with an AST walk.** Larger test for call
  sites that are already reviewed where they are written; not asked for.
- **A separate hard ban list (e.g. `ANTHROPIC_API_KEY`, `JWT_SECRET`).**
  Equality already forces a reviewed test edit for any new name; a second
  list adds nothing a reviewer would not see.
- **Allowlist instead of deny list.** That is #1757.

## Risks

- Adding a legitimate provider key now needs a test edit. That is the point.
- Importing `providers.antigravity` and `server.services.build_backend` in
  one test process: both already import in `tests/` today
  (`tests/test_build_backend_kubejob.py:36`).
- A change to `is_denied_env_key` or `_CREDENTIAL_NAME` can change which
  names count; the test then fails and the exemptions are re-reviewed.

## Verification

- `pytest tests/test_env_passthrough_credentials.py` passes on the branch.
- Regression proof: append `"JWT_SECRET"` to `RUNNER_KEEP` in a scratch
  copy; the test fails naming `RUNNER_KEEP` and `JWT_SECRET`. Revert.
- `ruff check` and `ruff format --check` on the new file pass.
