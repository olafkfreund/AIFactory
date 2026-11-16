---
status: draft
spec: spec/2026-11-16-2064-completion-standalone-load.md
reviewers: [codex]
---

# Plan

1. coders: you. Edit apps/backend/agents/parallel_integration.py loader as in the spec (add `import sys`).
2. coders: you. Add test_completion_emitter_loads_standalone to tests/test_parallel_integration_task_id.py.
3. Run: PYTHONPATH=apps/backend:apps/web-server:. pytest tests/test_parallel_integration_task_id.py

Rollback: revert the commit.
