---
status: approved
intent: intent/2026-11-16-2064-completion-standalone-load.md
---

# Spec: package-style file load

Pass `submodule_search_locations=[services dir]` to `spec_from_file_location` and register the module in `sys.modules` during exec (removed on failure), so `.billing` resolves as a sibling.

Rejected: changing completion.py to an absolute import (the server package imports it relatively; two import modes), or a try/except fallback in completion.py.

Risk: the lazy `..observability` imports inside completion.py stay unresolvable in this mode; they are already wrapped best-effort.
Verification: new test in tests/test_parallel_integration_task_id.py fails before, passes after.
