# AIFactory Compliance Audit — May 2026

> Generated: 2026-05-21
> Scope: Claude Code CLI, Claude Agent SDK, Google Antigravity, Gemini CLI, Codex CLI, Ollama, OpenAI-compatible providers, MCP
> Source: parallel research across docs + codebase survey (commit `27ac0df`)

## 1. Executive summary

AIFactory's Claude side is **mostly current** and uses many of the right primitives (extended thinking, structured output, sub-agents, MCP, OAuth tokens, custom MCP server validation). It is **materially behind on three things**:

1. **Google Antigravity** — Google re-launched Antigravity as the strategic successor to Gemini CLI two days ago (I/O 2026, May 19). AIFactory currently has only a *binary-fallback path* in the Gemini provider — no SDK, no Managed Agents API, no OAuth. This is the largest gap if "multi-provider parity" is a goal.
2. **Prompt caching** — every agent session reloads the same project context (CLAUDE.md, spec context, memory snapshots) without `cache_control`. The Claude API now caches at the workspace level with a 90 % discount on cache hits, which would stack with the existing Batch API 50 % discount.
3. **Adaptive / interleaved thinking on Opus 4.7** — AIFactory still passes manual `max_thinking_tokens` for Opus 4.7. The current model defaults to `type: "adaptive"` and unlocks interleaved thinking between tool calls. Manual mode on Opus 4.7 is sub-optimal (slower, more tokens used than needed).

Beyond those, the smaller gaps are skill-frontmatter modernisation, settings.json fields shipped after Jan 2026, and a missing `.mcp.json` for project-scoped MCP servers.

## 2. Current state — integration-by-integration

| Integration | Status | File(s) |
| --- | --- | --- |
| Claude Agent SDK | Solid — thinking, structured output, sub-agents, MCP, hooks, OAuth | `apps/backend/core/client.py`, `phase_config.py` |
| Claude Code CLI | Auth + token only; no slash commands / hooks shipped | `core/auth.py`, project `CLAUDE.md` |
| Codex CLI | Text + agentic (MCP JSON-RPC) | `providers/codex.py`, `codex_agentic.py` |
| Gemini CLI | Text + `--yolo` agentic; falls back to Antigravity binary if present | `providers/gemini.py`, `gemini_agentic.py` |
| **Google Antigravity** | **Binary-path detection only — no SDK, no API, no OAuth** | `providers/gemini.py:66-79` |
| Ollama | Text + native tool-calling agentic | `providers/ollama.py`, `ollama_agentic.py` |
| OpenAI-compatible (LM Studio, vLLM, OpenRouter, Together, Groq, LocalAI) | Text + agentic w/ tool_calls | `providers/openai_compatible*.py`, `factory.py` |
| MCP | Context7, Graphiti, Playwright, custom aifactory tools — all wired | `core/client.py:756-812` |

### What's good

- `phase_config.MODEL_ID_MAP` already routes the shorthand → full IDs and lists Opus 4.7, Opus 4.6 (1M), Opus 4.5, Sonnet 4.6, Haiku 4.5.
- `bash_security_hook` registered on `PreToolUse` — exactly the documented pattern.
- Custom MCP server validation (`client.py:245-287`) rejects shell commands and dangerous flags — better than what most projects ship.
- Phase-aware tool binding via `AGENT_CONFIGS` mirrors Claude Code's per-skill `allowed-tools` philosophy.

### What's missing

