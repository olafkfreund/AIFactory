---
status: approved
issue: 1632
spec: spec/2026-10-10-1632-openapi-feature-flags.md
---

# Plan: the OpenAPI spec must not depend on the generator's environment

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1632`, branch
`fix/1632-openapi-feature-flags`, base HEAD `efd4b433` (spec approved; contains
`origin/dev` `c59a0ea6`). `apps/web-server/static/` is absent. There is no
existing test for `scripts/generate-openapi-spec.py`.

Five steps. Files edited: the generator, `techdocs.yml` and `CHANGELOG.md`
(`openapi.yaml` only if its bytes differ). Three files, so under the model
split steps 1-3 go to the `coder` agent: start it with this path and step 1,
then send steps 2 and 3 to the same agent with `SendMessage`. Opus does step 0
and step 4, and the review runs on a fresh `opus` agent given only this file
and `git diff`.

No test file is added (spec Q4). The plan's earlier draft added
`apps/web-server/tests/test_generate_openapi_spec.py`; that went beyond the
approved spec and was removed.

Tool paths used below (run everything from the worktree root):

- `V=/mnt/code/Source-home/GitHub/AIFactory/apps/web-server/.venv/bin` (web-server venv, Python 3.12)
- `B=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin` (ruff, mypy, pre-commit)
- Before any commit: `PATH=$B:$PATH` so the pre-commit hook runs.

## Approved decisions (self-contained summary)

1. **D1 Canonical flag set is all off.** That is the default deployment. It
   matches the committed spec (295 paths) and needs no xmlsec/python3-saml in CI.
2. **D2 The pin lives in the generator only.** Inside `main()`, before
   `from server.main import app`, assign `"false"` directly into `os.environ`
   (not `setdefault`, not `pop`) for exactly these five names:
   `SAML_ENABLED`, `SCIM_ENABLED`, `AIFACTORY_MCP_REMOTE_ENABLED`,
   `AIFACTORY_RMUX_ENABLED`, `APP_RMUX_ENABLED`. This wins over:
   - `env_bootstrap.py:27`, which loads `.env` with `setdefault`;
   - pydantic `env_file=".env"` (`config.py:246-250`), where process env ranks above the file;
   - flags exported in the shell.
3. **D3 The pin goes inside `main()`, not at module top**, so importing the
   script has no side effects.
4. **D4 Write the output as UTF-8.** Use `OUT.open("w", encoding="utf-8")`, so
   the bytes do not depend on the locale. The API description has an em dash
   (`main.py:441`), and the old `open(OUT, "w")` raises `UnicodeEncodeError`
   under `LC_ALL=C`. This also removes one PTH123 ruff finding.
5. **D5 Add one docstring line to the generator.** It says the output is
   pinned to all flags off, independent of `.env`, exported flags and cwd, and
   that `apps/web-server/static/` must be absent.
6. **D6 `techdocs.yml` gets comments and echo text only:**
   - a comment above `Regenerate OpenAPI spec` naming the five pinned flags;
   - one `echo` in the fix hint about independence from `.env` and flags, and the absence of `static/`.

   `continue-on-error` stays off (#906). The step logic does not change.
7. **D7 Regenerate `apps/web-server/openapi.yaml`.** Commit it only if its
   bytes differ. Expect 295 paths and no diff.
8. **D8 No runtime changes.** These files are untouched:
   - `server/main.py`, `server/rmux/integration.py`, `server/mcp_remote/__init__.py`;
   - `server/config.py`, `server/env_bootstrap.py`, `.env.example`;
   - Helm;
   - `scripts/bump-version.js`, which belongs to #1631.
9. **D9 No spec markers or `x-` tags for gated routes.** Nothing gated is emitted.
10. **D10 No automated two-env spec-equality guard in this change.** The
    `ponytail:` comment names the fixed flag list as the known ceiling, and a
    follow-up issue tracks the guard. Per spec Q4 ("No new test in this
    change"), no test file is added at all: the fix is verified by the manual
    matrix in step 2 and the mutation checks under Tests.
11. **D11 #1631 and #1632 ship separately.** Whichever of them (or #1671)
    merges second regenerates `openapi.yaml`. Never hand-merge the yaml.
12. **D12 Lowercase variants such as `app_rmux_enabled` are a known limit.**
    The rejected importlib/case-variant-delete design is the graft if this is
    ever needed.
13. **D13 A built frontend is out of scope.** When `static/` exists, the
    placeholder `GET /` (`main.py:728-736`) disappears and the spec has 294
    paths. Fixing that needs a `main.py` change. The docstring and the CI
    hint mitigate it, and a follow-up tracks it.
14. **D14 File three follow-up issues:**
    - (a) a two-env spec-equality guard;
    - (b) publishing the opt-in API surfaces to Backstage;
    - (c) the spec depending on `static/` being absent (`include_in_schema=False`, or a constant `/` route).
15. **D15 Verification:**
    - Manual: before the fix there are more than 295 paths; after it, 295 paths with an identical sha256 across environments.
    - CI gates: ruff, the strict ruff and mypy ratchet, CodeQL, `gen_autonomy_matrix.py --check`, and techdocs refresh-and-validate.

**Line drift.** The spec's `main.py` citations are stale on this branch. The
actual lines are:

| What | Actual | Spec |
| ---- | ------ | ---- |
| env_bootstrap import | 22 | |
| em-dash description | 441 | 423 |
| SAML gate | 541 | 523 |
| SCIM gate | 545 | 527 |
| SAML block | 532-548 | |
| mcp_remote mount | 631-637 | 615 |
| rmux mount | 684-690 | 668 |
| placeholder `GET /` | 728-736 | 711-717 |

Comments written by this plan cite the actual lines. All other cited ranges
are verified accurate: generator 8-9/14/25-26/29, `env_bootstrap.py:27`,
`config.py:246-250`, `mcp_remote/__init__.py:50`, `rmux/integration.py:45-56`
and `techdocs.yml:152-160/193`.

**Probe facts (read-only, before the fix):**

| Environment | Paths |
| ----------- | ----- |
| No flags | 295 (`/api/tasks/{task_id}/agent-console/sse` present) |
| SAML, SCIM, MCP_REMOTE and AIFACTORY_RMUX set to true | 308 |
| Only `APP_RMUX_ENABLED=true` | 297 |

- With the four flags on, the leaked groups are `/api/auth/saml*`, `/scim/v2*`,
  `/api/mcp-remote*` and `/api/tasks/{spec_id}/agent-console/{attach,detach}`.
- `APP_RMUX_ENABLED` leaks through the `get_settings()` fallback even when
  `AIFACTORY_RMUX_ENABLED` is unset.
- Under `LC_ALL=C PYTHONUTF8=0 PYTHONCOERCECLOCALE=0`, only the yaml write fails.
- Every generator run is a fresh interpreter, so each run sees its own env.
- Under an ASCII locale the script's own final `print` (em dash in
  `Wrote ... —`) raises after the yaml is written. Every ASCII-locale run
  sets `PYTHONIOENCODING=utf-8`; the print is not in the spec, so it stays.

## Steps

`F` below means the hostile flag set, all five exported as true:
`F="SAML_ENABLED=true SCIM_ENABLED=true AIFACTORY_MCP_REMOTE_ENABLED=true AIFACTORY_RMUX_ENABLED=true APP_RMUX_ENABLED=true"`.
The local `$V` venv has python3-saml, so SAML on imports cleanly (probe: 308).

0. **Pre-fix count (Opus, manual, nothing committed; no file edited).**
   - Run `env $F APP_DISABLE_AUTH=true $V/python scripts/generate-openapi-spec.py`.
   - Record the "N paths" figure for the PR. Expect `308 paths` (verified in a scratch worktree).
   - Verify by `git diff --stat apps/web-server/openapi.yaml`, which shows the leak.

   Traps:
   - The run overwrites the committed `openapi.yaml`. Restore it right away with `git checkout -- apps/web-server/openapi.yaml`. Opus does this step because the coder cannot run `git checkout` or `git restore`.
   - Do not create `apps/web-server/.env` here.

1. **`scripts/generate-openapi-spec.py`: make the pin and encoding changes (D2-D5).**
   - **Lines 8-9:** after the Usage command line, add a blank line and the docstring text
     `Output is pinned to all feature flags off (independent of .env, exported flags and cwd);`
     `apps/web-server/static/ must not exist.` (two lines, under 100 columns).
   - **Line 14:** add `import os` above `import sys`.
   - **Between 25 (`import yaml`) and 26 (`from server.main import app`):** insert, after a blank line, exactly this (already `ruff format`-clean; verified):
     ```python
         # Pin every env-gated router OFF so the spec is the default deployment's API,
         # independent of .env, the shell, or the cwd (#1632). Direct assignment beats
         # server/env_bootstrap.py:27's setdefault, and pydantic ranks process env above
         # env_file (server/config.py:246-250). Gates: main.py:541/545 (SAML/SCIM),
         # mcp_remote/__init__.py:50, rmux/integration.py:45-56.
         # ponytail: fixed flag list; a new env gate is missed until added here
         # (follow-up: two-env spec-equality guard).
         os.environ.update(
             dict.fromkeys(
                 (
                     "SAML_ENABLED",
                     "SCIM_ENABLED",
                     "AIFACTORY_MCP_REMOTE_ENABLED",
                     "AIFACTORY_RMUX_ENABLED",
                     "APP_RMUX_ENABLED",
                 ),
                 "false",
             )
         )
     ```
   - **Line 29:** `with open(OUT, "w") as f:` becomes `with OUT.open("w", encoding="utf-8") as f:`.

   Verify:
   - `env $F APP_DISABLE_AUTH=true $V/python scripts/generate-openapi-spec.py` prints `295 paths`, and `git diff --exit-code apps/web-server/openapi.yaml` exits 0.
   - `env -u SAML_ENABLED $V/python -c "import importlib.util as u,os;s=u.spec_from_file_location('g','scripts/generate-openapi-spec.py');s.loader.exec_module(u.module_from_spec(s));assert 'SAML_ENABLED' not in os.environ"` exits 0, which shows import has no side effect (D3).
   - `$B/ruff format --check scripts/generate-openapi-spec.py` and `$B/ruff check scripts/generate-openapi-spec.py` exit 0.
   - After `git add scripts/generate-openapi-spec.py`, both ratchets (Tests 2) exit 0: ruff reports `1 improved` (PTH123), mypy `1 unchanged`. Verified in a scratch worktree.

   Traps:
   - Assign directly. Do not use `setdefault` or `pop` (D2).
   - Keep the pin out of module top level (D3).
   - Leave the existing `# noqa: E402` alone, because touching it risks RUF100 churn.
   - Do not one-line the tuple: it is 120+ columns and fails default `ruff format --check`.
   - Keep the comment ASCII apart from what is already there; no logging is added (CodeQL).
   - Commit: `fix(openapi): pin feature flags off in spec generator` with `#1632` in the body. The commit scope must not contain `#`. `git add` only this path, never `-A`.

