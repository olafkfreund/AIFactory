---
status: approved
issue: 1759
spec: spec/2026-10-15-1759-credential-passthrough-guard.md
---

# Plan: guard that no credential name joins an env passthrough list unreviewed

Paths are relative to the repo root; line numbers at `c89cffd8`.
`V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin`; put it
first on `PATH` (no `.venv` in the worktree).

## Approved decisions (carried from the spec)

1. Credential-shaped = `is_denied_env_key(n) or _CREDENTIAL_NAME.search(n)`.
2. Lists covered: `GITHUB_KEEP`, `RUNNER_KEEP`, `_PASSTHROUGH_BUILD_ENV`,
   `_AGENT_ENV_KEEP`, `_GEMINI_KEEP`. Inline `keep=(...)` tuples are out.
3. Per-list `EXEMPT` sets as in the spec table, compared by equality.
4. Test only; no production code change. #1757 out of scope.

## Steps

### Step 1: add `tests/test_env_passthrough_credentials.py`

New file. Contents:

- Module docstring citing #1759 and #1713.
- `sys.path` setup as `tests/test_child_process_env.py:13-15`
  (`apps/backend` and `apps/web-server` inserted at 0), imports with
  `# noqa: E402`:
  `from core.auth import _AGENT_ENV_KEEP, is_denied_env_key`,
  `from core.child_env import _CREDENTIAL_NAME, GITHUB_KEEP`,
  `from providers.antigravity import _GEMINI_KEEP`,
  `from server.services.build_backend import _PASSTHROUGH_BUILD_ENV`,
  `from server.utils.subprocess_env import RUNNER_KEEP`.
- `LISTS: dict[str, Collection[str]]` mapping each name to the object.
- `EXEMPT: dict[str, frozenset[str]]` with exactly the spec table's sets,
  one short comment per entry saying who reads the names.
- `_credential_names(names)` returns the set of names matching decision 1.
- `@pytest.mark.parametrize("name", sorted(LISTS))`
  `test_passthrough_credentials_are_exempted(name)`: compute
  `found = _credential_names(LISTS[name])`; assert `found == EXEMPT[name]`
  with a message listing `found - EXEMPT[name]` (unreviewed) and
  `EXEMPT[name] - found` (stale), and telling the reader to add a reviewed
  exemption in this file.
- `test_exempt_covers_every_list`: `assert EXEMPT.keys() == LISTS.keys()`.

Check: `PATH=$V:$PATH pytest tests/test_env_passthrough_credentials.py -q`
passes; `ruff check` and `ruff format --check` on the file pass.

Traps: `_PASSTHROUGH_BUILD_ENV` contains `S3_SECRET_KEY` (denied by
pattern, not `*_KEY` rule alone) and `ANTHROPIC_AUTH_TOKEN`;
`_GEMINI_KEEP` has `GOOGLE_APPLICATION_CREDENTIALS` (pattern match only).
Do not edit production code to make the test pass.

### Step 2: regression proof (no commit)

In the worktree, temporarily append `"JWT_SECRET",` to `RUNNER_KEEP`
(`apps/web-server/server/utils/subprocess_env.py:47`), run the step-1
check, confirm it fails naming `RUNNER_KEEP` and `JWT_SECRET`, then
`git checkout -- apps/web-server/server/utils/subprocess_env.py`.

Check: `git diff --quiet apps/` after revert.

## Tests

The new file is the test. Also run
`pytest tests/test_child_process_env.py tests/test_build_backend_kubejob.py -q`
to confirm nothing nearby broke.

## Rollback

Delete `tests/test_env_passthrough_credentials.py`. No runtime effect.
