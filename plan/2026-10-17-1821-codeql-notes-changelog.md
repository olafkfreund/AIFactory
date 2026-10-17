---
status: approved
issue: 1821
spec: spec/2026-10-17-1821-codeql-notes-changelog.md
---

# Plan: remove the dead STORE_KEYS constant, unify one test import, add six missing CHANGELOG entries

Paths relative to the repo root; line numbers at `f3c01454`.
`V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin`; put it
first on `PATH` (no `.venv` in the worktree).

## Approved decisions (carried from the spec)

1. Delete `STORE_KEYS`; do not make `is_store_key` use it (it would change
   which keys are accepted).
2. Docstrings name `is_store_key` instead.
3. The test keeps `import … as pss` and drops the `from … import project_key`.
4. The six CHANGELOG entries go in dev's `## [Unreleased]`, in the section
   that matches each, next to the other 3.10.2 entries. No merge of
   `origin/main`.

## Steps

### Step 1: delete `STORE_KEYS` (`apps/web-server/server/services/project_settings_store.py`)

- `:35`: delete `STORE_KEYS: frozenset[str] = PROJECT_ENV_KEYS | {"CUSTOM_MCP_SERVERS"}`
  and the blank line after it.
- `:28`: `from server.services.project_env import PROJECT_ENV_KEYS, is_project_env_key`
  becomes `from server.services.project_env import is_project_env_key`.
- `:5`: ``(keeping only ``STORE_KEYS``)`` becomes
  ``(keeping only keys ``is_store_key`` accepts)``; rewrap the docstring
  lines if over 88 chars.
- `:83`: ``keeping only ``STORE_KEYS``.`` becomes
  ``keeping only keys ``is_store_key`` accepts.``
- Check: `git grep -nw STORE_KEYS -- '*.py'` is empty;
  `cd apps/web-server && PATH=$V:$PATH python -m pytest -q tests/test_project_settings_store.py tests/test_project_settings_writers.py`;
  `PATH=$V:$PATH ruff check <file> && ruff format --check <file>`.
- Trap: `PROJECT_ENV_KEYS` must have no other use in the file
  (`grep -n PROJECT_ENV_KEYS`), or ruff F401 will not fire but keep it.

### Step 2: one import style (`apps/web-server/tests/test_pr_endgame.py`)

- Delete `:29` `from server.services.project_settings_store import project_key  # noqa: E402`.
- Replace `project_key(` with `pss.project_key(` at `:465`, `:478`, `:488`,
  `:490`, `:733` (the only uses).
- Check: `cd apps/web-server && PATH=$V:$PATH python -m pytest -q tests/test_pr_endgame.py`;
  ruff check and format check on the file.
- Trap: do not touch the `pr_endgame` imports at `:27-28`; both are
  `from` style, which CodeQL does not flag.

### Step 3: CHANGELOG entries (`CHANGELOG.md`)

Each entry: bold one-line title ending with `(#<issue>).`, then a short
wrapped body, as the neighbours. Facts come from the merged PR.

- After the #1753 entry (ends `:79`), in `### Security`:
  - #1762 (PR #1817): agent CLIs run with a private 0700 HOME holding only
    their own login file and a minimal `.gitconfig`; nothing falls back to
    the real HOME; with the env allowlist from the same PR, an agent child
    can no longer read the parent's secrets or `$HOME` credential files.
    Operators: agents lose `~/.ssh`, `SSH_AUTH_SOCK` and home tool configs;
    `AIFACTORY_AGENT_ENV_EXTRA` adds env names; MCP servers name host vars
    with `env_keys`. #1724's /proc window stays open on local runners.
  - #1795 (PR #1799): project settings move from `<project>/.aifactory/.env`
    to the web-server database (`project_settings`, sealed); one-time import
    and rename to `.env.migrated-<date>`; the web-server passes MCP config
    as `AIFACTORY_PROJECT_MCP_CONFIG`; an agent writing the file changes
    nothing trusted.
  - #1696 (PR #1794): TFactory handback receipt recorded in the
    `handback_receipts` table; auto-merge is withheld when a row exists,
    fails closed on read error; the file check stays.
- After the #1709 entry (ends `:271`), in `### Changed`, plain-bullet style:
  - #1646 (PR #1793): gemini key preflight and the TFactory handoff
    preflight; `AIFACTORY_AUTH_PREFLIGHT` warn/enforce/off; only a definite
    rejection blocks, and only in `enforce`.
- At the top of `### Fixed` (`:305`), bold-title style with a blank line
  between entries as in that section:
  - #1721 (PR #1801): image ships git-lfs; all push sites upload LFS
    objects for github.com https origins whose repo uses LFS; best-effort;
    nothing writes git config.
  - #1796 (PR #1808): build Job forwards `AIFACTORY_GATE_TIMEOUT_SECONDS`
    and `AIFACTORY_SANDBOX_NETWORK`; `AIFACTORY_DATA_ROOT` deliberately not.
- Check: `grep -c -E '#(1762|1795|1721|1796|1696|1646)\)' CHANGELOG.md`
  shows each number in a title; `git diff --stat` touches only CHANGELOG.md
  in this step.

## Tests

No new test: no behaviour change. Steps 1 and 2 run the existing suites
named above.

## Rollback

Revert the PR. No data, schema or config change.
