---
status: draft
issue: 1443
author: Olaf Krasicki-Freund
---

# Intent: The coder writes a Python test command into a JavaScript package.json

## Problem

Spec 161 (a JavaScript tic-tac-toe) produced a `package.json` with
`"test": "pytest -q"`: a Python test command in a JS project that has no Python
tests. Any lane that honours the package `test` script (`npm test`, TFactory) runs pytest
over JavaScript and errors. TFactory only reads `package.json`, so the language-blind
default comes from AIFactory's coder. The likely source is a Python `verify_commands`
default (`pytest -q`, as used in `core/nix_provisioner.py`) that leaks into the coder's
context for non-Python projects. This is not confirmed yet.

## Proposed outcome

A generated JS/TS project's `test` script runs that project's JS test runner. More
generally, no verify command for one language is proposed to a project in another.

## Affected users and systems

- `apps/backend`: the coder prompt/context and whichever default supplies `pytest -q`
- TFactory and any lane that runs `npm test`
- Users building non-Python projects

## Constraints

- Python projects must keep `pytest -q` as their default.
- Must not depend on the model "remembering"; the wrong default must not be offered.

## Open questions

1. The exact source is not yet traced. The spec stage starts by confirming it
   (spec 161's context and `requirements.json` / environment contract).