2. **`apps/web-server/openapi.yaml` (whole file): regenerate it and run the manual green matrix (D7, D15).** Each run must print `295 paths`, and `sha256sum apps/web-server/openapi.yaml` must give the same hash every time:
   - (a) no `.env`, no flags: `APP_DISABLE_AUTH=true $V/python scripts/generate-openapi-spec.py`;
   - (b) `cp apps/web-server/.env.example apps/web-server/.env`, append the five flags as `=true` to it, and run with `env $F` exported, from the worktree root and from `apps/web-server` (`../../scripts/generate-openapi-spec.py`);
   - (c) from another cwd: `cd /tmp && APP_DISABLE_AUTH=true $V/python <worktree>/scripts/generate-openapi-spec.py`;
   - (d) `LC_ALL=C PYTHONUTF8=0 PYTHONCOERCECLOCALE=0 PYTHONIOENCODING=utf-8 APP_DISABLE_AUTH=true $V/python scripts/generate-openapi-spec.py`.

   Verify by `git diff --exit-code apps/web-server/openapi.yaml`, which exits 0. Commit the file only if it differs. Scratch-worktree result: all runs 295 paths, sha256 `f2572ca3…7145`, no diff.

   Traps:
   - (d) without `PYTHONIOENCODING=utf-8` exits 1 with `UnicodeEncodeError` from the final `print`, after the file is written. That is not a regression; keep the variable.
   - Delete `apps/web-server/.env` afterwards (`rm`, the coder can do it). `git status` must be clean apart from intended paths.
   - Never create `apps/web-server/static/` (D13: 294 paths).
   - If bytes differ because #1631, #1671 or another merged spec change is in the base, run the CI-style regeneration (`/tmp/ws` venv, as in `techdocs.yml:162-165`) and commit that output. Never hand-edit the yaml (D11).
   - A local venv with a different pydantic can change bytes. If (a) differs from HEAD, compare against the `/tmp/ws` venv before concluding anything.
   - Do not test lowercase flag names (D12).

