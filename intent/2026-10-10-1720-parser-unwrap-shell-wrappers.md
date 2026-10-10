---
status: draft
issue: 1720
author: olafkfreund
---

# Intent: wrapped commands skip the per-command Bash validators

Follow-up to #1689. Still open on dev (checked at c59a0ea6). No commit or PR
on dev references it.

## Problem

The Bash hook (`apps/backend/security/hooks.py`) does two separate checks:

1. It checks the command names against the allowlist. The bashlex parser
   (`security/ast_parser.py`) finds these names, and it does look inside
   `bash/sh -c`, `env`, `xargs` and `find -exec`.
2. It runs a validator for each command name, for example `validate_git`. The
   validator's input comes from the old regex parser
   (`security/parser.py:get_command_for_validation`), which looks inside no
   wrapper at all. If it finds no matching segment, the validator gets the
   whole raw outer string.

As a result, wrapping a command gets it past its validator. These were run
against the real `validate_command` and `bash_security_hook` with bashlex
loaded:

| Command | Result |
|---|---|
| `git -c core.fsmonitor=x status` | blocked (control) |
| `command git -c core.fsmonitor=x status` | **allowed**: the parser sees only `command`, and `validate_git` never runs |
| `exec git -c core.fsmonitor=x status` | **allowed**, for the same reason |
| `bash -c 'git --upload-pack=/tmp/evil ls-remote x'` | **allowed** |
| `sh -c 'git --upload-pack=/tmp/evil ls-remote x'` | **allowed** |
| `bash -c 'git --config-env=core.fsmonitor=FOO status'` | **allowed** |
| `echo core.fsmonitor x \| xargs git config` | **allowed** |
| `bash -c 'git -c core.fsmonitor=x status'` | blocked, but only by accident: the outer `bash -c` is read as git's own `-c` |

Each of the allowed payloads can make git run an arbitrary program. No
refusal is logged, so the bypass leaves no trace. This needs no special
configuration: `command`, `exec`, `xargs`, `sh`, `bash` and `git` are all
base-allowlisted (`project/command_registry/base.py`), so every session and
project profile is affected.

Without bashlex (the fallback path), the old parser does not even find the
inner command name, so the inner command also skips the allowlist.

## Proposed outcome

- Each per-command validator receives the inner command, after wrappers are
  removed, and not the raw outer string. This covers `bash/sh -c`, `xargs`
  and the `command`/`exec` prefixes.
- Every row marked "allowed" in the table above is refused, with a reason that
  names the real dangerous flag or key.
- The `bash -c 'git -c core.fsmonitor=…'` case is refused on purpose, not by
  the accidental match.
- Unwrapped commands that are legitimate (`bash -c 'npm test'`,
  `command -v git`, `find … | xargs grep`) still pass.
- The sandbox tests cover each case and assert the refusal reason, not just
  that the command was refused.

## Affected users and systems

- Every AIFactory agent session that uses the Bash tool. `bash_security_hook`
  is the only Bash PreToolUse gate (`core/client.py:1031`).
- The in-process tool executor (`tools/executor.py:286-299`), which uses the
  same hook.
- Git on the server that touches agent worktrees, and the host operator. A
  `core.fsmonitor` or `--upload-pack` value that slips through runs with the
  backend's privileges.
- Code: `security/hooks.py`, `security/parser.py`, `security/ast_parser.py`,
  `security/git_validators.py`, and `tests/test_command_sandbox.py` and
  `tests/test_sandbox_escape_corpus.py`.
- Probably every validator in `security/validator_registry.py`, such as the
  `rm`/`chmod` path checks from #364, since they are called the same way. Only
  the git cases were run.

## Constraints

- The fix must fail closed. Whatever the parser cannot read is refused, and
  that includes the path where bashlex is not installed.
- No legitimate use of `command`, `exec`, `xargs`, `sh` or `bash` may be
  removed from the allowlist. Real agent runs depend on them.
- It depends on #1689. That branch (`fix/1689-fsmonitor-git-config-audit`)
  already edits `hooks.py`, `parser.py`, `git_validators.py` and
  `tests/test_command_sandbox.py` (commits 9f9580a2 and bf40c451), and it is
  not on dev yet. Until it lands, plain `git config core.fsmonitor x` is still
  allowed. #1720 must build on it, or the two will conflict.
- Stay consistent with #1719 (`.git/` write guard) and #1717 (pin
  ssh/pager/editor), which cover the same git-escape area.
- The sandbox tests, `security-lint.yml` and `security-cleanup-gates.yml` must
  stay green.

## Open questions

1. Sequencing: wait for #1689 to merge and then branch from dev, or stack on
   the #1689 branch now?
2. `xargs <command that has a validator>`: the arguments come from stdin and
   cannot be checked ahead of time. Refuse it in every case, refuse it only
   when the target has a validator (such as git), or accept the risk?
3. Which prefixes to unwrap: only `command` and `exec`, which the issue names,
   or also `builtin`, `nohup`, `time`, `nice`, `timeout` and `stdbuf`?
4. Without bashlex: refuse any wrapper or prefix, or teach the old regex
   parser to unwrap them too, which means keeping two implementations?
5. Nested or dynamic payloads (`bash -c "sh -c '…'"`, `bash -c "$VAR"`):
   unwrap recursively up to a fixed depth and refuse anything deeper or
   dynamic, or unwrap only one level?
