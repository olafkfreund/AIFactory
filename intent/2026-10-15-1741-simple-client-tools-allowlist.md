---
status: approved
issue: 1741
author: olafkfreund
---

# Intent: simple clients get only the tools their helper needs

## Problem

`create_simple_client` (`apps/backend/core/simple_client.py:95-119`) passes the
helper's tool list from `AGENT_CONFIGS` only as `allowed_tools`, and runs under
`permission_mode="bypassPermissions"`. Under bypass, `allowed_tools` restricts
nothing: the CLI still offers its whole default tool set. #1719 (4ec9a332)
already disallows Write, Edit, MultiEdit, NotebookEdit and Bash. What is left:
the text-only helpers (`commit_message`, `merge_resolver`, `spec_compaction`)
take untrusted input (diffs, conflict text, spec text from the agent's branch)
and still get the CLI's default Read, Glob, Grep, WebFetch and WebSearch. A
prompt injection in that input can read files or send data out.

## Proposed outcome

Each simple client gets exactly the base tool set its helper lists in
`AGENT_CONFIGS`: no tools for the text-only helpers, read tools for
`insights`, `batch_analysis` and `batch_validation`.

## Affected users and systems

Backend and web-server callers of `create_simple_client`: commit messages,
merge conflict resolution (backend and web-server), spec compaction, insights
extraction, batch issue analysis and validation.

## Constraints

- `insights` and `batch_*` need Read/Grep; keep the lists per helper. Never one
  empty set for everyone.
- The installed claude-agent-sdk must support the `tools` option (it does:
  0.2.82 backend, 0.2.87 web-server; `tools=[]` becomes `--tools ""`).
- Keep the #1719 `disallowed_tools` as defence in depth.

## Open questions

None for approval. `insights` and `batch_analysis` also list WebSearch and
WebFetch in `AGENT_CONFIGS`; this change keeps them (conservative, no
behaviour loss) and flags it for a later decision.