3. **`.github/workflows/techdocs.yml`: add comments and hint text (D6).**
   - **After line 159** (last line of the step-2 comment block, above `- name: Regenerate OpenAPI spec` at 160), append, in the block's `#    ` style:
     ```yaml
           #    The generator pins SAML_ENABLED, SCIM_ENABLED, AIFACTORY_MCP_REMOTE_ENABLED,
           #    AIFACTORY_RMUX_ENABLED and APP_RMUX_ENABLED to false, so the spec is
           #    the default deployment (#1632).
     ```
   - **After line 193** (the generator `echo`), add `          echo "  (output ignores .env and exported feature flags; apps/web-server/static/ must be absent)"`.

   Verify:
   - `$V/python -c "import yaml; yaml.safe_load(open('.github/workflows/techdocs.yml'))"` exits 0.
   - `actionlint .github/workflows/techdocs.yml` gives no output (installed at `/run/current-system/sw/bin/actionlint`).
   - `git diff --stat .github/workflows/techdocs.yml` shows `4 insertions(+)` and nothing removed.

   Traps:
   - No `continue-on-error` (#906).
   - No backticks or `$` in the echo text.
   - The `run:` logic stays unchanged.
   - Commit as `ci(techdocs): document the pinned OpenAPI flag set` with `#1632` in the body.

4. **`CHANGELOG.md`, follow-ups and PR (Opus).**
   - **CHANGELOG.md:** under `## [Unreleased]` (line 1), in a `### Fixed` section (create it after the existing `### Security` section if missing), add: `OpenAPI generator pins opt-in feature flags off, so apps/web-server/openapi.yaml no longer depends on .env or exported flags; output is written as UTF-8 (#1632).`
   - **Follow-up issues:** draft D14 (a), (b) and (c).
   - **PR to `dev`:**
     - link the intent, spec and plan;
     - include the step 0 count and the step 2 sha256 matrix;
     - say that the coder did steps 1-3.

   Verify by `$B/python scripts/gen_autonomy_matrix.py --check`, which exits 0 (no cited file is touched; verified), and by the CI checks: ruff, the strict ratchets, CodeQL and techdocs refresh-and-validate.

   Traps:
   - CHANGELOG conflicts with #1631, #1633 and #1688-#1690 are expected. Keep both entries.
   - If the autonomy matrix check fails after a rebase, regenerate it with `python scripts/gen_autonomy_matrix.py`, then `--check`; do not hand-merge it.
   - CodeQL: the change adds no logging. The existing print shows only title, version and path count.
   - None of the D8 files may appear in the diff.

## Tests

From the worktree root. No web-server code or test changes, so the web-server pytest suite is not a gate for this change.

1. **Ruff (default, as CI):** `$B/ruff format --check apps/backend apps/web-server scripts tests` and `$B/ruff check apps/backend apps/web-server scripts tests`. Both exit 0.
2. **Ratchets, after `git add` of the PR paths:**
   - `$B/python scripts/cq_ratchet.py --staged --ruff "$B/ruff" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
   - `$B/python scripts/cq_ratchet.py --staged --tool mypy --mypy "$B/mypy" --config standards/mypy.ini --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`

   Both exit 0. The generator's ruff count drops by one (PTH123).
3. **Workflow:** run the step 3 checks.
4. **Regeneration:** the step 2 matrix (a)-(d) gives 295 paths and one sha256, and `git diff --exit-code apps/web-server/openapi.yaml` exits 0.
5. **Autonomy matrix:** `$B/python scripts/gen_autonomy_matrix.py --check` exits 0.
6. **Mutation checks.** Edit `scripts/generate-openapi-spec.py`, run `env $F APP_DISABLE_AUTH=true PYTHONIOENCODING=utf-8 $V/python scripts/generate-openapi-spec.py`, then undo by re-editing and re-run step 2 (a) to restore `openapi.yaml` (295 paths, `git diff --exit-code` exits 0). All four were run in a scratch worktree with these results:
   - M1: drop `"APP_RMUX_ENABLED",` from the tuple. Expect `297 paths` (attach/detach leak through the `get_settings()` fallback).
   - M2: move the `os.environ.update(...)` block after `from server.main import app`. Expect `308 paths`.
   - M3: revert to `open(OUT, "w")` and add `LC_ALL=C PYTHONUTF8=0 PYTHONCOERCECLOCALE=0` to the run. Expect `UnicodeEncodeError` naming `'—'` from `yaml.safe_dump`, exit 1.
   - M4: change `"false",` to `"true",`. Expect `308 paths`.

   Afterwards `git diff scripts/generate-openapi-spec.py` must show only the intended change.

## Rollback

- **Before merge:** drop the branch commits.
- **After merge:** `git revert <merge-sha>`. This restores the old generator and workflow text, removes the CHANGELOG entry, and restores `openapi.yaml` if it was committed.
- **If techdocs goes red after a revert:** a local `.env` or exported flags can leak gated routes into the spec again. Regenerate in the CI `/tmp/ws` venv with no `.env` and no flags exported, then commit.
- No runtime code, config, Helm or data changes, so nothing deployed needs undoing.
