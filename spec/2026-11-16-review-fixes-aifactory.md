---
status: approved
issue: 2067
intent: intent/2026-11-16-review-fixes-aifactory.md
---

# Spec: CLI login terminal ownership

## Design
1. Route: `session = manager.create_session(cwd=home, owner_id=<user id>)`; `terminal_id = session.id`; the delayed thread calls `session.write(cmd)`. Drop the invalid `env`/`shell` kwargs.
2. `may_use`: owner match is required as before; project check is skipped only when `session.project_id is None` (owner-bound, project-less session). Project-bound sessions are unchanged.

## Alternatives rejected
- Give the session a project: the home dir belongs to no project.
- Restrict the route to the service principal: breaks the UI for real users.

## Risks
Project-less sessions only come from this route (the /terminals route forbids non-service callers without a project), so the relaxation is narrow.

## Verification
Two regression tests in tests/test_terminal_ownership.py fail before the fix: `may_use` for a project-less owned session, and the route passing owner_id, returning the string id and writing the command. tests/test_ws_org_routing.py mock updated.
