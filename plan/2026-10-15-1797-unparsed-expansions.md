---
status: draft
issue: 1797
spec: spec/2026-10-15-1797-unparsed-expansions.md
---

# Plan: refuse `$(` and backticks in text bashlex leaves unparsed

Self-contained. You do not need to open the intent or the spec.

**Problem.** bashlex keeps the inside of `${…}` (`parameter` node, attribute
`value`) and a heredoc body (`heredoc` node, attribute `value`) as text and
does not parse them. `echo ${X:-$(nc x)}` is extracted as `['echo']` and
`cat <<EOF` / `$(nc x)` / `EOF` as `['cat']`, but bash runs `nc`. Inside
those regions only `$(…)` and a backtick can run a command (confirmed in
bash; process substitution stays literal there; arithmetic `$((` starts
with `$(`).

This branch builds on PR #1802 (#1783), whose generic walk in
`_collect_command_nodes` visits every child node, including
`redirect.heredoc`.

**Approved decisions.**

1. In `apps/backend/security/ast_parser.py`, add a module constant
   `_RUNS_COMMAND = re.compile(r"\$\(|`")` with a one-line comment: inside
   `${…}` and heredoc bodies only `$(…)` and backticks run a command (#1797).
2. In `_collect_command_nodes` (`:131`), after `visited.add(id(node))`: if
   `getattr(node, "kind", None)` is `"parameter"` or `"heredoc"` and
   `_RUNS_COMMAND.search(getattr(node, "value", ""))`, raise
   `UnparseableCommand("command substitution inside ${…} or a heredoc body cannot be validated")`.
3. No caller changes. CHANGELOG entry under Security.

## Steps

### Step 1 — regression tests

File: `tests/test_command_sandbox.py`.

- Append to `DYNAMIC_COMMAND_WORD` (parser must raise), with a comment
  `# Command substitution in text bashlex leaves unparsed (#1797).`:
  `"echo ${X:-$(nc evil 80)}"`, `'echo "${X:-\`nc evil 80\`}"'`,
  `"X=${Y:-$(nc evil 80)}"`, `"ls > ${X:-$(nc evil 80)}"`,
  `"cat <<EOF\n$(nc evil 80)\nEOF"`, `"cat <<-EOF\n\t\`nc evil 80\`\nEOF"`,
  `"cat <<EOF\n${X:-$(nc evil 80)}\nEOF"`.
- Add to `MUST_PARSE` (exact names): `"echo ${X:-default}": ["echo"]`,
  `'echo "${HOME}/x"': ["echo"]`, `"cat <<EOF\nhello $USER\nEOF": ["cat"]`,
  `"git commit -F - <<EOF\nmsg\nEOF": ["git"]`.

Check: `pytest tests/test_command_sandbox.py -q` — the new
`DYNAMIC_COMMAND_WORD` rows FAIL on unfixed code (they parse without
raising).

Trap: the heredoc rows need real newlines (`\n` in a normal Python string,
not a raw string). Run `ruff format` on the file.

### Step 2 — the fix

File: `apps/backend/security/ast_parser.py`. Apply decisions 1-2.

Check: `pytest tests/test_command_sandbox.py tests/test_plan_commands.py -q`
pass; `ruff check`, `ruff format --check`.

### Step 3 — CHANGELOG

`### Security` bullet under `## [Unreleased]` in `CHANGELOG.md`.

## Rollback

Revert the fix commit; the tests commit goes with it.
