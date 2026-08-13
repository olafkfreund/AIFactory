"""
Phase Configuration Module
===========================

Handles model and thinking level configuration for different execution phases.
Reads configuration from task_metadata.json and provides resolved model IDs.
"""

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Literal, TypedDict

from routing_policy import contract_route, policy_route

logger = logging.getLogger(__name__)

# Model shorthand to full model ID mapping
MODEL_ID_MAP: dict[str, str] = {
    "opus": "claude-opus-4-8",
    "opus-4.7": "claude-opus-4-7",  # previous flagship — kept for pinning
    "opus-1m": "claude-opus-4-6",  # legacy alias — kept for users who pinned 4.6 + 1M beta
    "opus-4.5": "claude-opus-4-5-20251101",
    "sonnet": "claude-sonnet-5",  # current Sonnet (near-Opus coding, 1M ctx)
    "sonnet-4.6": "claude-sonnet-4-6",  # previous Sonnet — kept for pinning
    "haiku": "claude-haiku-4-5-20251001",
}

# Model shorthand to required SDK beta headers
# Maps model shorthands that need special beta flags (e.g., 1M context window)
MODEL_BETAS_MAP: dict[str, list[str]] = {
    "opus-1m": ["context-1m-2025-08-07"],
}

# Thinking level to budget tokens mapping (None = no extended thinking)
# Values must match frontend THINKING_BUDGET_MAP
THINKING_BUDGET_MAP: dict[str, int | None] = {
    "none": None,
    "low": 1024,
    "medium": 4096,  # Moderate analysis
    "high": 16384,  # Deep thinking for QA review
}

# Effort level mapping for adaptive thinking models (e.g., Opus 4.6)
# These models support CLAUDE_CODE_EFFORT_LEVEL env var for effort-based routing
EFFORT_LEVEL_MAP: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
}

# Models that support adaptive thinking via effort level (env var)
# These models get both max_thinking_tokens AND effort_level
ADAPTIVE_THINKING_MODELS: set[str] = {
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
}

# Spec runner phase-specific thinking levels
# Heavy phases use max for deep analysis
# Light phases use medium after compaction
SPEC_PHASE_THINKING_LEVELS: dict[str, str] = {
    # Heavy phases - high (discovery, spec creation, self-critique)
    "discovery": "high",
    "spec_writing": "high",
    "self_critique": "high",
    # Light phases - medium (after first invocation with compaction)
    "requirements": "medium",
    "research": "medium",
    "context": "medium",
    "planning": "medium",
    "validation": "medium",
    "quick_spec": "medium",
    "historical_context": "medium",
    "complexity_assessment": "medium",
}

# Default phase configuration (fallback, matches 'Balanced' profile)
DEFAULT_PHASE_MODELS: dict[str, str] = {
    # Default to Opus 4.8 (current flagship) for all stages — highest-capability
    # coding/agentic model. Override per-task via the contract `phase_models`.
    "spec": "opus",
    "planning": "opus",
    "coding": "opus",
    "qa": "opus",
    "qa_fixer": "opus",
}

DEFAULT_PHASE_THINKING: dict[str, str] = {
    "spec": "medium",
    "planning": "high",
    "coding": "medium",
    "qa": "high",
    "qa_fixer": "low",
}


class PhaseModelConfig(TypedDict, total=False):
    spec: str
    planning: str
    coding: str
    qa: str
    qa_fixer: str


class PhaseThinkingConfig(TypedDict, total=False):
    spec: str
    planning: str
    coding: str
    qa: str
    qa_fixer: str


class TaskMetadataConfig(TypedDict, total=False):
    """Structure of model-related fields in task_metadata.json"""

    isAutoProfile: bool
    phaseModels: PhaseModelConfig
    phaseThinking: PhaseThinkingConfig
    model: str
    thinkingLevel: str
    fastMode: bool


Phase = Literal["spec", "planning", "coding", "qa", "qa_fixer"]


def resolve_model_id(model: str) -> str:
    """
    Resolve a model shorthand (haiku, sonnet, opus) to a full model ID.
    If the model is already a full ID, return it unchanged.

    Priority:
    1. Environment variable override (from API Profile)
    2. Hardcoded MODEL_ID_MAP
    3. Pass through unchanged (assume full model ID)

    Args:
        model: Model shorthand or full ID

    Returns:
        Full Claude model ID
    """
    # Check for environment variable override (from API Profile custom model mappings)
    if model in MODEL_ID_MAP:
        env_var_map = {
            "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "opus-1m": "ANTHROPIC_DEFAULT_OPUS_MODEL",
            # opus-4.5 intentionally omitted — always resolves to its hardcoded
            # model ID (claude-opus-4-5-20251101) regardless of env var overrides.
        }
        env_var = env_var_map.get(model)
        if env_var:
            env_value = os.environ.get(env_var)
            if env_value:
                return env_value

        # Fall back to hardcoded mapping
        return MODEL_ID_MAP[model]

    # Already a full model ID or unknown shorthand
    return model


