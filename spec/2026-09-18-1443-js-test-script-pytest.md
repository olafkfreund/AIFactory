---
status: draft
issue: 1443
intent: intent/2026-09-18-1443-js-test-script-pytest.md
---

# Spec: Refuse a test script in the wrong language

## Findings

- No AIFactory code writes `package.json`'s `test` script. The coder model writes it.
  No language-blind default in the backend yields `pytest -q` for a JS project: the
  `pytest -q` literals in `core/nix_provisioner.py:880-980` are its own self-test fixtures.
- One real leak exists: `nix_provisioner.py:692`,
  `py_harness = language in ("", "python")`, treats an *unset* language as Python and
  provisions pytest. That makes pytest look available in a JS build and may nudge the model.
- Spec 161's artifacts are no longer on the control-plane pod, so the exact cause for
  that run cannot be confirmed.

## Design

Because the source is a model choice, fix it where a deterministic check can catch it,
and remove the one real nudge.

1. **Gate check** in `agents/gate_runner.py::detect_gates` (`:129-136`). When
   `package.json` has a `test` script whose command runs a Python test runner (`pytest`,
   `py.test`, `python -m pytest`, `python -m unittest`) **and** the project has no Python
   test harness (none of `pytest.ini`, `[tool.pytest` in `pyproject.toml`, or `*.py` test
   files), add a failing gate `test-script-language` with the message
   `package.json "test" runs <cmd> but this is not a Python project`.
   The trailing gates already feed QA, so the QA fixer receives it as a concrete defect to
   repair.
2. **Stop treating an unset language as Python** in `nix_provisioner.py:692`. Infer from
   the project's manifests (`package.json` → javascript) before defaulting, and keep
   `""` → python only when no manifest says otherwise.

## Alternatives rejected

- **Prompt instruction in `prompts/coder.md`.** It relies on the model remembering, which
  the intent rules out as the sole fix. It could be added as a cheap extra, but it is not
  the control.
- **Rewrite the script automatically.** This silently edits user-visible project files,
  and the correct JS runner (jest/vitest/node --test) is not knowable.

## Risks

- A mixed repo (a JS app with Python tests on purpose) would carry a Python test harness,
  so the check does not fire. The harness detection is what keeps this safe.
- Changing the language inference in the provisioner could change the closure for
  projects that relied on the `""` default. They are covered by its existing self-tests,
  plus one new JS case.

## Verification

- Unit: `package.json` `{"test":"pytest -q"}` with no Python harness → a failing
  `test-script-language` gate. The same script in a repo with `pytest.ini` → no such gate.
  `"test":"jest"` → unchanged.
- Unit: the provisioner with a `package.json` and unset language → no pytest in the closure.
- `apps/backend/.venv/bin/pytest tests/ -k "gate_runner or nix_provisioner" -v` passes.
