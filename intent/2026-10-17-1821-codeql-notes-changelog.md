---
status: approved
issue: 1821
author: olafkfreund
---

# Intent: remove the dead STORE_KEYS constant, unify one test import, add six missing CHANGELOG entries

Not fixed on dev. Checked at `f3c01454`. Raised by CodeQL on the 3.10.2
release PR (#1815).

## Problem

- `apps/web-server/server/services/project_settings_store.py:35` defines
  `STORE_KEYS`, which nothing reads. The real filter is `is_store_key`
  (`:58-60`). The module docstring (`:5`) and `_parse_env_file` (`:83`)
  still name the dead constant.
- `apps/web-server/tests/test_pr_endgame.py:26` and `:29` import
  `server.services.project_settings_store` both as a module (`pss`) and
  with `from … import project_key`.
- #1762, #1795, #1721, #1796, #1696 and #1646 shipped in 3.10.2 with no
  CHANGELOG entry (the release PR #1815 says so).

## Proposed outcome

No dead constant, docstrings that name the real filter, one import style
for the module in the test, and a CHANGELOG entry for each of the six.

## Affected users and systems

- Readers of the code and the CHANGELOG. No behaviour change.

## Constraints

- `is_store_key` must keep its current behaviour: it also accepts
  `AGENT_MCP_*_ADD/REMOVE` keys and refuses some names in
  `PROJECT_ENV_KEYS`, so it cannot be swapped for a set lookup.
- dev's CHANGELOG has no `## 3.10.2` heading (only main has it; dev keeps
  everything under `[Unreleased]` until a release merges main back).

## Open questions

- Where the six entries go on dev. Answered in the spec.