def get_model_betas(model_short: str) -> list[str]:
    """
    Get required SDK beta headers for a model shorthand.

    Some model configurations (e.g., opus-1m for 1M context window) require
    passing beta headers to the Claude Agent SDK.

    Args:
        model_short: Model shorthand (e.g., 'opus', 'opus-1m', 'sonnet')

    Returns:
        List of beta header strings, or empty list if none required
    """
    return MODEL_BETAS_MAP.get(model_short, [])


def get_thinking_budget(thinking_level: str) -> int | None:
    """
    Get the thinking budget for a thinking level.

    Args:
        thinking_level: Thinking level (none, low, medium, high, max)

    Returns:
        Token budget or None for no extended thinking
    """
    if thinking_level not in THINKING_BUDGET_MAP:
        valid_levels = ", ".join(THINKING_BUDGET_MAP.keys())
        logger.warning(
            "Invalid thinking_level '%s'. Valid values: %s. Defaulting to 'medium'.",
            thinking_level,
            valid_levels,
        )
        return THINKING_BUDGET_MAP["medium"]

    return THINKING_BUDGET_MAP[thinking_level]


def is_adaptive_model(model_id: str) -> bool:
    """
    Check if a model supports adaptive thinking via effort level.

    Adaptive models support the CLAUDE_CODE_EFFORT_LEVEL environment variable
    for effort-based routing in addition to max_thinking_tokens.

    Args:
        model_id: Full model ID (e.g., 'claude-opus-4-6')

    Returns:
        True if the model supports adaptive thinking
    """
    return model_id in ADAPTIVE_THINKING_MODELS


# Issue #7 — SDK-native adaptive + interleaved thinking
# The constants and helpers below are the entry point for callers that want
# to use the Claude Agent SDK's `thinking` parameter (post-Jan-2026 SDK).
# is_adaptive_model() above stays in use for the legacy CLAUDE_CODE_EFFORT_LEVEL
# path; the gate here is narrower: only Opus 4.7 routes to the SDK-native
# {"type": "adaptive"} shape — Opus 4.6 stays on the effort-level path.
_OPUS_47_ID: str = "claude-opus-4-7"
# Opus 4.8 — current flagship; same adaptive/interleaved-thinking support.
_OPUS_48_ID: str = "claude-opus-4-8"

INTERLEAVED_THINKING_AGENT_TYPES: frozenset[str] = frozenset({"planner", "coder"})
INTERLEAVED_THINKING_BETA: str = "interleaved-thinking-2025-05-14"


def thinking_config_for(
    model_id: str,
    thinking_level: str,
    explicit_budget: int | None = None,
) -> dict | None:
    """
    Build the SDK `thinking` config dict for ClaudeAgentOptions.thinking,
    or None when the caller should fall back to the legacy
    `max_thinking_tokens` path.

    Precedence:
      1. explicit_budget > 0  → {"type": "enabled", "budget_tokens": N}
         honoured even on Opus 4.7 so operator-tuned budgets are preserved.
      2. Opus 4.7 with thinking_level != "none" → {"type": "adaptive"}
         (Opus 4.7 deprecated manual budget in favour of model-controlled.)
      3. anything else → None (caller uses the legacy max_thinking_tokens).

    Args:
        model_id: Full model ID (e.g. 'claude-opus-4-7').
        thinking_level: Level string ("none", "low", "medium", "high").
        explicit_budget: Optional caller-specified token budget. When > 0,
            overrides adaptive even on Opus 4.7.

    Returns:
        Thinking config dict or None.
    """
    if explicit_budget is not None and explicit_budget > 0:
        return {"type": "enabled", "budget_tokens": explicit_budget}
    if model_id in (_OPUS_47_ID, _OPUS_48_ID) and thinking_level != "none":
        return {"type": "adaptive"}
    return None


def interleaved_thinking_betas_for(
    model_id: str,
    agent_type: str,
) -> list[str]:
    """
    Return [INTERLEAVED_THINKING_BETA] when the (model, agent_type) pair
    benefits from interleaved-thinking-2025-05-14, else an empty list.

    Today only Opus 4.7 supports this beta, and only planner + coder agents
    actually use the mid-tool-call reasoning. QA, fixer, and spec agents
    receive an empty list.

    Note: returned list is always a fresh allocation — safe for the caller
    to mutate (e.g. extend with context-window betas).

    Args:
        model_id: Full model ID.
        agent_type: Agent type ("planner", "coder", "qa_reviewer", …).

    Returns:
        List of beta header strings — either [INTERLEAVED_THINKING_BETA] or [].
    """
    if (
        model_id in (_OPUS_47_ID, _OPUS_48_ID)
        and agent_type in INTERLEAVED_THINKING_AGENT_TYPES
    ):
        return [INTERLEAVED_THINKING_BETA]
    return []


