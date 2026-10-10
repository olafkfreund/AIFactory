---
status: draft
issue: 1632
spec: spec/2026-10-10-1632-openapi-feature-flags.md
---

# Plan: the OpenAPI spec must not depend on the generator's environment

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1632`, branch
`fix/1632-openapi-feature-flags`, base HEAD `efd4b433` (spec approved; contains
`origin/dev` `c59a0ea6`). `apps/web-server/static/` is absent. There is no
existing test for `scripts/generate-openapi-spec.py`.

Six steps. Steps 1-4 edit four files, so under the model split they go to the
`coder` agent: start it with this path and step 1, then send steps 2, 3 and 4
to the same agent with `SendMessage`. Opus does step 0 and step 5, and the
review runs on a fresh `opus` agent given only this file and `git diff`.

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
    follow-up issue tracks the guard. The pin regression test in step 1 is
    not that guard: it runs one environment with every flag on and checks
    that no gated path leaks.
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
- Routers mount when `server.main` is imported, so any test must use a fresh
  interpreter.

## Steps

0. **Pre-fix count (Opus, manual, nothing committed; no file edited).**
   - Run `APP_RMUX_ENABLED=true AIFACTORY_MCP_REMOTE_ENABLED=true APP_DISABLE_AUTH=true $V/python scripts/generate-openapi-spec.py`.
   - Record the "N paths" figure for the PR. Expect more than 295.
   - Verify by `git diff --stat apps/web-server/openapi.yaml`, which shows the leak.

   Traps:
   - The run overwrites the committed `openapi.yaml`. Restore it right away with `git checkout -- apps/web-server/openapi.yaml`. Opus does this step because the coder cannot run `git checkout` or `git restore`.
   - Leave SAML and SCIM unset, because the SAML import may raise if xmlsec is missing.
   - Do not create `apps/web-server/.env`.

1. **`apps/web-server/tests/test_generate_openapi_spec.py` (new file): write the pin regression test first (red).**

   ```python
   """#1632: generate-openapi-spec.py pins feature flags off and writes UTF-8."""

   from __future__ import annotations

   import subprocess
   import sys
   from pathlib import Path

   import pytest

   from server.utils import subprocess_env

   _SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "generate-openapi-spec.py"
   _FLAGS = (
       "SAML_ENABLED",
       "SCIM_ENABLED",
       "AIFACTORY_MCP_REMOTE_ENABLED",
       "AIFACTORY_RMUX_ENABLED",
       "APP_RMUX_ENABLED",
   )
   # Fresh interpreter: routers mount at server.main import, so a cached module would hide the flags.
   _RUNNER = (
       "import importlib.util, pathlib, sys\n"
       "spec = importlib.util.spec_from_file_location('gen', sys.argv[1])\n"
       "assert spec and spec.loader\n"
       "m = importlib.util.module_from_spec(spec)\n"
       "spec.loader.exec_module(m)\n"
       "m.REPO = pathlib.Path(sys.argv[2])\n"
       "m.OUT = m.REPO / 'openapi.yaml'\n"
       "sys.exit(m.main())\n"
   )


   @pytest.fixture(scope="module")
   def run(tmp_path_factory: pytest.TempPathFactory) -> tuple[subprocess.CompletedProcess[str], Path]:
       out_dir = tmp_path_factory.mktemp("openapi")
       (out_dir / ".env").write_text("".join(f"{name}=true\n" for name in _FLAGS))  # cwd .env (pydantic env_file)
       env = subprocess_env.child_env(
           extra={
               **dict.fromkeys(_FLAGS, "true"),  # exported flags
               "APP_DISABLE_AUTH": "true",
               "LC_ALL": "C",  # ascii locale encoding ...
               "PYTHONUTF8": "0",
               "PYTHONCOERCECLOCALE": "0",  # ... not coerced to C.UTF-8
               "PYTHONIOENCODING": "utf-8",  # the em-dash print() must not mask the file-encoding check
               "PYTHONDONTWRITEBYTECODE": "1",
           }
       )
       proc = subprocess.run(  # noqa: S603 — fixed argv, our own interpreter
           [sys.executable, "-c", _RUNNER, str(_SCRIPT), str(out_dir)],
           cwd=out_dir,
           env=env,
           capture_output=True,
           text=True,
           timeout=180,
           check=False,
       )
       return proc, out_dir / "openapi.yaml"


   def test_generator_writes_utf8_under_ascii_locale(run: tuple[subprocess.CompletedProcess[str], Path]) -> None:
       proc, out = run
       assert proc.returncode == 0, proc.stderr[-2000:]
       assert "—" in out.read_text(encoding="utf-8")  # main.py:441 em dash: the check is not vacuous


   def test_generator_pins_feature_flags_off(run: tuple[subprocess.CompletedProcess[str], Path]) -> None:
       proc, out = run
       assert proc.returncode == 0, proc.stderr[-2000:]
       paths = [
           ln[2:-1]
           for ln in out.read_text(encoding="utf-8").splitlines()
           if ln.startswith("  /") and ln.endswith(":")
       ]
       assert "/api/tasks/{task_id}/agent-console/sse" in paths  # parser anchor
       leaked = [
           p
           for p in paths
           if p.startswith(("/api/auth/saml", "/scim/v2", "/api/mcp-remote"))
           or p.endswith(("/agent-console/attach", "/agent-console/detach"))
       ]
       assert leaked == []
   ```

   Verify by `cd apps/web-server && $V/python -m pytest tests/test_generate_openapi_spec.py -o asyncio_mode=auto -q -p no:cacheprovider`. Expect `2 failed`:
   - one `UnicodeEncodeError` naming `'—'`;
   - one listing the leaked saml, scim, mcp-remote and rmux paths.

   Traps:
   - The ratchet scope covers `apps/web-server/*.py`, including tests, so the file must be clean under strict ruff and `mypy --strict`.
   - Do not `import yaml`: no `types-PyYAML` stubs are installed, which is why the test parses path keys from text.
   - `subprocess.run` needs `# noqa: S603`, following `tests/test_server_non_dumpable.py:31`.
   - Build the env with `subprocess_env.child_env` (`server/utils/subprocess_env.py:43`), imported as a module.
   - Do not commit until step 2 is green. Commit steps 1 and 2 together.
   - No count assertion (`== 295`): it breaks whenever a route is added.

2. **`scripts/generate-openapi-spec.py`: make the pin and encoding changes (D2-D5).**
   - **Lines 8-9:** after the Usage command line, add a docstring line: `Output is pinned to all feature flags off (independent of .env, exported flags and cwd); apps/web-server/static/ must not exist.`
   - **Line 14:** add `import os` above `import sys`.
   - **Between 25 (`import yaml`) and 26 (`from server.main import app`):** insert an ASCII comment block.
     - It cites #1632 and the reasons the pin wins: `env_bootstrap.py:27` uses setdefault, and pydantic `config.py:246-250` ranks process env above env_file.
     - It lists the gates at `main.py:541/545`, `mcp_remote/__init__.py:50` and `rmux/integration.py:45-56`.
     - Add the line `# ponytail: fixed flag list; a new env gate is missed until added here (follow-up: two-env spec-equality guard).`
     - Then add:
       ```python
       os.environ.update(
           dict.fromkeys(
               ("SAML_ENABLED", "SCIM_ENABLED", "AIFACTORY_MCP_REMOTE_ENABLED", "AIFACTORY_RMUX_ENABLED", "APP_RMUX_ENABLED"),
               "false",
           )
       )
       ```
       Let `ruff format` decide the wrapping.
   - **Line 29:** `with open(OUT, "w") as f:` becomes `with OUT.open("w", encoding="utf-8") as f:`.

   Verify:
   - Re-run the step 1 pytest command. Expect `2 passed` in about 5 seconds.
   - Run `env -u SAML_ENABLED $V/python -c "import importlib.util as u,os;s=u.spec_from_file_location('g','scripts/generate-openapi-spec.py');s.loader.exec_module(u.module_from_spec(s));assert 'SAML_ENABLED' not in os.environ"`. It exits 0, which shows import has no side effect (D3).

   Traps:
   - Assign directly. Do not use `setdefault` or `pop` (D2).
   - Keep the pin out of module top level (D3).
   - Leave the existing `# noqa: E402` alone, because touching it risks RUF100 churn.
   - The ratchet compares staged counts. PTH123 drops by one, and no new finding (such as E501) may appear.
   - Commit (with step 1): `fix(openapi): pin feature flags off in spec generator` with `#1632` in the body. The commit scope must not contain `#`.
   - `git add` only the two paths, never `-A`.

3. **`apps/web-server/openapi.yaml` (whole file): regenerate it and run the manual green matrix (D7, D15).** Each run must print `295 paths`, and `sha256sum apps/web-server/openapi.yaml` must give the same hash every time:
   - (a) no `.env`, no flags: `APP_DISABLE_AUTH=true $V/python scripts/generate-openapi-spec.py`;
   - (b) `cp apps/web-server/.env.example apps/web-server/.env`, set the five flags to true in it, and run with all five exported as true;
   - (c) from another cwd: `cd /tmp && APP_DISABLE_AUTH=true $V/python <worktree>/scripts/generate-openapi-spec.py`;
   - (d) under `LC_ALL=C PYTHONUTF8=0 PYTHONCOERCECLOCALE=0`.

   Verify by `git diff --exit-code apps/web-server/openapi.yaml`, which exits 0. Commit the file only if it differs.

   Traps:
   - Delete `apps/web-server/.env` afterwards. `git status` must be clean apart from intended paths.
   - Never create `apps/web-server/static/` (D13: 294 paths).
   - If bytes differ because #1631, #1671 or another merged spec change is in the base, run the CI-style regeneration (`/tmp/ws` venv, as in `techdocs.yml:162-165`) and commit that output. Never hand-edit the yaml (D11).
   - A local venv with a different pydantic can change bytes. If (a) differs from HEAD, compare against the `/tmp/ws` venv before concluding anything.
   - Do not test lowercase flag names (D12).

4. **`.github/workflows/techdocs.yml`: add comments and hint text (D6).**
   - **Lines 152-159**, the step-2 comment block above `- name: Regenerate OpenAPI spec` at 160: append `# The generator pins SAML_ENABLED, SCIM_ENABLED, AIFACTORY_MCP_REMOTE_ENABLED, AIFACTORY_RMUX_ENABLED and APP_RMUX_ENABLED to false, so the spec is the default deployment (#1632).` Wrap it over lines to match the block.
   - **After line 193** (the generator `echo`), add `echo "  (output ignores .env and exported feature flags; apps/web-server/static/ must be absent)"`.

   Verify:
   - `$V/python -c "import yaml; yaml.safe_load(open('.github/workflows/techdocs.yml'))"` exits 0.
   - `actionlint .github/workflows/techdocs.yml` gives no output, if actionlint is installed.
   - `git diff .github/workflows/techdocs.yml` shows only `#` and `echo` lines.

   Traps:
   - No `continue-on-error` (#906).
   - No backticks or `$` in the echo text.
   - The `run:` logic stays unchanged.
   - Commit as `ci(techdocs): document the pinned OpenAPI flag set` with `#1632` in the body.

5. **`CHANGELOG.md`, follow-ups and PR (Opus).**
   - **CHANGELOG.md:** under `## [Unreleased]` (line 1), in a `### Fixed` section (create it if missing), add: `OpenAPI generator pins opt-in feature flags off, so apps/web-server/openapi.yaml no longer depends on .env or exported flags; output is written as UTF-8 (#1632).`
   - **Follow-up issues:** draft D14 (a), (b) and (c).
   - **PR to `dev`:**
     - link the intent, spec and plan;
     - include the step 0 count and the step 3 sha256 matrix;
     - say that the coder did steps 1-4.

   Verify by `$B/python scripts/gen_autonomy_matrix.py --check`, which exits 0, and by the CI checks: ruff, the strict ratchets, CodeQL and techdocs refresh-and-validate.

   Traps:
   - CHANGELOG conflicts with #1631, #1633 and #1688-#1690 are expected. Keep both entries.
   - If the autonomy matrix check fails after a rebase, regenerate the matrix and do not hand-merge it.
   - CodeQL: the change adds no logging. The existing print shows only title, version and path count.
   - None of the D8 files may appear in the diff.

## Tests

From the worktree root:

1. **New test:** `cd apps/web-server && $V/python -m pytest tests/test_generate_openapi_spec.py -o asyncio_mode=auto -q -p no:cacheprovider`. Expect `2 failed` before step 2 and `2 passed` after it.
2. **Web-server suite:** `cd apps/web-server && $V/python -m pytest tests -o asyncio_mode=auto -q -p no:cacheprovider`. Expect 0 failed and base count + 2. Leave `tests/test_security.py` GitCommitValidator out of the gate, because it fails whenever files are staged (environmental). Unstage before running the suite.
3. **Ruff:** `$B/ruff format --check apps/backend apps/web-server scripts tests` and `$B/ruff check apps/backend apps/web-server scripts tests`. Both exit 0.
4. **Ratchets, after `git add` of the PR paths:**
   - `$B/python scripts/cq_ratchet.py --staged --ruff $B/ruff --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
   - `$B/python scripts/cq_ratchet.py --staged --tool mypy --mypy $B/mypy --config standards/mypy.ini --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`

   Both exit 0. The generator's ruff count drops by one (PTH123), and the new test has 0 findings.
5. **Workflow:** run the step 4 checks.
6. **Regeneration:** the step 3 matrix (a)-(d) gives 295 paths and one sha256, and `git diff --exit-code apps/web-server/openapi.yaml` exits 0.
7. **Autonomy matrix:** `$B/python scripts/gen_autonomy_matrix.py --check` exits 0.
8. **Mutation checks**, against the step 1 pytest command. Edit `scripts/generate-openapi-spec.py`, run, then undo by editing. Afterwards `git diff` must show only the intended change.
   - M1: drop `"APP_RMUX_ENABLED"` from the tuple. Expect `1 failed, 1 passed`, with the attach and detach paths leaked through the `get_settings()` fallback.
   - M2: move the `os.environ.update` after `from server.main import app`. Expect `1 failed, 1 passed`, with all four groups leaked.
   - M3: revert to `open(OUT, "w")`. Expect `2 failed`, with `UnicodeEncodeError` naming `'—'`.
   - M4: change `"false"` to `"true"`. Expect `1 failed, 1 passed`, which shows the leak check is not vacuous.

## Rollback

- **Before merge:** drop the branch commits.
- **After merge:** `git revert <merge-sha>`. This restores the old generator and workflow text, removes the test and CHANGELOG entry, and restores `openapi.yaml` if it was committed.
- **If techdocs goes red after a revert:** a local `.env` or exported flags can leak gated routes into the spec again. Regenerate in the CI `/tmp/ws` venv with no `.env` and no flags exported, then commit.
- No runtime code, config, Helm or data changes, so nothing deployed needs undoing.
