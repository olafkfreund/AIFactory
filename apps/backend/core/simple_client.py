"""
Simple Claude SDK Client Factory
================================

Factory for creating minimal Claude SDK clients for single-turn utility operations
like commit message generation, merge conflict resolution, and batch analysis.

These clients don't need full security configurations, MCP servers, or hooks.
Use `create_client()` from `core.client` for full agent sessions with security.

Example usage:
    from core.simple_client import create_simple_client

    # For commit message generation (text-only, no tools)
    client = create_simple_client(agent_type="commit_message")

    # For merge conflict resolution (text-only, no tools)
    client = create_simple_client(agent_type="merge_resolver")

    # For insights extraction (read tools only)
    client = create_simple_client(agent_type="insights", cwd=project_dir)
"""

from pathlib import Path

from agents.tools_pkg import get_agent_config, get_default_thinking_level
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from core.auth import get_sdk_env_vars, require_auth_token
from core.model_config import DEFAULT_UTILITY_MODEL
from phase_config import get_thinking_budget


def create_simple_client(
    agent_type: str = "merge_resolver",
    model: str = DEFAULT_UTILITY_MODEL,
    system_prompt: str | None = None,
    cwd: Path | None = None,
    max_turns: int = 1,
    max_thinking_tokens: int | None = None,
    # v1.2 / #207 — opt-in enforcement.  Same semantics as create_client():
    # no enforcement when org_id is absent (system tasks / CLI callers).
    org_id: str | None = None,
    user_id: str | None = None,
    allowed_models: list[str] | None = None,
) -> ClaudeSDKClient:
    """
    Create a minimal Claude SDK client for single-turn utility operations.

    This factory creates lightweight clients without MCP servers, security hooks,
    or full permission configurations. Use for text-only analysis tasks.

    Args:
        agent_type: Agent type from AGENT_CONFIGS. Determines available tools.
                   Common utility types:
                   - "merge_resolver" - Text-only merge conflict analysis
                   - "commit_message" - Text-only commit message generation
                   - "insights" - Read-only code insight extraction
                   - "batch_analysis" - Read-only batch issue analysis
                   - "batch_validation" - Read-only validation
        model: Claude model to use (defaults to Haiku for fast/cheap operations)
        system_prompt: Optional custom system prompt (for specialized tasks)
        cwd: Working directory for file operations (optional)
        max_turns: Maximum conversation turns (default: 1 for single-turn)
        max_thinking_tokens: Override thinking budget (None = use agent default from
                            AGENT_CONFIGS, converted using phase_config.THINKING_BUDGET_MAP)

    Returns:
        Configured ClaudeSDKClient for single-turn operations

    Raises:
        ValueError: If agent_type is not found in AGENT_CONFIGS
    """
    # Get authentication
    oauth_token = require_auth_token()
    import os

    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

    # Get environment variables for SDK
    sdk_env = get_sdk_env_vars()

    # Get agent configuration (raises ValueError if unknown type)
    config = get_agent_config(agent_type)

    # Get tools from config (no MCP tools for simple clients)
    allowed_tools = list(config.get("tools", []))

    # Determine thinking budget using the single source of truth (phase_config.py)
    # IMPORTANT: Haiku models do NOT support extended thinking - must be None/0
    is_haiku = "haiku" in model.lower()

    if is_haiku:
        # Haiku doesn't support thinking mode - always disable it
        max_thinking_tokens = None
    elif max_thinking_tokens is None:
        thinking_level = get_default_thinking_level(agent_type)
        max_thinking_tokens = get_thinking_budget(thinking_level)

    bare_client = ClaudeSDKClient(
        options=ClaudeAgentOptions(
            model=model,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            cwd=str(cwd.resolve()) if cwd else None,
            env=sdk_env,
            max_thinking_tokens=max_thinking_tokens,
            permission_mode="bypassPermissions",  # Bypass prompts for headless execution
        )
    )

    # v1.2 / #207 — reviewer finding #6: simple_client also needs enforcement
    # wiring so web-server call sites can opt in by passing org_id.
    # System tasks (no org_id) continue to receive the bare client.
    from core.enforcement import build_enforcement_context, wrap_client_if_enforced

    _effective_allowed_models = allowed_models if allowed_models is not None else ["*"]
    enforcement = build_enforcement_context(
        org_id=org_id,
        user_id=user_id,
        model=model,
        allowed_models=_effective_allowed_models,
    )
    enforcement.enforce_allowlist()
    # #1128 outbound PII scrub, innermost — see core.client.create_client.
    from core.outbound_scrub import wrap_client_outbound_scrub

    return wrap_client_if_enforced(wrap_client_outbound_scrub(bare_client), enforcement)