def get_thinking_kwargs_for_model(model_id: str, thinking_level: str) -> dict:
    """
    Get thinking-related kwargs for create_client() based on model type.

    For adaptive models (Opus 4.6): returns both max_thinking_tokens and effort_level.
    For other models (Sonnet, Haiku): returns only max_thinking_tokens.

    Args:
        model_id: Full model ID (e.g., 'claude-opus-4-6')
        thinking_level: Thinking level string (none, low, medium, high, max)

    Returns:
        Dict with 'max_thinking_tokens' and optionally 'effort_level'
    """
    kwargs: dict = {"max_thinking_tokens": get_thinking_budget(thinking_level)}
    if is_adaptive_model(model_id):
        kwargs["effort_level"] = EFFORT_LEVEL_MAP.get(thinking_level, "medium")
    return kwargs


def load_task_metadata(spec_dir: Path) -> TaskMetadataConfig | None:
    """
    Load task metadata from the spec directory.

    Checks two locations in order:
    1. task_metadata.json (preferred, written by web UI)
    2. requirements.json["metadata"] (fallback for backward compatibility)

    Args:
        spec_dir: Path to the spec directory

    Returns:
        Parsed task metadata or None if not found
    """
    # First, try task_metadata.json (preferred)
    metadata_path = spec_dir / "task_metadata.json"
    if metadata_path.exists():
        # ponytail: corrupt/unreadable task_metadata.json falls through to the
        # requirements.json fallback below.
        with contextlib.suppress(json.JSONDecodeError, OSError):
            with open(metadata_path) as f:
                return json.load(f)

    # Fallback: check requirements.json["metadata"]
    requirements_path = spec_dir / "requirements.json"
    if requirements_path.exists():
        # ponytail: corrupt/unreadable requirements.json just means no metadata found
        with contextlib.suppress(json.JSONDecodeError, OSError):
            with open(requirements_path) as f:
                requirements = json.load(f)
                if "metadata" in requirements and isinstance(
                    requirements["metadata"], dict
                ):
                    return requirements["metadata"]

    return None


def _difficulty_tier(metadata: TaskMetadataConfig | None) -> str | None:
    """The RFC-0011 difficulty tier from task metadata, for the routing floor.

    ``difficultyTier`` is carried into task_metadata from the tier path (#825
    follow-up) but is not a declared TaskMetadataConfig key, so ``.get`` returns
    ``object``; narrow it to ``str | None`` for ``policy_route``'s floor arg.
    """
    value = metadata.get("difficultyTier") if metadata else None
    return value if isinstance(value, str) else None


def get_phase_model(
    spec_dir: Path,
    phase: Phase,
    cli_model: str | None = None,
) -> str:
    """Resolve a stage's model, then assert it against the approved-model registry.

    Thin wrapper over :func:`_resolve_phase_model` (which owns the full RFC-0014
    precedence). The registry assertion is advisory by default (warn), a no-op
    when ``AIFACTORY_MODEL_REGISTRY_ENFORCE=off``, and only blocks in ``deny``
    mode — so with the default flag this returns exactly what resolution picked.
    """
    model = _resolve_phase_model(spec_dir, phase, cli_model)
    # Lazy import avoids a module-load cycle (model_registry imports this module).
    from model_registry import enforce_model_registry  # noqa: PLC0415

    enforce_model_registry(model, phase)
    return model


