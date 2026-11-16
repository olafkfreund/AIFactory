---
status: draft
issue: 2055
intent: intent/2026-11-16-review-low-findings-aifactory.md
---

# Spec: Low review findings, AIFactory

## Design
1. `_path_command_refusal` skips a word with `/` when it is a system binary. Otherwise it refuses only if the path is absolute, contains `..`, or its basename is in `BASE_COMMANDS` or `VALIDATORS` (the names an agent file could impersonate). Other relative paths continue to the unchanged allowlist check, which still needs the basename allowed (so `./gradlew` stays refused unless the profile allows it).
2. `project_github_token` also reads a string `projectId` from the JSON body on non-read methods (body is cached by Starlette). It then follows the existing access check and org pinning. No project or unparseable body: no token, as before.

## Alternatives rejected
- Trust base-tracked scripts by path: path-vs-cwd problem (`cd sub && ./x`), spec #2028.
- Bind org from a body `projectPath`: unauthenticated path choice.

## Risks
Stack commands (node, python) can now be run by relative path; the agent could already run arbitrary code through them.

## Verification
tests/test_base_ref_scripts.py (allowed and refused cases); web-server tests/test_org_installation_wiring.py (body projectId allowed, denied 403, projectPath and bad JSON no token); ruff.
