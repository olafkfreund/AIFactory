---
status: draft
issue: 1690
spec: spec/2026-10-10-1690-lfs-push-hooks-off.md
---

# Plan: push LFS objects while git hooks stay off

Checkout: `fix/1690-lfs-push-hooks-off` at 8f546926. All line numbers below
were checked against that commit.

## Background

#1680 sets `core.hooksPath=/dev/null` in every env that `child_env()` builds
(`apps/backend/core/child_env.py:53-57`). Because of that, git-lfs's
`pre-push` hook no longer runs, and LFS objects are not uploaded on the
web-server PR pushes. This plan adds an explicit, locked-down `git lfs push`
before those pushes. Hooks stay off.

## Approved decisions (from the spec)

- **D1, no git-lfs in the image.** `Dockerfile:175-187` installs git, gh and
  curl but not git-lfs, and it is not touched. Where git-lfs is missing, no
  LFS step runs, so k8s behaves as it does today. A follow-up issue (opened
  in step 5) covers git-lfs in the image with a `>=` version floor, plus the
  2 askpass push sites.
- **D2, a failed upload is a push failure.** `git lfs push` runs before
  `git push`. On a non-zero exit, or a refusal (D7), the ref push is skipped
  and the site takes its existing push-failure path. No new states are
  added. At `pr_endgame.create_pr` the existing path is warn-and-continue to
  `gh pr create`. That stays, but the ref is not pushed.
- **D3, the endpoint is pinned.** E = `https://github.com/<owner>/<repo>.git/info/lfs`.
  - E is derived from origin's push URL.
  - It is passed as both `-c lfs.url=E` and `-c lfs.pushurl=E`. Pinning
    `lfs.url` alone would let the repo's own pushurl win.
  - The origin URL must whole-string match exactly one
    `https://github.com/<owner>/<repo>`, with an optional `.git` and an
    optional trailing `/`.
  - These are rejected: userinfo, `?`, `#`, extra path segments, `.` or `..`
    as the repo, other hosts, ssh, multiple lines, and empty output.
  - On a reject, the LFS step is skipped with a warning and the push runs as
    it does today.
  - No custom LFS servers are supported.
  - Do not import `GITHUB_HTTPS_PREFIX`. The import would be circular, and it
    is only a prefix match.
- **D4, scope is the 4 web-server gh-credential-helper pushes:**
  - the `pr.py` push;
  - the `completion_orchestration` fix push;
  - the `pr_endgame` `create_pr` push;
  - the `pr_endgame` post-conflict-resolve push.

  `workspace_fetch.py` and `tfactory_client.py` (askpass, which #1688 will
  change) go to the follow-up issue. `git_credentials.py` is not touched.
- **D5, the trigger is `shutil.which("git-lfs")`.** `.gitattributes` is never
  read.
  - `git remote get-url --push --all origin` runs before every push, even on
    hosts without git-lfs. It is one local config read with no network
    access, so the runner call list is the same on every host. This is an
    approved deviation.
  - In `pr.py` it runs inside the existing `try`, so a timeout or OSError
    surfaces as a push failure.
- **D6, timeouts are per spawn.** Each spawn keeps its site's own timeout:
  60s in `pr.py`, and 120s through `_default_runner`. An LFS repo can take
  about twice the old push time. The `Runner` signature is unchanged, and
  there is no shared deadline. This is an approved deviation from the
  intent.
- **D7, transfer-program refusal.**
  - Just before the upload, run
    `git config --name-only --get-regexp '^lfs\.customtransfer\.'` in the
    worktree.
  - Exit 1 means proceed. A match, or any other exit code, refuses the step,
    which is a push failure under D2.
  - The check only runs on hosts with git-lfs and a github.com origin.
  - There is no `lfs.standalonetransferagent` pin, because URL-scoped repo
    keys bypass it.
