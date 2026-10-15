---
status: draft
issue: 1797
author: olafkfreund
---

# Intent: command substitution in `${…}` and heredoc bodies must not go unchecked

## Problem

Open on `dev` at `d0c64187`; no commit mentions #1797. bashlex leaves two
regions unparsed, keeping only their text:

- a `parameter` node's `value` (the inside of `${…}`): `echo ${X:-$(nc x)}`
  and `X=${Y:-$(nc x)}` are extracted as `['echo']` and `[]`;
- a `heredoc` node's `value` (the body): `cat <<EOF` / `$(nc x)` / `EOF` is
  extracted as `['cat']`.

bash runs the substitution in both (checked in bash), so `nc` runs unchecked.
Arithmetic (`$((…))`) is not affected: bashlex refuses to parse it, so it
already fails closed. Quoted heredoc delimiters (`<<'EOF'`) also fail to parse
in bashlex today.

## Proposed outcome

- A `${…}` or heredoc body that could run a command is refused (fail closed).
- `${X:-default}`, `"${HOME}/x"` and heredocs with plain text or `$VAR` stay
  allowed.
- Rows for each in the sandbox tests.

## Affected users and systems

- `apps/backend/security/ast_parser.py` (`_collect_command_nodes`); callers
  unchanged.
- `tests/test_command_sandbox.py`.

## Constraints

- Fail closed, allow-list shaped (#1782's lesson).
- The heredoc node is reachable only through the redirect's `heredoc`
  attribute, which the generic walk from PR #1802 (#1783) visits. This work
  builds on that branch.

## Open questions

1. Re-parse the region and check its commands, or refuse? Taken here: refuse.
   The region is text bashlex does not parse, so we cannot see its commands
   reliably.