def _resolve_phase_model(
    spec_dir: Path,
    phase: Phase,
    cli_model: str | None = None,
) -> str:
    """
    Get the resolved model ID for a specific execution phase.

    Priority (RFC-0014 #803: pinned > per-task override > policy tier > default):
    1. Contract pinned model (task_metadata.json ``pinnedModel``, carried from
       the Task Contract's ``execution.routing.pinned_model``)
    2. Phase-specific config from task_metadata.json (if auto profile) — wins
       over CLI default because the auto profile is the user's explicit
       per-phase choice (e.g. Claude plans, Ollama codes).
    3. CLI argument (if provided)
    4. Single model from task_metadata.json (if not auto profile)
    5. Routing-policy tier (AIFACTORY_ROUTING_POLICY; absent = no-op)
    6. Default phase configuration

    Args:
        spec_dir: Path to the spec directory
        phase: Execution phase (spec, planning, coding, qa)
        cli_model: Model from CLI argument (optional)

    Returns:
        Resolved full model ID
    """
    # Load task metadata first — the auto profile's per-phase choice is the
    # user's most specific intent and must win over the run.py CLI default
    # model. (Without this, e.g. coding always falls back to the CLI's
    # 'sonnet' default and never routes to Ollama/qwen3 even when the user
    # selected ollama:qwen3:14b for the coding phase.)
    metadata = load_task_metadata(spec_dir)

    # Contract-pinned model wins over everything (RFC-0014 routing block).
    if metadata and metadata.get("pinnedModel"):
        return resolve_model_id(metadata["pinnedModel"])

    if metadata and metadata.get("isAutoProfile") and metadata.get("phaseModels"):
        phase_models = metadata["phaseModels"]
        model = phase_models.get(phase, DEFAULT_PHASE_MODELS[phase])
        return resolve_model_id(model)

    # CLI argument takes precedence over non-auto metadata
    if cli_model:
        return resolve_model_id(cli_model)

    # RFC-0014 routing (contract requested/overrides, then the local
    # AIFACTORY_ROUTING_POLICY) is consulted BEFORE the tier's static
    # metadata.model (#825). The RFC-0011 tier assigns a per-tier model into
    # metadata.model as a DEFAULT; a routing policy is the operator's per-stage
    # override and wins over that default — while staying below pinnedModel,
    # auto-profile phaseModels, and an explicit cli_model (handled above). Both
    # return None when nothing routes the stage, so with routing OFF this is
    # byte-identical: metadata.model below drives exactly as before.
    contracted = contract_route(phase, metadata)
    if contracted is not None:
        return resolve_model_id(contracted[0])
    routed = policy_route(phase, _difficulty_tier(metadata))
    if routed is not None:
        return resolve_model_id(routed[0])

    # Non-auto profile: the tier's static model (RFC-0011) or a user's saved
    # single model from task_metadata.json — the default when nothing routes.
    if metadata and metadata.get("model"):
        return resolve_model_id(metadata["model"])

    # Fall back to default phase configuration
    return resolve_model_id(DEFAULT_PHASE_MODELS[phase])


def get_phase_model_betas(
    spec_dir: Path,
    phase: Phase,
    cli_model: str | None = None,
) -> list[str]:
    """
    Get required SDK beta headers for the model selected for a specific phase.

    Uses the same priority logic as get_phase_model() to determine which model
    shorthand is selected, then looks up any required beta headers.

    Args:
        spec_dir: Path to the spec directory
        phase: Execution phase (spec, planning, coding, qa)
        cli_model: Model from CLI argument (optional)

    Returns:
        List of beta header strings, or empty list if none required
    """
    # Same precedence as get_phase_model: auto profile metadata wins over CLI.
    metadata = load_task_metadata(spec_dir)

    if metadata and metadata.get("pinnedModel"):
        return get_model_betas(metadata["pinnedModel"])

    if metadata and metadata.get("isAutoProfile") and metadata.get("phaseModels"):
        phase_models = metadata["phaseModels"]
        model_short = phase_models.get(phase, DEFAULT_PHASE_MODELS[phase])
        return get_model_betas(model_short)

    if cli_model:
        return get_model_betas(cli_model)

    # Routing before the tier's static metadata.model (#825), mirroring
    # get_phase_model so a routed model gets the right beta headers.
    contracted = contract_route(phase, metadata)
    if contracted is not None:
        return get_model_betas(contracted[0])
    routed = policy_route(phase, _difficulty_tier(metadata))
    if routed is not None:
        return get_model_betas(routed[0])

    if metadata and metadata.get("model"):
        return get_model_betas(metadata["model"])

    return get_model_betas(DEFAULT_PHASE_MODELS[phase])


def get_phase_thinking(
    spec_dir: Path,
    phase: Phase,
    cli_thinking: str | None = None,
) -> str:
    """
    Get the thinking level for a specific execution phase.

    Priority:
    1. CLI argument (if provided)
    2. Phase-specific config from task_metadata.json (if auto profile)
    3. Single thinking level from task_metadata.json (if not auto profile)
    4. Default phase configuration

    Args:
        spec_dir: Path to the spec directory
        phase: Execution phase (spec, planning, coding, qa)
        cli_thinking: Thinking level from CLI argument (optional)

    Returns:
        Thinking level string
    """
    # Same precedence as get_phase_model: auto profile metadata wins over CLI.
    metadata = load_task_metadata(spec_dir)

    if metadata and metadata.get("isAutoProfile") and metadata.get("phaseThinking"):
        phase_thinking = metadata["phaseThinking"]
        return phase_thinking.get(phase, DEFAULT_PHASE_THINKING[phase])

    if cli_thinking:
        return cli_thinking

    if metadata and metadata.get("thinkingLevel"):
        return metadata["thinkingLevel"]

    return DEFAULT_PHASE_THINKING[phase]