- **D8, the pinned config.**
  - The pins are `lfs.url=E`, `lfs.pushurl=E`, `lfs.<E>.locksverify=false`
    and `lfs.allowincompletepush=false`.
  - With `locksverify=false`, repos without LFS make no network call and
    exit 0. It also means GitHub LFS locks are never honoured, which is the
    same as today.
  - With `allowincompletepush=false`, a missing object fails the upload.
  - argv: `git -c … lfs push origin <ref>`, where `<ref>` is the same ref the
    site pushes (`HEAD` or the validated branch).
  - No secret goes in argv (#1366).
- **D9, code placement.**
  - A pure helper goes in `apps/backend/core/child_env.py` and is
    re-exported from `server/utils/subprocess_env.py`. It takes the get-url
    output (plus whether git-lfs is present) and returns the argv or None.
  - A wrapper, `push_with_lfs`, goes in `pr_endgame.py`. Every spawn goes
    through the injectable `runner`, and it returns a `CmdResult`.
  - `completion_orchestration` uses the wrapper with `_default_runner`.
  - `pr.py` repeats the sequence inline, to keep its 60s timeout and its
    exception mapping.
  - No new module, no `sys.path` change, no new env names. Every spawn uses
    `child_env(keep=GITHUB_KEEP)`.
- **D10, get-url failure.** If get-url exits non-zero, no LFS step runs, and
  the push runs and fails on its own path as it does today.
- **D11, docs.**
  - The known-limit sentence in `CHANGELOG.md` and in the plan-1680 note
    becomes: "LFS objects are now uploaded explicitly on the 4 server PR
    pushes where git-lfs is installed. A failed upload fails the push. The
    build-Job push, the TFactory push and git-lfs in the image are pending
    (follow-up #N)."
  - Add `lfs.<url>.access`, `lfs.transfer.*`, `http.<url>.*` and
    `url.*.insteadOf` to #1689's list.
- **D12, tests.** New helper, wrapper, integration and site tests, detailed
  under Tests. Existing suites must pass unchanged, except that the needle in
  `test_pr_endgame.py:123` is tightened.

## GATE result (spec Risk 2): verified, does not block

The question was whether old pointers whose objects are absent locally fail
`lfs push` under `allowincompletepush=false` when the remote-tracking refs
are missing, even though the server already has the objects.

**Setup.** git-lfs 3.8.0 against a fake HTTP batch server
(`srv.py` in the session scratchpad):

- clone with `GIT_LFS_SKIP_SMUDGE`;
- delete `.git/lfs/objects`;
- delete `refs/remotes/*`;
- make a new commit on top of an old pointer.

**Results.**

- **The server batch says it already has the object** (no actions): the
  pinned `lfs push origin feat` gives rc=0 and skips the object. This is the
  GitHub case, so nothing breaks.
- **The server asks for an upload:** rc=2, with "(missing) a.bin ... rejected
  due to missing or corrupt local objects". This is the correct failure, and
  it takes the D2 push-failure path.
- **A file:// or standalone-transfer remote** gives rc=2 even when the remote
  has the object, because there is no batch negotiation. D3 rejects every
  non-github.com origin, so this path is never reached. Record it in the
  follow-up issue.

## Line-reference corrections (spec text vs. this checkout)

- **CHANGELOG sentence:** spans `CHANGELOG.md:14-15` (not :15 or :14 alone).
- **Plan-1680 note:** `plan/2026-10-09-1680-child-process-env.md:253-254`.
- **`pr.py`:** `pr.py:335-355` is the push try/except. `push_cmd` is built at
  `325-334` and stays unchanged.
- **SeqRunner:** lives in `tests/test_pr_endgame_conflict_loop.py:28-43`,
  under the repo root `tests/`.
- **`completion_orchestration`:** the `.pr_endgame` lazy imports are at :221,
  :274 and :318.
- **`pr_endgame.py` import of `subprocess_env`:** line 35, not 34.
- **`child_env.py` stdlib imports:** lines 9-11 (`os`, `re`,
  `collections.abc`); line 13 is `from core.auth import ...`.

## Common setup

- Environment: `export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH`.
- Root tests: `python -m pytest tests -q -m "not slow"` (`tests/pytest.ini`
  sets `asyncio_mode = auto`).
- Web-server tests: run from `apps/web-server` with
  `python -m pytest tests -q -o asyncio_mode=auto`.

**Lint gate L.** Run it before every commit:

```
ruff format --check apps/backend apps/web-server scripts tests \
 && ruff check apps/backend apps/web-server scripts tests \
 && python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py' \
 && python scripts/cq_ratchet.py --staged --tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'
```

**Rules for every commit:**

- The commit scope may not contain `#`, for example
  `fix(git): upload LFS objects on server PR pushes (#1690)`.
- Each commit message ends with the attribution trailers.
- Name the plan step being executed.

**Handoff (model split).** There are 4 code steps across 3 or more files.
Step 1 goes to `coder` with this plan path, and steps 2-4 go to the same
agent via `SendMessage`. Step 5 is done by the session model. Review uses a
fresh `opus` agent given only this plan path and `git diff`.

## Steps

### Step 1: red tests (one commit, tests only)

Write every test listed under Tests:

- the new `tests/test_lfs_push.py`;
- the additions to `apps/web-server/tests/test_pr_endgame.py`,
  `tests/test_pr_endgame_conflict_loop.py`,
  `tests/test_create_pr_fetches_branch.py` and
  `tests/test_completion_lands_via_merger.py`;
- the needle change at `test_pr_endgame.py:123`.

→ **Verify by:** run the root and web-server commands under Tests.

- The new tests fail with ImportError or AttributeError (`lfs_push_argv` and
  `push_with_lfs` do not exist yet) or with AssertionError.
- All pre-existing tests stay green.

Commit as `test(git): red tests for explicit LFS upload (#1690)`.

**Traps:**

- **Patching `which`.** Use
  `monkeypatch.setattr("core.child_env.shutil.which", _which)`, where
  `_which` returns `"/x/git-lfs"` for `"git-lfs"` and calls the saved real
  `which` otherwise.
  - This patches the global `shutil`, so the selective fake is what keeps
    every other lookup working.
  - Never import the module as `apps.backend.core.child_env`, or you get a
    second module object and the patch misses.
- **Wrapper and site tests force the present case.** They must patch `which`
  to report git-lfs as present, so they cannot pass silently on a host
  without git-lfs.
- **Fake runners match argv exactly, never by substring.** `"push"` also
  matches `get-url --push`. Classify calls like this:
  - get-url: `argv[:3] == ["git","remote","get-url"]`;
  - config check: `"--get-regexp" in argv`;
  - upload: `"lfs" in argv and argv[argv.index("lfs")+1] == "push"`;
  - push: `argv[:2] == ["git","push"]`.
- **Every helper test except H1 forces the present case too.** H2's warning
  assertion and H3/H4's argv need `which` patched to present, or they fail
  on a host without git-lfs (CI). Only H1 patches it to None.
- **Existing `FakeRunner`/`SeqRunner` route by substring.** The new routes
  there use `"get-url"`, `"--get-regexp"` and `"lfs push"`, which are
  unambiguous. Never add a bare `"push"` route: it matches `get-url --push`.
- **`pr.py` imports `subprocess` inside the function (`pr.py:71`).**
  `pr_routes.subprocess` does not exist. P1-P4 patch the stdlib attribute:
  save `real = subprocess.run`, then
  `monkeypatch.setattr(subprocess, "run", rec)`, where `rec` passes
  everything it does not fake through to `real`. The completion test does
  the same (the fix push goes through `pr_endgame._default_runner`, which
  uses the module-level `subprocess`); there `rec` fakes every call it is
  given, so nothing real runs against the tmp worktree.
- **I1-I4 proxy env.** Also `monkeypatch.delenv` `NO_PROXY`, `no_proxy`,
  `https_proxy` and `http_proxy` (raising=False), so a host proxy setting
  cannot exempt github.com from the dead proxy and mask a network call in
  I2.
- **Root test imports.** Copy the `sys.path` inserts from
  `tests/test_completion_lands_via_merger.py:21-23`. Use module imports, one
  per line, each with `# noqa: E402`:
  - `from server.services import pr_endgame`
  - `from core import child_env`
- **I3 needs a real LFS object.** Without a committed LFS object, the evil
  transfer agent never runs, so the test would pass even with the guard
  deleted.

### Step 2: helper and re-export

**`apps/backend/core/child_env.py:9-11`** (stdlib imports)

- Add `import logging` and `import shutil` in alphabetical order.
- Add `logger = logging.getLogger(__name__)` after the imports. Today the
  module has no logger.

**`apps/backend/core/child_env.py`** (append after line 58)

- Add `lfs_push_argv(push_url_output: str, ref: str) -> list[str] | None`:
  1. If `shutil.which("git-lfs") is None`, return None.
  2. If the output contains `\n` or `\r`, log a warning and return None.
     Callers strip it first, so a single trailing newline is already gone.
  3. Match with
     `re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", s)`.
     On no match, log a warning and return None.
  4. If owner or repo is `.` or `..`, log a warning and return None. This
     also covers a repo of `..git`, which strips to `.`.
  5. Return the argv:
     `["git","-c",f"lfs.url={E}","-c",f"lfs.pushurl={E}","-c",f"lfs.{E}.locksverify=false","-c","lfs.allowincompletepush=false","lfs","push","origin",ref]`,
     where `E = f"https://github.com/{owner}/{repo}.git/info/lfs"`.

**`apps/web-server/server/utils/subprocess_env.py:28-30`**

- Add `lfs_push_argv = _core.lfs_push_argv` next to `GITHUB_KEEP`.
- Add `"lfs_push_argv"` to `__all__`, keeping it sorted.

→ **Verify by:**

```
python -m pytest tests/test_lfs_push.py -q -k "helper or pins or offline"
python -m pytest tests/test_child_process_env.py -q
```

The `-k` expression selects H1-H4, I1, I2 and also I4
(`test_pins_alone_...` contains "pins"), which all pass: I4 needs only the
helper and `_default_runner`. W1-W5 and I3 are not selected and stay red, as
do the site tests. Then run L.

**Traps:**

- **Use `fullmatch`, not `match` with `^…$`.** `$` also matches before a
  trailing `\n`.
- **Warning text.** The warning must not include the raw get-url output,
  because it can carry userinfo. Log a fixed message such as "origin is not a
  plain https://github.com/<owner>/<repo> URL; skipping LFS upload".
- **The repo group must be lazy (`+?`).** A greedy group turns `o/r.git` into
  `r.git.git`.
- **Do not import `core.git_credentials`.** `git_credentials.py:33` imports
  `core.child_env`, so the import would be circular.
- **mypy ratchet.** It needs the return annotation.
- **Rebase overlap.** #1671 edits the end of `child_env.py` and `__all__`
  (`RUNNER_KEEP`), so expect a rebase conflict there.

### Step 3: wrapper and the three pr_endgame/completion sites

**`apps/web-server/server/services/pr_endgame.py:35`**

- Change it to
  `from server.utils.subprocess_env import GITHUB_KEEP, child_env, lfs_push_argv`.

**`apps/web-server/server/services/pr_endgame.py`** (after `_default_runner`, lines 67-76)

- Add `push_with_lfs(push_argv: list[str], ref: str, cwd: str, runner: Runner = _default_runner) -> CmdResult`
  (the default is what `completion_orchestration` uses, so it never touches
  the private name):
  1. Run `url = runner(["git","remote","get-url","--push","--all","origin"], cwd)`.
  2. Set `argv = lfs_push_argv(url.out.strip(), ref) if url.ok else None`.
     This is D10: on a failed get-url, the output is never parsed.
  3. If `argv` is set:
     - run `chk = runner(["git","config","--name-only","--get-regexp", r"^lfs\.customtransfer\."], cwd)`;
     - if `chk.rc != 1`, return
       `CmdResult(1, "", "LFS step refused (lfs.customtransfer.* defined)")`;
     - run `up = runner(argv, cwd)`, and if `not up.ok`, return `up`
       unchanged (the same object).
  4. Return `runner(push_argv, cwd)`.

**`pr_endgame.py:581`** (`create_pr`)

- Change it to
  `push = push_with_lfs(["git","push","-u","origin",branch], branch, str(worktree), runner)`.
- Lines 582-601 are unchanged: warn and keep going to `gh pr create`.

**`pr_endgame.py:1148-1152`** (conflict loop)

- Change it to
  `await asyncio.to_thread(push_with_lfs, ["git","push","--force-with-lease","origin","HEAD"], "HEAD", worktree, runner)`.
- Lines 1153-1170 are unchanged, and they lead to `merge_conflict_unresolved`.

**`apps/web-server/server/services/completion_orchestration.py:316`**
(the line after `import subprocess as _sp`, which is blank)

- Add a module import line, before `from .pr_data_service import ...`:
  `from . import pr_endgame  # noqa: PLC0415`.
- Do not add names to the `from .pr_endgame import ReviewState` line at :318,
  and do not add a second `from .pr_endgame import` line. Both were tried on
  this checkout: a second line gives I001 under both the default config and
  `standards/ruff.toml` (+1 I001, so the ratchet fails), and the merged
  multi-name line gives I001 plus a `ruff format` diff under the default
  config. `from . import pr_endgame` passes both configs and the ratchet.

**`completion_orchestration.py:404-416`**

- Replace the bare `_sp.run([... "push","origin","HEAD"] ...)` with
  `push = pr_endgame.push_with_lfs(["git","push","origin","HEAD"], "HEAD", str(_wt))`
  (the default runner is `_default_runner`: same 120s timeout and
  `child_env(keep=GITHUB_KEEP)` as the bare call it replaces). Then run
  `ruff format` on the file: the call wraps.
- Change the check to `if not push.ok:`, and log `push.err[:200]`.
- Keep `return False`.
- Lines 398-403 (`gh auth setup-git`) are unchanged.

→ **Verify by:**

```
python -m pytest tests/test_lfs_push.py tests/test_pr_endgame_conflict_loop.py tests/test_completion_lands_via_merger.py tests/test_no_unscrubbed_spawn.py -q
cd apps/web-server && python -m pytest tests/test_pr_endgame.py -q -o asyncio_mode=auto
```

All pass. (The 4 P-tests in `test_create_pr_fetches_branch.py`, not run
here, stay red until step 4.) Then run L.

**Traps:**

- **Pass `cwd` positionally.** The `Runner` type is `(argv, cwd)`.
- **Pass the worktree through unchanged.** Line 1148 already passes it as
  the code does today.
- **`.err` is already stripped by the runner.**
- **The config check's rc 1 means "no match".** Write the condition as
  `rc != 1`, not `rc == 0`. Test W2b catches the mistake.
- **mypy at :1148.** `watch_and_finish` takes `worktree: str | None`; it is
  narrowed to `str` by `and worktree` in the enclosing `if`, which
  `push_with_lfs(cwd: str)` relies on. Do not move the call out of that
  block.
- **Ratchet.** `push_with_lfs` spawns nothing itself, so it adds no
  S603/PLW1510. Replacing the bare `_sp.run` push removes one PLW1510 and one
  S607 from `completion_orchestration.py`; `_sp` is still used by
  `gh auth setup-git`, so keep that import.
- **Rebase overlap.** #1672 edits `pr_endgame.py`, so expect a rebase.

### Step 4: pr.py inline sequence

**`apps/web-server/server/routes/pr.py:38`**

- Change it to
  `from server.utils.subprocess_env import GITHUB_KEEP, child_env, lfs_push_argv`.

**`pr.py:335`** (inside the existing `try`, before the push run at line 336)

Every new spawn uses
`subprocess.run(..., cwd=worktree_path, capture_output=True, text=True, timeout=60, env=child_env(keep=GITHUB_KEEP))`.

1. Run `git remote get-url --push --all origin`. If its rc is 0, set
   `argv = lfs_push_argv(r.stdout.strip(), worktree_branch)`; otherwise set
   `argv = None`.
2. If `argv` is set:
   - Run the customtransfer config check. If its rc is not 1, return
     `{"success": False, "error": "Failed to push branch: LFS step refused (lfs.customtransfer.* defined)"}`.
   - Run `argv`. If its rc is not 0, return
     `{"success": False, "error": f"Failed to push LFS objects: {stderr.strip()}"}`.
3. The existing `push_cmd` run follows unchanged.

Lines 325-334 (`push_cmd`) and 349-355 (`except`) are unchanged. The
`except` blocks now also cover the new spawns.

→ **Verify by:**

```
python -m pytest tests/test_create_pr_fetches_branch.py tests/test_no_unscrubbed_spawn.py -q
```

That gives 6 + 2 passed. Then run L.

**Traps:**

- **Each new `subprocess.run` carries `env=child_env(keep=GITHUB_KEEP)`
  literally at the call.** `test_no_unscrubbed_spawn` scans
  `apps/web-server/server/**/*.py`.
- **The new spawns go inside the `try`.** P4 maps a timeout to
  "Push timed out".
- **Ratchet (strict config).** The existing push at `pr.py:336` already
  carries ASYNC221, S603 and PLW1510 under `standards/ruff.toml`. Each new
  `subprocess.run` adds one of each, plus S607 when argv is a literal
  `["git", ...]` list, and the per-rule ratchet fails. Copy the fetch call's
  suppressions at `pr.py:305-306`: `# noqa: S603, ASYNC221, PLW1510` on the
  `subprocess.run(` line and `# noqa: S607` on a literal argv line. The
  `lfs_push_argv` spawn takes a variable, so it needs no S607.

### Step 5: docs, follow-up issue, plan update, PR

**Docs**

- `CHANGELOG.md:14-15`: replace "Git LFS objects are not uploaded on these
  pushes yet (#1690)." with the D11 sentence, using the follow-up issue
  number. Keep the "Follow-ups:" clause.
- `plan/2026-10-09-1680-child-process-env.md:253-254`: replace "Known
  limit, follow-up #1690" with "LFS is now uploaded explicitly on the 4
  server PR pushes (#1690). The remaining items are in #N."
- Make both edits in the same commit as the code they describe. They may go
  in step 4's commit if the follow-up number is known by then.

**Follow-up issue** (session model, after user OK). It covers:

- git-lfs in the image with a `>=` floor (`Dockerfile:175-187`);
- `apps/backend/core/workspace_fetch.py:135-146`;
- `apps/backend/pfactory/tfactory_client.py:397-408` (after #1688);
- the GATE note that a file:// or standalone remote fails under
  `allowincompletepush=false`.

**#1689.** Add `lfs.<url>.access`, `lfs.transfer.*`, `http.<url>.*` and
`url.*.insteadOf` to its list, as a comment on #1689 (session model, after
user OK).

**This plan.** Record any deviation in the same commit as the code.

**PR body:**

- links to the intent, spec and plan;
- which steps the coder did;
- the overlaps: #1671 (`child_env.py` end and `subprocess_env.__all__`,
  `RUNNER_KEEP`), #1672 (`pr_endgame.py`), and #1671/#1673
  (`tests/test_child_process_env.py`, not edited here, but its count of 16
  in the expected totals moves after their rebase). #1669/#1670
  (`agent_kubejob.py`) do not touch any file here.

→ **Verify by:** the full Tests section, plus L.

**Traps:**

- **`[Unreleased]`.** The CHANGELOG entry stays under `[Unreleased]`.
- **Branches are stale.** Local branch refs are about 430 commits behind
  main. Re-check overlaps with `git log --name-only origin/main..HEAD` at
  rebase time.

### Not touched (D1/D4)

- `Dockerfile:175-187`
- `apps/backend/core/workspace_fetch.py:135-146`
- `apps/backend/pfactory/tfactory_client.py:397-408`
- `apps/backend/core/git_credentials.py:37,75`

## Tests

### Baseline on 8f546926

| Suite | Tests |
| --- | --- |
| `apps/web-server/tests/test_pr_endgame.py` | 55 |
| `test_pr_endgame_merge_gate.py` | 26 |
| `test_pr_endgame_path_risk_floor.py` | 10 |
| `test_pr_endgame_review_tier.py` | 32 |
| `tests/test_pr_endgame_conflict_loop.py` | 3 |
| `tests/test_create_pr_fetches_branch.py` | 2 |
| `tests/test_completion_lands_via_merger.py` | 4 |
| `tests/test_no_unscrubbed_spawn.py` | 2 |
| `tests/test_git_push_credential_helper.py` | 5 |
| `tests/test_child_process_env.py` | 16 |

`gen_autonomy_matrix.py --check` prints
`ok: tiers=10 overlay=12 val=8 paths=28 gates=3 controls=13`.

### New file `tests/test_lfs_push.py`

30 tests. Without git-lfs, 4 skip and 26 pass. Throughout,
E = `https://github.com/o/r.git/info/lfs`.

**Helper tests**

| Test | Setup | Asserts |
| --- | --- | --- |
| H1 `test_helper_none_when_git_lfs_missing` | `which` → None, valid URL | Result is None. |
| H2 `test_helper_rejects` (15 cases, listed below) | — | None; a WARNING from `core.child_env`; `"SECRET" not in caplog.text`. |
| H3 `test_helper_exact_argv` (`o/r`, `o/r.git`, `o/r.git/`) | `GITHUB_TOKEN=ghp_SECRET` | The exact D8 argv with ref `HEAD`; no `ghp_SECRET` and no `@` in the joined argv. |
| H4 `test_helper_accepts_real_names` | `https://github.com/my-org/my.repo_1` | E = `https://github.com/my-org/my.repo_1.git/info/lfs`. |

The 15 H2 cases:

- `git@github.com:o/r.git`
- `ssh://git@github.com/o/r`
- `http://github.com/o/r`
- `https://gitlab.com/o/r`
- `https://github.com.evil.com/o/r`
- `https://x-access-token:SECRET@github.com/o/r`
- `…/o/r?x=1`
- `…/o/r#x`
- `…/o/..`
- `…/o/.`
- `…/o/..git`
- `…/../r`
- `…/o/r/x`
- two lines
- `""`

**Wrapper tests** (recording fake runner, `which` forced to present)

| Test | Fake runner | Asserts |
| --- | --- | --- |
| W1 `test_upload_failure_skips_push` | Upload returns `CmdResult(2,"","boom")` | The result `is` that object; no push call. |
| W2a `test_customtransfer_defined_refuses` | Config check rc 0 | Not ok; calls are exactly [get-url, check]. |
| W2b `test_customtransfer_check_error_refuses` | Config check rc 128 | Same as W2a. |
| W3 `test_all_ok_order_and_push_argv_unchanged` | All calls ok | Order is get-url, check, upload, push; the last call equals `push_argv`; every cwd is `"/wt"`; the result is the push result. |
| W4 `test_get_url_failure_pushes_without_lfs` | get-url returns `CmdResult(2,"https://github.com/o/r","err")` | Calls are exactly [get-url, push]. |
| W5 `test_non_github_remote_pushes_without_lfs` | get-url returns `/srv/origin.git` | Calls are exactly [get-url, push]. |

**Integration tests** (`skipif(shutil.which("git-lfs") is None)`)

The fixture builds this repo in `tmp_path`:

- `git init`, then `remote add origin https://github.com/o/r`.
- A commit made with `-c user.* -c commit.gpgsign=false`.
- `.lfsconfig` contains `[lfs] url = https://evil.invalid/a`.
- `.git/config` sets:
  - `lfs.url=https://evil.invalid/c`
  - `lfs.pushurl=https://evil.invalid/b`
  - `lfs.allowincompletepush=true`
  - `lfs.locksverify=true`

It sets this environment through monkeypatch:

- `HOME=tmp_path/home`
- `GIT_CONFIG_NOSYSTEM=1`
- `GIT_TERMINAL_PROMPT=0`
- `HTTPS_PROXY` and `HTTP_PROXY` set to `http://127.0.0.1:9`
- `GITHUB_TOKEN` and `GH_TOKEN` removed

Spawns go through `pr_endgame._default_runner`. In the table,
`base = argv[:argv.index("lfs")]`.

| Test | Setup | Asserts |
| --- | --- | --- |
| I1 `test_pins_override_lfsconfig_and_git_config` | Fixture repo | `base+["lfs","env"]` contains `f"Endpoint={E} ("`; `base+["config","--get",X]` gives E for `lfs.pushurl`, `false` for `lfs.allowincompletepush` and `false` for `lfs.{E}.locksverify`. `lfs env` never shows the push URL, so the pushurl check is required. |
| I2 `test_no_object_push_is_offline_and_credential_free` | Fixture repo | `_default_runner(argv, repo).rc == 0`. The proxy is dead, so any network call would give rc 2. |
| I3 `test_evil_standalone_agent_is_refused` | See below | Not ok; PWNED is absent; no upload call and no push call were recorded. |
| I4 `test_pins_alone_do_not_stop_the_agent` (positive control) | I3's setup, then run `_default_runner(argv, repo)` directly | PWNED exists. This proves I3's guard is load-bearing. |

I3 setup:

- `git lfs install --local`.
- `git lfs track '*.bin'`, then commit a 100-byte `a.bin`.
- Precondition: `git lfs ls-files` output is non-empty.
- Set `lfs.customtransfer.evil.path` to a script that touches
  `tmp_path/PWNED` and exits 1.
- Set `lfs.<E>.standalonetransferagent=evil`.
- Call `push_with_lfs(["git","push","origin","HEAD"], "HEAD", str(repo), rec)`,
  where `rec` passes calls through to `_default_runner` but records any
  `git push` and fakes it as ok.

### Changes to existing files

**`apps/web-server/tests/test_pr_endgame.py`** (55 → 56)

- **Line 123:** change it to
  `push_i = next(i for i, c in enumerate(r.calls) if "git push" in " ".join(c))`.
- **New `test_create_pr_upload_failure_still_opens_pr`.** FakeRunner routes:
  - get-url → rc 0, `https://github.com/o/r`;
  - `--get-regexp` → rc 1;
  - `lfs push` → rc 2;
  - `pr create` → `https://github.com/o/r/pull/11`.

  Asserts: `pr == 11`, `r.saw("lfs push")`, and no call with
  `c[:2] == ["git","push"]`.
- **FakeRunner (lines 36-49) is unchanged.** Unknown calls return rc 0 with
  empty output, so no LFS step runs.

**`tests/test_pr_endgame_conflict_loop.py`** (3 → 4)

- **New `test_conflict_push_lfs_failure_is_unresolved`.** Use test 1's
  SeqRunner script plus:
  - `"get-url": [CmdResult(0,"https://github.com/o/r","")]`
  - `"--get-regexp": [CmdResult(1,"","")]`
  - `"lfs push": [_FAIL]`

  Asserts: the reason is `merge_conflict_unresolved`,
  `runner.ran("lfs push")`, and
  `not runner.ran("git push --force-with-lease")`.
- **SeqRunner (28-43) is unchanged.**

**`tests/test_create_pr_fetches_branch.py`** (2 → 6)

These reuse `packed_path_repos` and `fake_gh`, with `which` forced to
present. They wrap `pr_routes.subprocess.run` with a recorder that:

- answers get-url with `https://github.com/acme/proj\n`;
- passes everything else through to the real `subprocess.run`.

The 2 existing tests have a local-path origin, so the helper returns None and
they are unaffected.

| Test | Fake | Asserts |
| --- | --- | --- |
| P1 `test_lfs_upload_failure_fails_create_pr` | Check rc 1; upload rc 2 with "boom" | `success is False`; the error contains "Failed to push LFS objects"; no `git push` call. |
| P2 `test_lfs_customtransfer_refuses_create_pr` | Check rc 0 | The error contains "LFS step refused"; no upload and no push. |
| P3 `test_lfs_runs_before_push` | Upload rc 0; the real push goes to the local origin | Status 200; upload index < push index. |
| P4 `test_lfs_timeout_maps_to_push_timed_out` | Upload raises `TimeoutExpired` | Error is "Push timed out"; no push call. |

**`tests/test_completion_lands_via_merger.py`** (4 → 5)

New `test_fix_push_lfs_failure_returns_false`.

Setup:

- Use the `_finish` harness with `_make_spec(..., commits=["a1"])` and
  `completed=True`.
- Patch these on `server.services.pr_endgame`:
  - `is_auto_pr_enabled` → True;
  - `gather_pr_context` →
    `{"worktree": str(wt), "branch": "b", "base": "main", "repo": "o/r"}`;
  - `resolve_pr_reviewer` → `"aifactory"`;
  - `run_pr_endgame` → `AsyncMock()`.
- Patch `qa.correction.apply_correction` with `AsyncMock()`.
- Force `which` to present.
- Replace `subprocess.run` with a recorder:
  - `gh auth setup-git` → rc 0;
  - get-url → a github URL;
  - config check → rc 1;
  - upload → rc 2.

Call:

- `fix_fn = run_pr_endgame.call_args.kwargs["fix_fn"]`.
- Then `await asyncio.to_thread(fix_fn, [])`. `fix_fn` uses `asyncio.run`
  internally, so it cannot run on the test's event loop.

Asserts: the result is False, an upload was recorded, and no `git push` was
recorded.

**Must pass unchanged:**

- `tests/test_no_unscrubbed_spawn.py`
- `tests/test_git_push_credential_helper.py`
- `tests/test_child_process_env.py`

### Commands and expected results

1. Targeted root suites. Expect 68 passed, or 64 passed and 4 skipped
   without git-lfs:

   ```
   python -m pytest tests/test_lfs_push.py tests/test_pr_endgame_conflict_loop.py tests/test_create_pr_fetches_branch.py tests/test_completion_lands_via_merger.py tests/test_no_unscrubbed_spawn.py tests/test_git_push_credential_helper.py tests/test_child_process_env.py -q
   ```

2. Web-server suites. Expect 124 passed (56+26+10+32):

   ```
   cd apps/web-server && python -m pytest tests/test_pr_endgame.py tests/test_pr_endgame_merge_gate.py tests/test_pr_endgame_path_risk_floor.py tests/test_pr_endgame_review_tier.py -q -o asyncio_mode=auto
   ```

3. Full suites, as CI runs them. Expect 0 failed. The root suite is the main
   baseline + 36 (30 in `test_lfs_push.py`, plus 1 + 4 + 1 in the existing
   root files). The web-server suite is its baseline + 1:

   ```
   python -m pytest tests -q -m "not slow"
   python -m pytest apps/web-server/tests -q -o asyncio_mode=auto
   ```

4. Lint gate L. Expect it to pass.

5. Autonomy matrix. Expect
   `ok: tiers=10 overlay=12 val=8 paths=28 gates=3 controls=13`:

   ```
   python scripts/gen_autonomy_matrix.py --check
   ```

   - It does not cite the touched files by line. `subprocess_env` is already
     in `pr_endgame`'s import closure.
   - On drift: regenerate with `python scripts/gen_autonomy_matrix.py`, then
     commit the regenerated output in the same commit.

6. Manual check before merge. Against a scratch github.com repo with
   `GIT_TRACE=1`, push a branch with one LFS object through `create_pr`.
   Expect:
   - the object is on GitHub;
   - a fresh clone smudges it;
   - `GIT_TRACE` shows the upload going only to E.

## Rollback

1. **Keep commits separate.** Implementation and tests are in their own
   commits, and the intent, spec and plan docs are in separate commits, so a
   revert never touches the docs.
2. **Revert.** Run `git revert --no-edit <impl-sha>…` for the step 1-5 code
   commits, newest first. Commit it as
   `revert(git): restore bare PR pushes (#1690)`. This undoes:
   - `tests/test_lfs_push.py` and the new tests;
   - the bare pushes;
   - line 123;
   - the CHANGELOG text;
   - the plan-1680 note.
3. **Confirm the baseline is back.**
   - Counts: `test_pr_endgame.py` 55, `test_pr_endgame_conflict_loop.py` 3,
     `test_create_pr_fetches_branch.py` 2,
     `test_completion_lands_via_merger.py` 4, `test_no_unscrubbed_spawn.py` 2.
   - `grep -rn "lfs_push_argv\|push_with_lfs" apps tests` finds nothing.
   - `gen_autonomy_matrix.py --check` prints ok.
4. **Redeploy.** There is no runtime flag, persisted state, config or
   migration; a rollback is a revert and redeploy. Until the redeploy, hosts
   without git-lfs (including the default k8s image) already skip the LFS
   step, and only the extra get-url spawn remains.
5. **Re-open #1690** with the reason, and update this plan's status in the
   revert PR.
