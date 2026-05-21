---
name: aifactory-spec
description: >
  Drive AIFactory spec creation via apps/backend/runners/spec_runner.py.
  Triggers when the user asks to create a spec, start a new feature, or
  invokes /aifactory-spec.
when_to_use: >
  Activate when the user says "create a spec", "start a feature",
  "build <X>", "/aifactory-spec", or otherwise wants to kick off the
  AIFactory planning pipeline.
allowed-tools:
  - Bash(python apps/backend/runners/spec_runner.py *)
  - Bash(python apps/backend/run.py --list)
  - Read
  - Glob
context: fork
agent: general-purpose
paths:
  - apps/backend/**
  - .aifactory/**
  - .claude/skills/aifactory-spec/**
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          if: Bash(rm *)
          command: 'echo "rm is not permitted inside aifactory-spec skill" >&2 && exit 2'
disable-model-invocation: false
user-invocable: true
---

# /aifactory-spec — Drive AIFactory spec creation

Use the AIFactory planning pipeline (`apps/backend/runners/spec_runner.py`) to turn a one-line task description into a multi-phase spec under `.aifactory/specs/<NNN-name>/`.

This skill is intentionally narrow: it shells out to the project's own spec runner, which handles complexity detection, multi-phase scaffolding, and the spec/context/plan handoff. Do not duplicate that logic here.

## Workflow

1. Ask the user for a one-line task description.
   - If they name a file, `Read` it instead.
   - Keep the description focused — one sentence works better than three paragraphs because the runner has its own complexity detection.
2. Run the spec runner:

   ```
   python apps/backend/runners/spec_runner.py --task "<their description>"
   ```

   Let complexity auto-detect. Only pass `--complexity simple|standard|complex` if the user explicitly asks.
3. Stream the runner's stdout to the user. Don't summarise — the multi-phase log is informative, and the user expects to see it.
4. When the runner exits, locate the new spec directory under `.aifactory/specs/<NNN-name>/` and `Read` its `spec.md` to confirm the result.
5. Tell the user where the spec landed and suggest `/aifactory-build` (follow-up skill — not yet shipped) as the next step.

## Boundaries

This skill's `allowed-tools` is deliberately narrow:

- `Bash(python apps/backend/runners/spec_runner.py *)` — drives spec creation.
- `Bash(python apps/backend/run.py --list)` — lists existing specs before creating a new one.
- `Read` — for the user's task-description file and the resulting `spec.md`.
- `Glob` — for locating the new spec directory.

That's it. Within this skill:

- **Do not** run arbitrary `Bash`. If the user asks for `npm install` or anything similar, tell them to drop out of the skill (a permission prompt will fire if they try anyway).
- **Do not** `Write` or `Edit` files directly — the runner owns those writes.
- **Do not** `pip install` packages. If the backend venv is missing, tell the user to run `npm run install:backend` from the repo root and stop.

A skill-scoped `PreToolUse` hook blocks any `rm` command from inside this skill — guard against the model trying to clean up failed runs by deleting files.

## Reference

- Spec-runner CLI: `apps/backend/runners/spec_runner.py` (CLI surface at lines 129–211).
- List existing specs: `python apps/backend/run.py --list`.
- Companion skills (to be authored in future issues):
  - `/aifactory-build` — wraps `python apps/backend/run.py --spec <NNN>`.
  - `/aifactory-qa` — wraps the QA loop.

## Note on hook composition

AIFactory ships an SDK-level `bash_security_hook` at `apps/backend/core/security.py` that runs inside AIFactory's own backend (when its runners spawn Claude). The skill-scoped `PreToolUse` hook above runs inside the Claude Code CLI when a user invokes `/aifactory-spec` interactively. These are two distinct runtime surfaces — both are active in their respective contexts, neither overrides the other.
