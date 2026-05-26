# Prompt Caching in AIFactory

> Issue #8 — Part of Epic #6 (Claude Agent SDK / Antigravity compliance audit).

AIFactory uses Anthropic's prompt-caching feature to avoid paying full
input-token cost for the static portion of every agent session's system
prompt. On a cache hit, the cached portion is billed at **0.10× the
base input price**.

## How it works

The Claude Agent SDK's `ClaudeAgentOptions.system_prompt` field accepts
only `str` — there is no way to pass `cache_control` markers through
the SDK to the CLI subprocess (verified against
`claude-agent-sdk==0.2.82`).

So AIFactory does the next-best thing: it builds the system_prompt
**byte-identical** across sessions of the same project. The Anthropic
API automatically caches identical prefixes server-side without
needing explicit markers. The helper that enforces the byte-identical
guarantee lives at `apps/backend/core/cache.py:build_cached_system_str`.

The flow:

```
create_client(project_dir, …, model, agent_type)
   │
   ├── load CLAUDE.md (if enabled)            ← stable
   ├── build agent-type-specific intro        ← dynamic (last in prompt)
   ├── build_cached_system_str(
   │       base_instructions=…intro,
   │       claude_md_content=…CLAUDE.md,
   │       model=…,                            ← drives min-size guard
   │       project_dir=str(project_dir),       ← drives hash-change warning
   │   )
   └── ClaudeAgentOptions(system_prompt=…)    ← still a `str`
```

The static prefix sits at the **top** of the assembled string so the
server-side cache hash covers the largest stable portion.

## Direct-API callers (bypass the SDK)

For code that calls `anthropic.messages.create()` directly — e.g.
`apps/backend/insight_extractor.py`,
`apps/backend/spec_agents/critic.py` — use
`build_cached_system_blocks(...)` instead. It returns a list of
content blocks with an explicit `cache_control` marker on the **last
static block**:

```python
from core.cache import build_cached_system_blocks

system_blocks = build_cached_system_blocks(
    base_instructions=agent_intro,
    claude_md_content=load_claude_md(project_dir),
    project_context=json.dumps(context),
    ttl="ephemeral",   # 5 min default; use "1h" for long sessions (2× write cost)
)

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=4096,
    system=system_blocks,
    messages=[…],
)
```

## TTL choice

| TTL | Lifetime | Write cost | When to use |
| --- | --- | --- | --- |
| `"ephemeral"` (default) | 5 min | 1.25× input | Tight loops (coder, qa_fixer). |
| `"1h"` | 60 min | 2.00× input | Long planner sessions, multi-story BMad runs. |

TTL only applies to `build_cached_system_blocks` (direct-API). The
SDK path cannot set TTL; the server applies the default 5-min lifetime
on automatic caches.

## Reading the logs

`agents/session.py` extracts cache metrics from every `ResultMessage`
and emits one **aggregated** `INFO` line per agent session:

```
INFO  Prompt cache totals — read=8421 tok write=0 tok session=…
```

Per-turn lines are emitted at `DEBUG`; enable with
`LOG_LEVEL=DEBUG` to see them.

Two `WARNING` lines flag common pitfalls:

| Warning | Meaning |
| --- | --- |
| `Prompt-cache prefix below floor for model 'X' — got ~N tokens, need ≥M.` | Static prefix is shorter than the model's cache floor (1024 Sonnet / 4096 Opus & Haiku 4.5). Caching won't engage; consider adding more static context. |
| `Prompt-cache prefix changed for /path — cache will be cold on the next API call.` | The static prefix (CLAUDE.md + project context) changed between two `create_client()` calls in the same process. The next API call won't get a cache hit. |

## Verifying caching works end-to-end

Run the operator smoke test:

```bash
export ANTHROPIC_API_KEY=sk-...                # or CLAUDE_CODE_OAUTH_TOKEN
cd apps/backend
.venv/bin/python ../../scripts/smoke-test-prompt-cache.py
```

Expected:

```
Call 1: write=N read=0
Call 2: write=0 read=N
PASS — prompt cache active (read=N tokens on call 2)
```

If the second call shows `read=0`:

1. Check the static prefix is long enough (look for the floor warning).
2. Confirm nothing has rewritten CLAUDE.md between calls.
3. Make sure the model id is in `_MIN_CACHE_TOKENS` (`core/cache.py`).

## Files

| Path | Purpose |
| --- | --- |
| `apps/backend/core/cache.py` | Helpers + min-size / hash-change guards |
| `apps/backend/core/client.py` | Calls `build_cached_system_str` |
| `apps/backend/agents/session.py` | Logs aggregated cache usage |
| `tests/test_cache_blocks.py` | 27 unit tests (21 base + 6 guards) |
| `scripts/smoke-test-prompt-cache.py` | Operator runbook (real API calls) |

## References

- [Anthropic — Prompt Caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
- [Claude Agent SDK Python — types](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/types.py)
- Compliance audit: `guides/COMPLIANCE_AUDIT_2026-05.md` §9.3 item #2
