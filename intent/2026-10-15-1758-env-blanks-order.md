---
status: draft
issue: 1758
author: olafkfreund
---

# Intent: pin the order of agent env blanks and the MCP env merge

## Problem

`create_client` (`apps/backend/core/client.py:615`) blanks host secrets in
`sdk_env` with `get_agent_env_blanks()`. Later (`client.py:915`)
`_externalize_secret_env` (`client.py:380-394`) moves each catalog MCP
server's `env` into the same `sdk_env`, over the blanks. Nothing tests this
order. If someone moves the blanking below the MCP merge, the blanks
silently wipe the operator's MCP credentials (for example `GITHUB_TOKEN` for
the GitHub MCP server). If someone drops the blanking, host secrets reach the
agent. Follow-up from #1713 (spec `spec/2026-10-11-1713-agent-env-scrub-gaps.md`,
section 6).

## Proposed outcome

A test fails if either happens:

- an operator MCP credential whose name is on the deny list no longer reaches
  `options.env`, or
- a denied host secret that no MCP server sets is no longer blanked.

## Affected users and systems

Backend agent client (`apps/backend/core/client.py`), tests only.

## Constraints

- Test only. No production code change.
- The current order (blanks first, MCP env merged over them) is intended:
  operator MCP credentials must survive.

## Open questions

None. The issue text says "a blank cannot be overridden by ordering"; the
lead confirmed the intended behaviour is the opposite for MCP-set keys (the
MCP env wins), so the test pins the current order.