| Capability | Current | Recommended | Cost / risk if ignored |
| --- | --- | --- | --- |
| Adaptive thinking for Opus 4.7 | Manual `max_thinking_tokens` | `{"type": "adaptive"}` on Opus 4.7; manual elsewhere | 15-30 % slower sessions, higher token bill |
| Interleaved thinking between tool calls | Off | Set `interleaved-thinking-2025-05-14` beta header for planner / coder | Loses mid-execution reasoning quality |
| Prompt caching on static context | None | `cache_control: {"type": "ephemeral"}` on CLAUDE.md + project context blocks | ~90 % cache-hit discount lost on every session |
| Files API | Not used | Use for large doc uploads in spec gather phase | Spec_gatherer truncates large inputs unnecessarily |
| Citations | Not used | Surface in QA reviewer output | Less auditable QA traces |
| `.mcp.json` (project-scoped MCP servers) | All in settings | Migrate aifactory + Graphiti to `.mcp.json` so they commit | Teammates can't pick up your custom MCP servers |
| Skill frontmatter `allowed-tools`, `context: fork`, `hooks`, `paths` | Not used in shipped skills | Add to skills you author | Skills are uglier to permission |
| Settings: `skillOverrides`, `skillListingBudgetFraction`, `maxSkillDescriptionChars` | Not used | Set sensible defaults when shipping skills | Skills can flood the description window |
| Batch API for spec runs | Not used | Use for multi-spec / multi-story parallel runs | Pay 100 % when you could pay 50 % |
| Antigravity SDK | None | Add an `AntigravityProvider` next to `gemini_agentic` | No path to Gemini 3.5 Flash via Managed Agents, no migration off Gemini CLI as Google sunsets it |
| Antigravity OAuth (Google) | None | Add OAuth flow + token storage in settings | Users can't auth their Google account from the UI |
| Antigravity Managed Agents (cloud) | None | Optional, but unlocks cloud worktrees without local Docker | Forces local-only worktrees forever |
| Antigravity headless HTTP API (port 8045) | None | Optional shim — lets AIFactory drive Antigravity as it does Claude CLI | No headless Antigravity in AIFactory |

## 3. Google Antigravity — what changed and what to build

**Status (May 2026):** Google's Antigravity 2.0 is GA. Free tier (20 agent requests/day), Pro $20/mo, AI Ultra $100/$200/mo. The Gemini CLI is being sunset in favour of the Antigravity CLI. Key surfaces:

| Surface | Purpose |
| --- | --- |
| Desktop IDE (Tauri) | End-user agent UX |
| `antigravity` CLI (Go) | Headless agent driver; slash commands `/resume`, `/rewind`, `/permissions`, `/model`, `/skills`, `/mcp`, `/tasks`, `/logout` |
| `google-antigravity` (PyPI) | Three-layer Python SDK: `Agent` → `Conversation/Step` → `Connection` (local or cloud) |
| Managed Agents API (Gemini API) | Cloud-hosted agents with persistent workspace |
| HTTP API (port 8045) | Bearer-token-auth REST API — what `antigravity-gateway` wraps as OpenAI/Anthropic-compatible |

**Default model**: Gemini 3.5 Flash (faster + cheaper than Opus 4.7 on tool-loop benchmarks; community comparisons claim ~4× throughput and lower per-token cost). Model selection inside Antigravity itself is mostly fixed; Managed Agents API allows model parameterisation.

**Concrete actions for AIFactory:**

1. **Add `providers/antigravity.py` (text-only) + `antigravity_agentic.py`** — mirror the existing Codex / Gemini split. Use the `google-antigravity` Python SDK; route `gemini-3.5-flash`, `gemini-3.1-pro` to it.
2. **Register `antigravity` in `providers/factory.py`** — both the agentic and text variants, with phase-aware routing.
3. **Add an OAuth integration** for Google in the project-settings UI (analogous to the GitHub OAuth flow you already have). Token storage: `.aifactory/.env` (`ANTIGRAVITY_OAUTH_TOKEN`).
4. **Auto-register AIFactory's MCP servers** (`context7`, `graphiti-memory`, `aifactory`) in Antigravity's `mcp_servers.json` so the same tools work across providers.
5. **Optional: HTTP API shim** — drive Antigravity in headless mode from `agent_service.py` the same way you drive `claude` today.
6. **Optional: Managed Agents** — for users without a local IDE, run agents in Google's cloud workspace and stream artefacts back to the worktree.

Where to read more (in order of usefulness):
- `https://antigravity.google/`
- `https://developers.googleblog.com/build-with-google-antigravity-our-new-agentic-development-platform/`
- `https://ai.google.dev/gemini-api/docs/managed-agents-quickstart`
- `https://github.com/google-antigravity/antigravity-sdk-python`

## 4. Claude Agent SDK — concrete upgrades

### 4.1 Adopt adaptive thinking on Opus 4.7

`phase_config.py` currently maps thinking via `THINKING_BUDGET_MAP`. The May 2026 guidance is:

