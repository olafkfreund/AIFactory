---
status: draft
issue: 1821
intent: intent/2026-10-17-1821-codeql-notes-changelog.md
---

# Spec: remove the dead STORE_KEYS constant, unify one test import, add six missing CHANGELOG entries

Line numbers are at `f3c01454`.

## Design

1. **Delete `STORE_KEYS`** (`apps/web-server/server/services/project_settings_store.py:35`)
   and the `PROJECT_ENV_KEYS` import it was the only user of (`:28`).
   Point the module docstring (`:5`) and `_parse_env_file` (`:83`) at
   `is_store_key` instead. Nothing outside the module reads `STORE_KEYS`
   (`git grep` on dev: only this file and the #1795 spec/plan prose).
2. **One import style in `apps/web-server/tests/test_pr_endgame.py`:** drop
   `from server.services.project_settings_store import project_key` (`:29`)
   and call `pss.project_key(...)` at its five uses (`:465`, `:478`, `:488`,
   `:490`, `:733`). The `pss` module import stays because the test reads
   and writes `pss._SNAPSHOT`.
3. **CHANGELOG:** six entries in the existing bold-title style, each
   grounded in its merged PR:
   - Security: #1762 (PR #1817), #1795 (PR #1799), #1696 (PR #1794),
     placed after the #1753 entry, the last of the 3.10.2 security group.
   - Changed: #1646 (PR #1793), after the #1709 entry.
   - Fixed: #1721 (PR #1801), #1796 (PR #1808), at the top of `### Fixed`.

   They go in dev's `## [Unreleased]`. dev has no `## 3.10.2` heading:
   the release branch merges `origin/main` and moves dev's `[Unreleased]`
   entries under the version (PR #1815 body). The other 3.10.2 entries
   (#1745 … #1753, #1698, #1709) sit in dev's `[Unreleased]` the same way,
   so these six join them and the next release moves them with the rest.

## Alternatives rejected

- **Make `is_store_key` use `STORE_KEYS`.** Wrong: `is_project_env_key`
  also accepts `AGENT_MCP_<name>_ADD/REMOVE` and refuses names matching
  `_REFUSED_KEY` (`apps/web-server/server/services/project_env.py:68-73`),
  so a set lookup would change which keys the store accepts.
- **Merge `origin/main` into this branch to get a `## 3.10.2` heading.**
  It drags version bumps and a CHANGELOG restructure (a conflict) into a
  chore PR while other lanes edit `[Unreleased]` in parallel. That sync is
  the release branch's job.
- **Switch the test to `from … import` only.** The test monkeypatches
  `pss._SNAPSHOT`, so it needs the module object.

## Risks

- The six entries land in 3.10.3's notes, not 3.10.2's, unless the next
  release files them under 3.10.2. Flagged in the PR. The issue allows
  "under 3.10.2 or the next release".

## Verification

- `git grep -n STORE_KEYS -- '*.py'` is empty (`_STORE_KEYS`-suffixed
  names elsewhere are unrelated; the grep uses `-w`).
- `cd apps/web-server && pytest tests/test_pr_endgame.py` and the
  project-settings store tests pass.
- `ruff check` and `ruff format --check` on the two Python files.
- No regression test: no behaviour changes (a dead constant and an import).
