---
status: draft
issue: 1692
author: olafkfreund
---

# Intent: non-Claude agent CLIs must not inherit the runner's credentials

## Problem

Still open on dev (head `1c2df772`). That commit (#1680, PR #1691) names
#1692 as a follow-up and leaves it unfixed.

Since #1680 the runner (`run.py`) starts with a scrubbed environment, but it
deliberately keeps `GITHUB_TOKEN`, `GH_TOKEN` and the OpenAI,
OpenAI-compatible, Gemini, Google, OpenRouter and Voyage keys (`RUNNER_KEEP`
in `apps/web-server/server/utils/subprocess_env.py`). With `runner=True`,
other credential-shaped names that are not on the deny list also survive,
for example `CLAUDE_CODE_OAUTH_TOKEN` and `CONTEXT7_API_KEY`. Keys merged in
from `backend/.env` and `<project>/.aifactory/.env` are added after the scrub.

The Claude SDK agent removes these again (`core/auth.py`,
`get_agent_env_blanks`). The non-Claude agent CLIs do not. Six spawn sites
hand them the runner environment unchanged:

- `providers/codex_agentic.py`: no `env=`
- `providers/antigravity_agentic.py`: `{**os.environ, ...}`
- `providers/copilot_agentic.py`: `{**os.environ, ...}`
- `providers/opencode_agentic.py`: `os.environ.copy()`
- `providers/codex.py` and `providers/antigravity.py` (text-only): no `env=`

The issue names only the first two. These CLIs run model-driven shell
commands in the task worktree (Codex full-auto, Antigravity `--yolo`,
`COPILOT_ALLOW_ALL`). A prompt-injected agent can run `env` and take the
operator's GitHub token (push, PR and API access) and the other vendors'
billing keys. Nothing fails or logs, so the exposure is invisible.

## Proposed outcome

- Every non-Claude agent CLI spawn gets an environment with no GitHub token
  and no provider key except the ones that CLI needs to authenticate.
- `ANTHROPIC_API_KEY*` never reaches a non-Claude CLI.
- Each CLI still works with the auth it supports today: an API key from the
  environment, or its own OAuth login on disk.
- A test per changed spawn site fails if a foreign secret reaches the child,
  or if the provider's own key or its required flag is missing.

## Affected users and systems

- `apps/backend/providers/`: the six spawn sites above.
- `apps/backend/core/child_env.py` (reused) and `core/auth.py` (the deny
  source, read only).
- Any task whose coding, QA, QA-fixer, spec or planning phase uses the
  codex, antigravity/gemini, copilot or opencode provider, plus text-only
  Codex/Antigravity calls (QA review, complexity).
- Local, in-process and kubejob runners alike. In kubejob, the Job spec
  injects the keys as env, so the fix has to work inside the runner pod on
  its own.
- Operators whose GitHub token and vendor keys are exposed today.

## Constraints

- Reuse `child_env(keep=..., extra=...)` with the default `runner=False`.
  Do not add a second deny list, and do not edit `core/auth.py`. #1674 is
  changing it.
- Build the environment per spawn, not at import.
- `HOME`, `PATH` and `XDG_*` must survive, or the CLIs lose their OAuth
  logins (`~/.codex`, gemini, copilot) and OpenCode's catalogue. Keep each
  CLI's flags (`GEMINI_CLI_TRUST_WORKSPACE`, `COPILOT_ALLOW_ALL`, OpenCode's
  autoupdate-disable).
- Custom endpoints read from env (base URLs, `OPENAI_COMPATIBLE_*`, Google
  project and location) must still reach the CLI.
- `child_env` also sets `GIT_CONFIG_*` to disable git hooks. The CLIs will
  inherit that. The PR must say so, and it must not clash with #1689/#1690.
- Do not claim coverage of the in-house tool loop
  (`tools/executor.py`, used by the OpenAI-compatible and Ollama agents).
  #1674's spec excludes it from #1692.
- Copilot's GitHub-token decision must not conflict with #1688 (per-call
  GitHub token) or #1671 (GitHub identity merge).
- Put new tests in a new file. `tests/test_child_process_env.py` has
  uncommitted edits in the #1671 and #1673 worktrees.
- No Helm or Job-spec changes.
- One PR, based on dev. It is a `fix(security)` change, so the PR must say
  whether keys change on any deployed path.

## Open questions

1. Scope: fix only the two spawn sites the issue names, or all six?
2. Copilot: does it keep `GH_TOKEN`/`GITHUB_TOKEN`, or rely only on its own
   OAuth login, with an operator who needs a token opting in?
3. OpenCode: does it keep every provider key the runner has, or only the key
   for the configured model?
4. Git hooks: accept that agent CLIs run with hooks disabled through
   `child_env`'s `GIT_CONFIG_*`, or use a variant without it?
5. Tool loop: fold the `tools/executor.py` scrub (OpenAI-compatible and
   Ollama agents) into #1692, or file it as a separate follow-up issue?