| Model | Thinking mode |
| --- | --- |
| Opus 4.7 | `{"type": "adaptive"}` (manual deprecated) |
| Opus 4.6, Sonnet 4.6 | Adaptive recommended; manual still works |
| Haiku 4.5 | Manual only (`{"type": "enabled", "budget_tokens": N}`) |

Action:

- Extend `phase_config.py` with a `THINKING_MODE_FOR_MODEL(model_id)` helper that returns the right shape.
- Update `core/client.py:511-517` and `720-728` so Opus 4.7 sessions pass `thinking={"type": "adaptive"}` instead of a budget.
- For planner + coder agents, also set `betas=["interleaved-thinking-2025-05-14"]` so Claude can reason between tool calls. Already half-supported via `MODEL_BETAS_MAP` — extend it.

### 4.2 Turn on prompt caching for static context

The big wins are:

- CLAUDE.md (loaded into every agent session)
- Project context (`.aifactory/specs/<id>/context.json`)
- BMad architecture docs
- Per-story technical context (architecture, ADRs)

Implementation pattern (`core/client.py` system-prompt builder):

```python
system_blocks = [
    {"type": "text", "text": base_instructions},
    {
        "type": "text",
        "text": claude_md_content,
        "cache_control": {"type": "ephemeral"},
    },
    {
        "type": "text",
        "text": project_context_json,
        "cache_control": {"type": "ephemeral"},
    },
]
```

For long-running thinking sessions, use the 1-hour TTL variant (`cache_control: {"type": "ephemeral", "ttl": "1h"}`).

### 4.3 Skill modernisation

The existing 15 skills work — but they predate fields shipped after Jan 2026:

- `allowed-tools` — pre-approve specific tool patterns per skill.
- `context: fork` + `agent: Explore` — research skills run in their own subagent with minimal context.
- `paths: "src/**"` — auto-activate skill only when files match.
- `hooks: PreToolUse: [...]` — skill-scoped safety rules without polluting global settings.
- `disable-model-invocation: true` — user-only skills.
- `user-invocable: false` — background-knowledge skills.

Action: when authoring AIFactory-specific skills (e.g. a `/spec-gather`, `/spec-critic` skill that wraps the existing spec_agents), use the new frontmatter.

### 4.4 settings.json fields shipped after Jan 2026

| Field | Purpose |
| --- | --- |
| `skillOverrides` | Disable / scope skills per project |
| `skillListingBudgetFraction` | Cap the share of context skill descriptions can consume |
| `maxSkillDescriptionChars` | Per-skill description budget |
| `effort` | `low|medium|high|xhigh|max` global effort default |
| `theme` | `dark-ansi`, etc. — UI hint, low priority |

Ship a sample `settings.json` (under `guides/`) so AIFactory users get sensible defaults when they enable skills.

### 4.5 Batch API for parallel spec runs

