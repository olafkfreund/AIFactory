---
status: approved
issue: 1443
spec: spec/2026-09-18-1443-js-test-script-pytest.md
---

# Plan: Refuse a test script in the wrong language

## Decisions (carried from the approved spec)

- A deterministic **gate** catches a `package.json` `test` script that runs a Python test
  runner in a project with no Python test harness. It fails with a concrete message, and
  the existing trailing-gate → QA-fixer loop repairs it. There is no automatic rewrite.
- The Nix provisioner stops treating an **unset** language as Python **only when the
  project directory proves otherwise** (a `package.json` and no Python markers). Its
  documented default (`nix_provisioner.py:715`: "correct there… for manifests that omit
  it") is kept whenever nothing is known.
- No prompt-only fix.

## Steps

1. `apps/backend/agents/gate_runner.py`: add
   `_PY_TEST_RUNNER = re.compile(r"(^|[\s;&|])(pytest|py\.test|python3?\s+-m\s+(pytest|unittest))\b")`
   and `_has_python_test_harness(p) -> bool` (`pytest.ini`, `setup.cfg` containing
   `[tool:pytest]`, `pyproject.toml` containing `[tool.pytest`, or any `test_*.py` /
   `*_test.py` under `p` at most 3 levels deep, skipping `node_modules` and `.git`).
   → verify: unit tests for each marker, and for `node_modules/x/test_a.py` being ignored.
2. Same file, `detect_gates` (:129-136): when `"test" in scripts` and
   `_PY_TEST_RUNNER.search(scripts["test"])` and `not _has_python_test_harness(p)`, append
   `Gate("test-script-language", ["sh", "-c", 'echo "package.json \\"test\\" runs <cmd> but this is not a Python project; use the project\'s JS test runner" >&2; exit 1'])`
   **instead of** the `npm test` gate. That gate would only run pytest over JS and fail
   with a less useful message.
   → verify: new tests: `{"test":"pytest -q"}` with no harness → gate names contain
   `test-script-language` and not `test`; the same script with `pytest.ini` → a normal
   `test` gate; `{"test":"jest"}` → unchanged.
3. `apps/backend/core/nix_provisioner.py:679-692` `_python_libs(m, project_dir=None)`: when
   `m.language` is unset, `project_dir` is given, `project_dir/package.json` exists, no
   Python markers exist (`pyproject.toml`, `requirements.txt`, `setup.py`, `pytest.ini`), and
   `"pytest"` is not in the verify commands → `py_harness = False`. Otherwise unchanged.
   Add a self-test `_test_js_unset_language_with_project_dir()` next to the existing
   `_test_*` fixtures, and call it from `_test()` (the `__main__` entrypoint, `:1030`).
   → verify: `python -m core.nix_provisioner` (the self-test entrypoint) passes.
4. `apps/backend/core/nix_env.py:83`: pass `project_dir=project_dir` to `generate_flake`
   (it already accepts it at `nix_provisioner.py:501`, but the call site omits it).
   → verify: `tests/ -k nix_env` passes, and a flake generated for a JS project dir with an
   unset language contains no `pytest`.

## Tests

```bash
apps/backend/.venv/bin/pytest tests/test_gate_runner.py tests/ -k "nix_provisioner or nix_env" -v
(cd apps/backend && .venv/bin/python -m core.nix_provisioner)
```
Expected: all pass. Python projects' gates and flakes are unchanged.

## Rollback

Revert the implementation commit. There are no data or config changes.

## Deviations (recorded during implementation)

- **Steps 3-4 moved into `core/nix_env.py`, with the same decision.** An unset language
  sends `generate_flake` down the *python* branch before `_python_libs` runs. Turning off
  `py_harness` there would still yield a Python shell with no node. Threading `project_dir`
  into `generate_flake` (step 4) would also add pyproject deps and pip to *Python* flakes,
  which breaks "Python flakes unchanged". Instead, `materialize_flake_into` fills an unset
  language with `javascript` when the project has a `package.json`, no Python marker
  (`pyproject.toml`, `requirements.txt`, `setup.py`, `pytest.ini`) and no `pytest` in its
  verify commands. `nix_provisioner.py` is untouched, so its self-test is unchanged. The
  new cases live in `tests/test_test_script_language.py`.
- **Step 2: the gate message is passed to `sh` as `$0`**, not interpolated into the
  script, so a `package.json` script value cannot inject shell.