def get_phase_thinking_budget(
    spec_dir: Path,
    phase: Phase,
    cli_thinking: str | None = None,
) -> int | None:
    """
    Get the thinking budget tokens for a specific execution phase.

    Args:
        spec_dir: Path to the spec directory
        phase: Execution phase (spec, planning, coding, qa)
        cli_thinking: Thinking level from CLI argument (optional)

    Returns:
        Token budget or None for no extended thinking
    """
    thinking_level = get_phase_thinking(spec_dir, phase, cli_thinking)
    return get_thinking_budget(thinking_level)


def get_phase_config(
    spec_dir: Path,
    phase: Phase,
    cli_model: str | None = None,
    cli_thinking: str | None = None,
) -> tuple[str, str, int | None]:
    """
    Get the full configuration for a specific execution phase.

    Args:
        spec_dir: Path to the spec directory
        phase: Execution phase (spec, planning, coding, qa)
        cli_model: Model from CLI argument (optional)
        cli_thinking: Thinking level from CLI argument (optional)

    Returns:
        Tuple of (model_id, thinking_level, thinking_budget)
    """
    model_id = get_phase_model(spec_dir, phase, cli_model)
    thinking_level = get_phase_thinking(spec_dir, phase, cli_thinking)
    thinking_budget = get_thinking_budget(thinking_level)

    return model_id, thinking_level, thinking_budget


def get_phase_client_thinking_kwargs(
    spec_dir: Path,
    phase: Phase,
    phase_model: str,
    cli_thinking: str | None = None,
) -> dict:
    """
    Get thinking kwargs for create_client() for a specific execution phase.

    Combines get_phase_thinking() and get_thinking_kwargs_for_model() to produce
    the correct kwargs dict based on phase config and model capabilities.

    Args:
        spec_dir: Path to the spec directory
        phase: Execution phase (spec, planning, coding, qa)
        phase_model: Resolved full model ID for this phase
        cli_thinking: Thinking level from CLI argument (optional)

    Returns:
        Dict with 'max_thinking_tokens' and optionally 'effort_level'
    """
    thinking_level = get_phase_thinking(spec_dir, phase, cli_thinking)
    return get_thinking_kwargs_for_model(phase_model, thinking_level)


def get_fast_mode(spec_dir: Path) -> bool:
    """
    Check if Fast Mode is enabled for this task.

    Fast Mode provides faster Opus 4.6 output at higher cost.
    Reads the fastMode flag from task_metadata.json.

    Args:
        spec_dir: Path to the spec directory

    Returns:
        True if Fast Mode is enabled, False otherwise
    """
    metadata = load_task_metadata(spec_dir)
    if metadata:
        enabled = bool(metadata.get("fastMode", False))
        if enabled:
            logger.info(
                "[Fast Mode] ENABLED — read fastMode=true from task_metadata.json"
            )
        else:
            logger.info("[Fast Mode] disabled — fastMode not set in task_metadata.json")
        return enabled
    logger.info("[Fast Mode] disabled — no task_metadata.json found")
    return False


def _is_openai_o_series(m: str) -> bool:
    """Return True for OpenAI o-series model IDs (o1, o3, o4-mini, …)."""
    import re

    return bool(re.match(r"^o\d", m))


