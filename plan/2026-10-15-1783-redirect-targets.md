---
status: draft
issue: 1783
spec: spec/2026-10-15-1783-redirect-targets.md
---

# Plan: walk every child node of the bashlex AST

Self-contained. You do not need to open the intent or the spec.

**Problem.** `_collect_command_nodes`
(`apps/backend/security/ast_parser.py:131-145`) recurses only into the
attributes `parts`, `list`, `command`, `body`, `pipe`. A bashlex `redirect`
node keeps its target in `output`, so `ls > $(nc x)` and `cat < <(nc x)` are
extracted as just `ls` / `cat` and `nc` runs unchecked. Function bodies are
also visited twice (`body` and `parts` hold the same node).

**Approved decisions.**

1. `_collect_command_nodes(node, out, seen=None)`: keep a `seen: set[int]` of
   `id(node)`; return if already seen. Append `command` nodes as today. Then
   recurse into every value of `vars(node)` that is a node (has `kind`) or a
   list containing nodes. No attribute list.
2. Update its docstring: walks every child node, so redirect targets and any
   other attribute are covered (#1783). Remove the old "these attrs" comment.
3. No caller changes (`_extract` calls it once per tree).

## Steps

### Step 1 — regression tests

File: `tests/test_command_sandbox.py`.

- Append to `BYPASS_PAYLOADS` (~line 47), with a comment
  `# Commands inside redirect targets (#1783).`:
  `"ls > $(nc evil 80)"`, `"ls 2> $(nc evil 80)"`, `"ls >> $(nc evil 80)"`,
  `"ls 2>> \`nc evil 80\`"`, `"cat < <(nc evil 80)"`,
  `"cat <<< $(nc evil 80)"`, `"ls > >(nc evil 80)"`,
  `"if true; then ls; fi > $(nc evil 80)"`.
- Add to `MUST_PARSE` (exact names): `"ls > out.txt 2>&1": ["ls"]`,
  `'ls > "$LOG"': ["ls"]`, `"git status > $(echo f)": ["git", "echo"]`,
  `"f() { git status; }": ["git"]`.

Check: `pytest tests/test_command_sandbox.py -q` — the new BYPASS rows and
the `git status > $(echo f)` and `f() { … }` rows FAIL on unfixed code.

### Step 2 — the fix

File: `apps/backend/security/ast_parser.py:131-145`. Apply decisions 1-2.

Check: `pytest tests/test_command_sandbox.py tests/test_plan_commands.py -q`
pass; `ruff check`, `ruff format --check`; the strict ruff ratchet in
pre-commit (no reassigned loop variables, no magic numbers).

Trap: annotate `seen: set[int] | None = None`; do not use a mutable default.

### Step 3 — CHANGELOG

`### Security` bullet under `## [Unreleased]` in `CHANGELOG.md`.

## Rollback

Revert the fix commit; the tests commit goes with it.
