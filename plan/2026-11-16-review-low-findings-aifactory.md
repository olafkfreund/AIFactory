---
status: approved
issue: 2055
spec: spec/2026-11-16-review-low-findings-aifactory.md
reviewers:
  - codex   # independent read-only diff review
# coders: none, both steps are small and done by the lead
---

# Plan: Low review findings, AIFactory

Decisions: as in the spec (narrow path refusal; read body projectId in the dependency).

## Steps
1. apps/backend/security/hooks.py `_path_command_refusal`: refuse only absolute non-system, `..`, or BASE_COMMANDS/VALIDATORS basename → `pytest tests/test_base_ref_scripts.py`. Tests: add allowed and refused cases. Traps: ruff plain names.
2. apps/web-server/server/routes/project_github.py: read body projectId on writes → `pytest apps/web-server/tests/test_org_installation_wiring.py -o asyncio_mode=auto`. Test the body cases.

## Tests
ruff check/format on touched files; the two test files above.

## Rollback
Revert the commit.