def infer_provider_from_model(model: str) -> str:
    """
    Infer the LLM provider from the model ID.  Works for any phase.

    The provider is determined by the model string itself, so a separate
    provider setting is no longer needed.

    studio:* prefix -> 'openai-compatible' (Google AI Studio OpenAI-compatible endpoint)
    ollama:* prefix -> 'ollama'
    opencode:* prefix -> 'opencode' (OpenCode CLI runtime; model form opencode:<provider/model>)
    openai:* or openai-compatible:* prefix -> 'openai-compatible'
    bedrock/* prefix -> 'openai-compatible' (LiteLLM-routed; see note below)
    vertex_ai/* prefix -> 'openai-compatible' (LiteLLM-routed; see note below)
    Claude shorthands (opus, sonnet, haiku) or claude-* IDs -> 'claude'
    gpt-* or *codex* IDs (incl. bare 'codex' / 'codex:default' = account
        default, no explicit model) or bare 'default' -> 'codex'
    antigravity / antigravity-* / gemini-* IDs -> 'antigravity'
        (gemini-* is kept for back-compat — the Antigravity CLI still serves
        Google's gemini-* model strings)
    Otherwise -> check QA_LLM_PROVIDER env var, then default 'claude'

    Note on bedrock/* and vertex_ai/* prefixes:
        These are LiteLLM-native model prefixes. AIFactory does not talk to
        Bedrock or Vertex directly; instead it routes through the LiteLLM
        gateway (``LITELLM_GATEWAY_URL``), which translates the OpenAI-format
        request into the appropriate cloud API call. The
        ``openai_compatible`` provider handles the HTTP leg from AIFactory to
        LiteLLM. ``get_provider_extra_kwargs`` enforces that
        ``LITELLM_GATEWAY_URL`` is configured before these prefixes are used.
        See docs/docs/concepts/cloud-llm-routing.md.

    Args:
        model: Model shorthand or full model ID

    Returns:
        Provider name string (e.g., "claude", "codex", "antigravity",
        "ollama", "openai-compatible")
    """
    m = model.strip().lower()

    # Explicit prefix: "github-models/<publisher>/<model>"
    # Checked first so "github-models/openai/gpt-4.1" doesn't fall through to
    # the gpt-* → codex rule below.
    if m.startswith("github-models/"):
        return "github-models"

    # Explicit prefix: "studio:model-name"
    if m.startswith("studio:"):
        return "openai-compatible"

    # Explicit prefix: "ollama:model-name"
    if m.startswith("ollama:"):
        return "ollama"

    # Explicit prefix: "copilot:model-name" — GitHub Copilot CLI.  Checked
    # before the claude-*/gpt-* rules below because Copilot's own backend
    # names (claude-sonnet-4.5, gpt-5) would otherwise route to claude/codex.
    if m.startswith("copilot:"):
        return "copilot"

    # Explicit prefix: "opencode:provider/model" — OpenCode CLI runtime.
    # Checked before the claude-*/gpt-*/codex rules below because OpenCode's
    # native model strings ("anthropic/claude-...", "openai/gpt-4o") and the
    # literal substring "opencode" (contains "codex"? no, but "code") could
    # otherwise be mis-routed.  The "codex" substring rule in particular would
    # never match "opencode", but routing the explicit prefix first keeps the
    # provider selection unambiguous.
    if m.startswith("opencode:"):
        return "opencode"

    # Explicit prefix for OpenAI-compatible endpoints (LM Studio, vLLM,
    # OpenRouter, Together, Groq, LocalAI, ...).  Connection details come
    # from env vars OPENAI_COMPATIBLE_BASE_URL / OPENAI_COMPATIBLE_API_KEY
    # or, in a later integration, from the user's saved llm_endpoints config.
    if m.startswith("openai:") or m.startswith("openai-compatible:"):
        return "openai-compatible"

    # LiteLLM-routed cloud backends.  Both prefixes use LiteLLM's native
    # model-name convention (forward-slash separator).  The openai_compatible
    # provider sends the full model string (including prefix) to the LiteLLM
    # gateway, which resolves the backend.  A validation guard in
    # get_provider_extra_kwargs raises a clear error when LITELLM_GATEWAY_URL
    # is not set, preventing a cryptic 404 from api.openai.com.
    if m.startswith("bedrock/") or m.startswith("vertex_ai/"):
        return "openai-compatible"

    # Claude models: known shorthands or full claude-* IDs
    if m in MODEL_ID_MAP or m.startswith("claude-"):
        return "claude"

    # OpenAI Codex models.  A bare "codex" (or "codex:default" / "codex-default"
    # / "default") routes here with NO concrete model id — the provider then
    # sends no `model` field to the MCP request and codex uses its account
    # default.  This is the model string ChatGPT-account users should pick,
    # since a ChatGPT-account login rejects every explicit model.  Concrete ids
    # ("gpt-5.3-codex", "o4-mini", ...) also route here and are forwarded as-is
    # for API-key accounts.  (#293)
    # o-series OpenAI models: o1, o1-mini, o1-preview, o3, o3-mini, o4-mini, …
    if m == "default" or m.startswith("gpt-") or "codex" in m or _is_openai_o_series(m):
        return "codex"

    # Antigravity CLI (Google).  Canonical IDs are "antigravity" /
    # "antigravity-*".  Legacy "gemini-*" model strings still route here
    # (back-compat) — the Antigravity CLI serves Google's gemini-* models.
    if (
        m == "antigravity"
        or m.startswith("antigravity-")
        or m.startswith("antigravity:")
    ):
        return "antigravity"
    if m.startswith("gemini"):
        return "antigravity"

    # Env fallback for unknown models (e.g., ollama custom models)
    env_provider = os.environ.get("QA_LLM_PROVIDER", "").strip()
    return env_provider or "claude"


# Backward compatibility alias
infer_qa_provider_from_model = infer_provider_from_model


