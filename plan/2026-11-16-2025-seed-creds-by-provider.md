---
status: draft
issue: 2025
spec: spec/2026-11-16-2025-seed-creds-by-provider.md
coders:
  1: lead (small, tightly coupled logic; a handoff would cost more than it saves)
  2: lead
reviewers:
  - codex
  - agy
---

# Plan: seed only the CLI logins a build Job uses

Carries the approved spec: one shared `cred_files_for_models` rule, used by build and spec Jobs.

## Steps

1. apps/web-server/server/services/build_backend.py: add `_PROVIDER_CRED_KEY`, `cred_files_for_models`, `_run_phase_models`; pass the result to `_inject_seed_creds` in `build_run_py_job_manifest` → run tests/test_build_backend_kubejob.py
   Traps: lazy `phase_config` import (backend path is on sys.path at runtime only).
2. apps/web-server/server/services/spec_job_backend.py: replace the Copilot-only filter with `cred_files_for_models([model, "sonnet"])`; update tests in tests/test_build_backend_kubejob.py and tests/test_spec_job_backend.py → pytest both files
   Traps: use plain names in imports (ruff alias sorting).

## Tests

`.venv/bin/python -m pytest tests/test_build_backend_kubejob.py tests/test_spec_job_backend.py` all pass; `ruff check` clean on touched files.

## Rollback

Revert the commit; build Jobs seed every login again.
