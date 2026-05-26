# Claude Code `settings.json` — AIFactory sample explainer

> Sample file: [`settings.example.json`](./settings.example.json)
> Issue: #12 — part of Epic #6 (Claude Code / Agent SDK compliance audit).

The sample file is a starting point for `~/.claude/settings.json`. Copy it
there, tweak the opinionated bits for your machine, and Claude Code picks
up the changes on the next session. This explainer walks through each
field, with extra attention to the post-Jan-2026 ones that aren't well
documented elsewhere.

## Where the file lives (scope and precedence)

Claude Code reads settings from four scopes, highest precedence first:

| Scope | Path | Use for |
| --- | --- | --- |
| Managed | OS-specific (enterprise IT) | Cannot be overridden — admin lock-down |
| Local | `.claude/settings.local.json` | Your private per-project tweaks (gitignored) |
| Project | `.claude/settings.json` | Team-shared per-project config (commit) |
| User | `~/.claude/settings.json` | Your global defaults |

**The AIFactory sample targets the User scope.** Copy it to
`~/.claude/settings.json` and it applies across every project.

## Strict JSON, not JSONC

Claude Code does **not** accept comments. Trailing commas, `//` or
`/* */` blocks all break validation. If you want comments, keep them in
this file and edit the JSON without them.

## `$schema` — IDE validation

```json
"$schema": "https://json.schemastore.org/claude-code-settings.json"
```

The community-maintained JSON Schema lives on
[json.schemastore.org](https://json.schemastore.org/claude-code-settings.json).
VS Code, JetBrains and friends will validate against it once `$schema` is
set. The schema may lag the official docs by a few weeks — Claude Code
itself never validates against schemastore.

## Model + effort

```json
"model": "claude-sonnet-4-6",
"effortLevel": "high",
"alwaysThinkingEnabled": true
```

- **`model`** — default Claude model. Override per-session with `/model`.
- **`effortLevel`** — **NOT `effort`**. This is the most common settings
  mistake. The top-level field is `effortLevel`; accepts `"low"`,
  `"medium"`, `"high"`, `"xhigh"`. The bare `effort` keyword exists only
  inside hook `if:` predicates, which is a different surface entirely.
- **`alwaysThinkingEnabled`** — enable extended thinking by default
  (Opus 4.7 always does adaptive thinking regardless; this matters for
  Sonnet/Haiku). See PR #15 / Issue #7 for AIFactory's thinking config.

## `permissions` — what Claude can do without asking

```json
"permissions": {
  "allow": ["Bash(npm run *)", "Read(src/**)"],
  "ask":   ["Bash(git push *)"],
  "deny":  ["Read(.env)", "WebFetch(*.internal.*)"]
}
```

Three buckets:

- **`allow`** — pre-approved. No prompt.
- **`ask`** — pattern matches, Claude prompts you per use.
- **`deny`** — pattern matches, Claude refuses (no prompt, no override
  via skill `allowed-tools`).

**Tool names are case-sensitive**: `Bash`, `Read`, `Write`, `Edit`,
`Glob`, `WebFetch`, etc. `bash` (lowercase) silently never matches.

**`deny` wins everywhere**, including over skill-scoped `allowed-tools`.

## `hooks` — pre-approval validators

```json
"hooks": {
  "PreToolUse": [
    {
      "matcher": "Bash",
      "hooks": [
        {
          "type": "command",
          "if": "Bash(rm -rf *)",
          "command": "bash",
          "args": ["-c", "echo 'blocked' >&2 && exit 2"],
          "timeout": 5
        }
      ]
    }
  ]
}
```

Structure (the nesting is intentional):

- `hooks` → object keyed by **event name** (`PreToolUse`, `PostToolUse`,
  `SessionStart`, `Stop`, etc.).
- Each event → **array of matcher groups**.
- Each matcher group → `matcher` (tool name, supports `|` and regex)
  plus an **inner `hooks` array** of handler entries.
- Each handler → `type` (`command`, `http`, `mcp_tool`, `prompt`, `agent`),
  optional `if` predicate (extra filter past the matcher), command +
  args + timeout (for `type: "command"`).

Handler exit semantics for `type: "command"`:

- Exit `0` + JSON on stdout → structured decision (allow / deny / ask).
- Exit `0` + no JSON → allow.
- Exit `2` (or any non-zero) → deny with stderr shown to Claude.

### AIFactory's two hook surfaces — they don't conflict

`apps/backend/core/security.py:bash_security_hook` is registered at the
**Claude Agent SDK** level — it fires when AIFactory's own backend
spawns a Claude session for a build. The hooks block in this
`settings.json` fires when **you** invoke `claude` interactively. Two
different runtimes; both apply in their own contexts.

## Skill controls

```json
"skillOverrides": {
  "deploy": "off",
  "second-brain": "name-only"
},
"skillListingBudgetFraction": 0.015,
"maxSkillDescriptionChars": 1024
```

- **`skillOverrides`** — per-skill toggle. Values:
  - `"off"` — skill is hidden entirely.
  - `"name-only"` — name visible, description hidden (saves context).
  - Omitted — full visibility.
- **`skillListingBudgetFraction`** — fraction of the context window
  Claude Code allocates to skill descriptions. **0 to 1**, not 0 to 100.
  Default 0.02 (2%). The sample uses 0.015 (1.5%) — tighter, leaves
  more room for your prompts.
- **`maxSkillDescriptionChars`** — per-skill description cap. Default
  2048. Lowering to 1024 forces skill authors to write tighter docs.

## `sandbox` — filesystem guardrails

```json
"sandbox": {
  "enabled": false,
  "filesystem": {
    "denyRead": ["~/.aws/credentials", "~/.ssh/id_rsa", ".env"]
  }
}
```

`enabled: false` keeps the broad sandbox off (the AIFactory backend has
its own SDK-level allow-list); the `denyRead` patterns still apply as a
belt-and-braces guard against accidental secret reads.

## Common pitfalls

1. **`effort` vs `effortLevel`** — top-level field is `effortLevel`.
   `effort` is only valid inside hook `if:` predicates.
2. **Comments in JSON** — strict JSON only. No `//`, no trailing
   commas, no `/* */`.
3. **`skillListingBudgetFraction: 50`** — that's 5000% of context, not
   50%. Use `0.5` for half.
4. **Hooks without `matcher`** — every matcher group needs an explicit
   `matcher` field. Use `"*"` to match all tools of that event.
5. **`Bash(rm *)` matches `rmdir`** — globs are substring-style, not
   command-name aware. Use `Bash(rm -rf *)` to be specific.
6. **Lowercase tool names** — `Bash`, not `bash`. Case-sensitive.
7. **Forgetting that `deny` wins** — a skill's `allowed-tools` cannot
   override a user-level deny rule.

## Verifying your file

```bash
# Strict JSON validity
python -m json.tool < ~/.claude/settings.json > /dev/null && echo OK

# Open Claude Code with the new settings
claude --version
# Expected: no validation warnings on startup.
```

## References

- Official Claude Code settings docs: <https://code.claude.com/docs/en/settings.md>
- Hooks docs: <https://code.claude.com/docs/en/hooks.md>
- Permissions docs: <https://code.claude.com/docs/en/permissions.md>
- JSON Schema: <https://json.schemastore.org/claude-code-settings.json>