def strip_provider_prefix(model: str) -> str:
    """Strip a leading ``ollama:``, ``openai:``, ``openai-compatible:``, or ``studio:`` prefix.

    The factory and providers expect a bare model name.  When a user picks
    ``openai-compatible:gpt-4o-mini``, the provider only needs ``gpt-4o-mini``.
    """
    for prefix in (
        "openai-compatible:",
        "openai:",
        "ollama:",
        "studio:",
        "copilot:",
        "opencode:",
    ):
        if model.lower().startswith(prefix):
            return model[len(prefix) :]
    # github-models uses a slash separator: "github-models/<publisher>/<model>"
    if model.lower().startswith("github-models/"):
        return model[len("github-models/") :]
    return model


_LLM_ENDPOINTS_DB_PATH = Path.home() / ".aifactory" / "data.db"


def _load_openai_endpoint_by_label(label: str) -> dict | None:
    """Look up an llm_endpoint row by label.  Returns None if not found."""
    import sqlite3

    if not _LLM_ENDPOINTS_DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(_LLM_ENDPOINTS_DB_PATH))
        row = conn.execute(
            "SELECT base_url, api_key, default_model FROM llm_endpoints "
            "WHERE label = ? LIMIT 1",
            (label,),
        ).fetchone()
        conn.close()
        if row:
            return {"base_url": row[0], "api_key": row[1], "default_model": row[2]}
    except sqlite3.Error as exc:
        logger.warning("llm_endpoints lookup by label failed, treating as unconfigured: %s", exc)
    return None


