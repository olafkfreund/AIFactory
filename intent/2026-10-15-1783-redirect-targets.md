---
status: approved
issue: 1783
author: olafkfreund
---

# Intent: commands inside redirect targets must be checked

## Problem

Open on `dev` at `357e6d95`; no commit mentions #1783.
`_collect_command_nodes` (`apps/backend/security/ast_parser.py:131`) walks
only the child attributes `parts`, `list`, `command`, `body` and `pipe`.
A bashlex `redirect` node keeps its target in `output`, so a command or
process substitution used as a redirect target is never extracted. A probe on
`dev` gives:

- `ls > $(nc x)`, `ls 2>> \`nc x\``, `cat < <(nc x)`, `cat <<< $(nc x)`,
  `ls > >(nc x)` → only the outer command; `nc` runs unchecked.
- `f() { nc x; }` → `['nc', 'nc']`: `body` and `parts` point at the same
  node, so the walk visits it twice.

## Proposed outcome

- Every command bash would run from a redirect target is extracted and checked.
- The walk is not a list of known child attributes (the shape #1782 warned
  about): it visits every child node, so a node kind or attribute nobody
  listed cannot hide a command.
- Rows for `>`, `2>`, `>>`, `<`, `<<<` and `> >(…)` targets.

## Affected users and systems

- `apps/backend/security/ast_parser.py` (`_collect_command_nodes`); callers
  `security/hooks.py`, `security/plan_commands.py` (no change).
- `tests/test_command_sandbox.py`.

## Constraints

- Fail closed; keep `MUST_PARSE` exact-name tests green.
- Out of scope: #1797 (`${…}` and heredoc bodies), #1784, #1754 (PR #1800).

## Open questions

None.