`spec_runner.py` (and BMad's per-story session spawner) is the natural caller. Submit N spec-creation jobs as a batch; collect on poll. Combined with prompt caching, the cost-per-spec drops materially.

## 5. Claude Code CLI (slash-command surface)

AIFactory uses Claude **only via the SDK**. The CLI itself is barely touched (`claude setup-token` plus version detection). That's fine — AIFactory is a *web platform*, not an IDE wrapper — but a few CLI features would help users who also work in Claude Code directly:

1. **Ship custom skills** for AIFactory workflows: `/aifactory-spec`, `/aifactory-build`, `/aifactory-qa`. Distribute via the `skills/` directory you already have.
2. **Ship a `.claude/agents/` definition** for AIFactory's planner / coder / qa_reviewer so users can `/agent planner` directly from Claude Code (project-level adoption).
3. **Ship `.mcp.json`** with the aifactory MCP server, so users get the same MCP tools whether they run from the web UI or Claude Code.

These are user-facing wins, not platform compliance — but they're cheap.

## 6. Other providers

- **Codex CLI** — already on agentic MCP mode. No changes needed; track for `gpt-5.3-codex` model bumps.
- **Gemini CLI** — Google is sunsetting this in favour of Antigravity. Keep current path working but stop investing.
- **Ollama** — current. Consider adding thinking-budget exposure for models that support it (`qwen3` adds reasoning).
- **OpenAI-compatible** — current. Adding prompt caching support when the backend advertises it (LM Studio recently added cache_control passthrough) is a future win.

## 7. Suggested execution order

Strict cost / value ranking:

| # | Action | Effort | Value |
| --- | --- | --- | --- |
| 1 | Adaptive thinking for Opus 4.7 (`phase_config.py`, `client.py`) | S | High — faster, cheaper, better |
| 2 | Prompt caching on CLAUDE.md + project context (`client.py` system-block builder) | M | High — 90 % discount on every session |
| 3 | `AntigravityProvider` (text + agentic) + factory entry + Google OAuth | L | High strategically — Google CLI is being sunset |
| 4 | Skill frontmatter (`allowed-tools`, `context: fork`) on AIFactory skills | S | Medium |
| 5 | Interleaved thinking beta for planner + coder | S | Medium |
| 6 | `.mcp.json` for the aifactory MCP server | S | Medium |
| 7 | Batch API in `spec_runner.py` | M | Medium |
| 8 | Antigravity Managed Agents (cloud workspace) | L | Optional — only if cloud worktrees matter |
| 9 | settings.json new fields (`skillOverrides`, etc.) | XS | Low |
| 10 | Citations / Files API in spec_gatherer | M | Low |

## 8. Acceptance check (when this audit can be closed)

- [ ] `phase_config.py` differentiates thinking shape by model
- [ ] At least one agent session shows `cache_creation_input_tokens > 0` and `cache_read_input_tokens > 0` on a re-run
- [ ] `providers/factory.py` returns an `AntigravityProvider` instance when `gemini-3.5-flash` is requested
- [ ] A teammate cloning the repo gets the aifactory MCP server registered without editing `settings.json`
- [ ] At least one skill ships with `allowed-tools` + `context: fork`

---

## 9. Status update — 2026-05-21 (later same day)

Cross-checked against repo state after this working session. No new external research — this is a delta against §7 and §8.

### 9.1 Audit acceptance checklist — current status

- [ ] `phase_config.py` differentiates thinking shape by model — **not started**
- [ ] Agent session shows `cache_creation_input_tokens > 0` / `cache_read_input_tokens > 0` — **not started**
- [ ] `providers/factory.py` returns an `AntigravityProvider` — **not started**
- [ ] Teammate cloning gets aifactory MCP registered without editing `settings.json` — **not started**
- [ ] At least one skill ships with `allowed-tools` + `context: fork` — **not started**

No item from §7's priority list (1–10) was shipped in this session.

### 9.2 Adjacent platform work shipped (not on the audit list, but relevant)

| Area | Change | File(s) |
| --- | --- | --- |
| GitLab provider — PR creation | New `GitLabProvider.create_pr()` method (POSTs `/api/v4/projects/:id/merge_requests`, supports `Draft:` prefix). Verified against gitlab.com — opened MR !676 in compliance-calitii/sarc. | `apps/backend/runners/github/providers/gitlab_provider.py` |
| PR routing | `create_pr_from_task` now branches on `_use_provider_api(project_id)` and calls `provider.create_pr(...)` for GitLab/ADO; gh CLI path reserved for GitHub. Closes the "Head ref must be a branch" GraphQL failure. | `apps/web-server/server/routes/tasks.py` (≈line 3203) |
| Issue investigation | `investigate_github_issue` now routes through the provider abstraction (`provider.fetch_issue`, `_get_provider_issue_comments`). GitLab issue #394 fetched end-to-end. | `apps/web-server/server/routes/github.py` (≈line 1374) |
| Provider-aware UI | `NotConnectedState`, `InvestigationDialog`, `GitHubPRs` accept `provider` prop → render `GitHub / GitLab / Azure DevOps` labels and icons. i18n key uses `{{provider}}` placeholder. | `apps/frontend-web/src/components/github-issues/components/EmptyStates.tsx`, `InvestigationDialog.tsx`, `GitHubIssues.tsx`, `github-prs/GitHubPRs.tsx`, `shared/i18n/locales/en/common.json` |
| Settings persistence | `handleSettingsChange` now auto-persists all six git fields (`gitProvider`, `gitToken`, `gitBaseUrl`, `gitOrg`, `gitProject`, `gitRepo`) via `updateProjectSettings` on every keystroke — values stick without an explicit Save click. | `apps/frontend-web/src/components/settings/integrations/GitHubIntegration.tsx` |
| Skills system | 15 Claude SKILL.md files converted to AIFactory's flat schema (`skills/<category>/<name>.md`) across 6 categories; SkillsService re-indexed. | `skills/` (new), `/tmp/copy_claude_skills.py` |
| Self-healing UI | `wsManager.onConnect('/ws/events')` triggers `loadTasks(projectId)` on reconnect; `visibilitychange` listener does the same when the tab returns to foreground. Resolves "task stuck at qa_review 100 %" symptom. | `apps/frontend-web/src/lib/api-adapter.ts` |
| Backend import fix | `from roadmap import RoadmapOrchestrator` → `from .roadmap import ...`. Was breaking the entire `runners.github.providers` import chain when called from the web server. | `apps/backend/runners/roadmap_runner.py:33` |

These ship in the same direction as §7 items 3 and 6 — the platform's GitLab story is now functional end-to-end, which makes the future Antigravity provider work and `.mcp.json` rollout easier (the same routing pattern applies).

### 9.3 Re-baselined priority for the next session

Re-ordered with this session's shipped work treated as done:

| # | Action | Effort | Value | Notes |
| --- | --- | --- | --- | --- |
| 1 | **Adaptive thinking for Opus 4.7** | S | High | `phase_config.py:THINKING_BUDGET_MAP` + `core/client.py:511-517,720-728`. `ADAPTIVE_THINKING_MODELS` set already exists — only need a `THINKING_MODE_FOR_MODEL()` helper and the call-site rewrite. |
| 2 | **Prompt caching on static context** | M | High | One-file change in `core/client.py` system-block builder. Adds `cache_control: {"type": "ephemeral"}` to CLAUDE.md + project context blocks. |
| 3 | **`AntigravityProvider` + Google OAuth** | L | High strategically | Google is sunsetting Gemini CLI; Antigravity is the path forward. Could ship the SDK-backed provider first, defer OAuth UI. |
| 4 | **Backend `task:update` deduplication** | S | Medium | Suppress identical re-emissions in `_emit_progress` (cuts ~90% of WebSocket noise during long phases). Reduces log spam and makes real transitions readable. |
| 5 | **Skill frontmatter modernisation** | S | Medium | Apply `allowed-tools` + `context: fork` to AIFactory-shipped skills. |
| 6 | **`.mcp.json` for the aifactory MCP server** | S | Medium | Teammates get the custom MCP tools without editing settings. |
| 7 | **Terminal-transition emission collapse** | S | Medium | Trim the 5-event flurry at process exit to a clean `task:update → task:status` pair. Removes the `phase: N/A` blip. |
| 8 | **Interleaved thinking beta for planner + coder** | S | Medium | `betas=["interleaved-thinking-2025-05-14"]` for those two agent types. |
| 9 | **Batch API in spec_runner.py** | M | Medium | Stacks with prompt caching for ~95 % savings on bulk spec runs. |
| 10 | **settings.json new fields** | XS | Low | Ship a sample `guides/settings.example.json`. |
| 11 | Antigravity Managed Agents (cloud workspace) | L | Optional | Only if cloud worktrees become a goal. |
| 12 | Citations / Files API in spec_gatherer | M | Low | Defer. |

Items 4 and 7 are new entries added from the "stuck task" diagnosis earlier this session.

### 9.4 Recommended next step

Items 1 and 2 remain the highest leverage: both single-file changes, both in `apps/backend/core/client.py` + `phase_config.py`, both ship measurable cost/quality wins on every Claude session. Recommend pairing them in one PR.

The Antigravity provider (item 3) is strategic but L-effort; worth planning before starting (factor `providers/antigravity.py` + `antigravity_agentic.py`, decide on Managed-Agents-vs-local-SDK first, then OAuth surface). A short design doc before code would be appropriate.

---

*Audit assembled from three parallel research streams: local codebase survey (Explore agent), Google Antigravity research (search-specialist), and Claude Code / Agent SDK current docs (claude-code-guide). Status update §9 added 2026-05-21 after a working session that fixed the GitLab provider, PR creation, skills, and WebSocket reconnect.*