def _load_first_openai_endpoint() -> dict | None:
    """Return the oldest configured llm_endpoint — for single-endpoint users."""
    import sqlite3

    if not _LLM_ENDPOINTS_DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(_LLM_ENDPOINTS_DB_PATH))
        row = conn.execute(
            "SELECT base_url, api_key, default_model FROM llm_endpoints "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            return {"base_url": row[0], "api_key": row[1], "default_model": row[2]}
    except sqlite3.Error as exc:
        logger.warning("llm_endpoints lookup (first) failed, treating as unconfigured: %s", exc)
    return None


def get_provider_extra_kwargs(provider_name: str, model: str) -> dict:
    """Return additional kwargs to pass to ``get_provider`` for non-trivial providers.

    For ``openai-compatible`` the provider needs ``base_url`` and ``api_key``
    on top of the model name.  Resolution order:

    1. ``bedrock/<model>`` or ``vertex_ai/<model>`` — LiteLLM-routed cloud
       backends.  LITELLM_GATEWAY_URL MUST be set; raises ``ValueError`` with
       an operator-actionable message if not.  The full model string (including
       prefix) is forwarded to the gateway so LiteLLM can resolve the backend.
    2. ``studio:<model>`` — Google AI Studio native OpenAI-compatible endpoint.
    3. ``openai:<label>:<model>`` — look up endpoint by label, use that model
       (or the endpoint's default_model if no model specified).
    4. ``openai:<model>`` (single colon) — use the first/only configured
       endpoint with the given model name.
    5. No DB row at all — fall back to env vars
       (``OPENAI_COMPATIBLE_BASE_URL`` / ``OPENAI_COMPATIBLE_API_KEY`` /
       ``OPENAI_API_KEY``) for power users without the UI.

    Args:
        provider_name: Canonical provider name from ``infer_provider_from_model``.
        model: The original (possibly prefixed) model string.

    Returns:
        Dict of extra kwargs to spread into the ``get_provider`` call.

    Raises:
        ValueError: When a ``bedrock/*`` or ``vertex_ai/*`` model is requested
            but ``LITELLM_GATEWAY_URL`` is not configured.
    """
    if provider_name != "openai-compatible":
        return {}

    m_lower = model.strip().lower()

    # LiteLLM-routed cloud backends require the gateway — raise early with a
    # clear message rather than producing a cryptic 404 from api.openai.com.
    if m_lower.startswith("bedrock/") or m_lower.startswith("vertex_ai/"):
        from providers._gateway import is_gateway_enabled

        if not is_gateway_enabled():
            raise ValueError(
                "Bedrock / Vertex models require LITELLM_GATEWAY_URL to be "
                "configured (LiteLLM is the routing layer for cloud-provider "
                "models). Set LITELLM_GATEWAY_URL to your LiteLLM proxy "
                "address (e.g. http://litellm:4000). "
                "See docs/concepts/cloud-llm-routing.md."
            )
        # Forward the full model string (including cloud prefix) so LiteLLM
        # can route to the correct backend.  base_url is resolved by the
        # openai_compatible provider via _gateway.resolve_base_url().
        gateway_url = os.environ.get("LITELLM_GATEWAY_URL", "").strip()
        # Data-plane auth: a production LiteLLM proxy is started with a master
        # key and/or per-tenant virtual keys, so it rejects unauthenticated
        # /v1/chat/completions calls with 401.  Without a Bearer token here the
        # provider sends no Authorization header and the whole Bedrock/Vertex
        # path 401s — this was the broken LiteLLM-routed path.  Read the
        # deployment-wide gateway key from env (LITELLM_API_KEY, matching the
        # verify-routing curl in docs/concepts/cloud-llm-routing.md), falling
        # back to the generic OpenAI-compatible key env vars so existing
        # single-key deployments keep working.
        # TODO(Epic #35 #38 PR-2b): inject the caller's *per-org* LiteLLM
        # virtual key (sk-... minted via POST /key/generate) instead of a
        # deployment-wide key once org context is plumbed through this call —
        # that enables per-tenant budget/allowlist enforcement.  Requires a
        # live LiteLLM proxy + minted virtual key to validate end-to-end.
        gateway_key = (
            os.environ.get("LITELLM_API_KEY", "").strip()
            or os.environ.get("OPENAI_COMPATIBLE_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
            or None
        )
        return {
            "model": model.strip(),
            "base_url": gateway_url,
            "api_key": gateway_key,
        }

    stripped = strip_provider_prefix(model).strip()

    # Special handling for Google AI Studio
    if model.lower().startswith("studio:"):
        base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
        api_key = (
            os.environ.get("GOOGLE_API_KEY", "").strip()
            or os.environ.get("GEMINI_API_KEY", "").strip()
            or None
        )
        return {
            "model": stripped or "gemini-2.5-flash",
            "base_url": base_url,
            "api_key": api_key,
        }

    # 1) "<label>:<model>" — disambiguate among multiple endpoints
    if ":" in stripped:
        label_part, model_part = stripped.split(":", 1)
        endpoint = _load_openai_endpoint_by_label(label_part.strip())
        if endpoint:
            return {
                "model": model_part.strip() or endpoint["default_model"],
                "base_url": endpoint["base_url"],
                "api_key": endpoint["api_key"],
            }

    # 2) Just a model name — use the first/only saved endpoint
    endpoint = _load_first_openai_endpoint()
    if endpoint:
        return {
            "model": stripped
            if stripped and stripped != "default"
            else endpoint["default_model"],
            "base_url": endpoint["base_url"],
            "api_key": endpoint["api_key"],
        }

    # 3) No DB row at all — env-var fallback for power users / CLI usage
    base_url = (
        os.environ.get("OPENAI_COMPATIBLE_BASE_URL", "").strip()
        or "https://api.openai.com"
    )
    api_key = (
        os.environ.get("OPENAI_COMPATIBLE_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
        or None
    )
    return {
        "model": stripped or "gpt-4o-mini",
        "base_url": base_url,
        "api_key": api_key,
    }


# Provider capabilities: which providers support agentic phases (file ops, code execution)
PROVIDER_AGENTIC_SUPPORT = {
    "claude",
    "codex",
    "antigravity",
    "ollama",
    "openai-compatible",
    "github-models",
}


def validate_phase_provider(phase: Phase, model: str) -> tuple[bool, str]:
    """
    Validate that the model/provider is compatible with the phase.

    Agentic phases (spec, planning, coding, qa_fixer) require providers that
    support file operations and code execution.  Providers in
    PROVIDER_AGENTIC_SUPPORT can handle these phases.

    Args:
        phase: Execution phase (spec, planning, coding, qa, qa_fixer)
        model: Model shorthand or full model ID

    Returns:
        Tuple of (is_valid, error_message).  error_message is empty when valid.
    """
    provider = infer_provider_from_model(model)
    agentic_phases: set[str] = {"spec", "planning", "coding", "qa_fixer"}
    if phase in agentic_phases and provider not in PROVIDER_AGENTIC_SUPPORT:
        return False, (
            f"Provider '{provider}' doesn't support agentic mode needed for "
            f"{phase} phase. Supported: {sorted(PROVIDER_AGENTIC_SUPPORT)}"
        )
    return True, ""


def get_spec_phase_thinking_budget(phase_name: str) -> int | None:
    """
    Get the thinking budget for a specific spec runner phase.

    This maps granular spec phases (discovery, spec_writing, etc.) to their
    appropriate thinking budgets based on SPEC_PHASE_THINKING_LEVELS.

    Args:
        phase_name: Name of the spec phase (e.g., 'discovery', 'spec_writing')

    Returns:
        Token budget for extended thinking, or None for no extended thinking
    """
    thinking_level = SPEC_PHASE_THINKING_LEVELS.get(phase_name, "medium")
    return get_thinking_budget(thinking_level)
