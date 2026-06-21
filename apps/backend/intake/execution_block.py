"""Build a tier-driven RFC-0002 ``execution`` block from an RFC-0011 tier.

Pure and dependency-light: given a :class:`pfactory.tiers.Tier` (and optional
``change_mode``), produce the ``execution`` dict that the trusted-plan fast path
and ``apply_execution_profile`` already understand
(model / skip_planning / review_tier / complexity / autonomy_tier).

Tier policy (RFC-0011 routing table)::

    low    -> model=resolve_low_tier_model() (ollama->haiku), skip_planning,
              review_tier=auto,     complexity=simple,   autonomy_tier=low
    medium -> model=sonnet,         skip_planning (lite), review_tier=async,
              complexity=standard, autonomy_tier=medium
    hard   -> model=opus,           full planning,        review_tier=blocking,
              complexity=complex,  autonomy_tier=hard

``change_mode == "migration"`` (rewrite) is resolved to ``hard`` upstream by
``tier_for``; this builder simply honours whatever tier it is handed.
"""

from __future__ import annotations

from collections.abc import Callable

from pfactory.tiers import Tier, resolve_low_tier_model

__all__ = ["build_execution_block"]

# review_tier values mirror review_tier.py (AUTO/ASYNC/BLOCKING).
_REVIEW_TIER: dict[Tier, str] = {
    Tier.LOW: "auto",
    Tier.MEDIUM: "async",
    Tier.HARD: "blocking",
}

# Coarse complexity hint consumed by phase_config / the executor.
_COMPLEXITY: dict[Tier, str] = {
    Tier.LOW: "simple",
    Tier.MEDIUM: "standard",
    Tier.HARD: "complex",
}

# Static model per tier (low resolves dynamically via the Ollama probe).
_STATIC_MODEL: dict[Tier, str] = {
    Tier.MEDIUM: "sonnet",
    Tier.HARD: "opus",
}

# Low/medium skip AIFactory's own planning (low = full skip, medium = lite);
# hard runs the full decompose. Only hard sets skip_planning=False.
_SKIP_PLANNING: dict[Tier, bool] = {
    Tier.LOW: True,
    Tier.MEDIUM: True,
    Tier.HARD: False,
}


def build_execution_block(
    tier: Tier,
    *,
    change_mode: str | None = None,
    low_model_resolver: Callable[[], str] | None = None,
) -> dict:
    """Return the ``execution`` dict for ``tier``.

    Args:
        tier: the resolved RFC-0011 difficulty tier.
        change_mode: optional change-mode (e.g. ``"migration"``) carried through
            onto the block for downstream equivalence handling; does not change
            the tier (resolve that with ``tier_for`` first).
        low_model_resolver: injectable Ollama probe for the low tier (defaults
            to :func:`pfactory.tiers.resolve_low_tier_model`) — lets tests pin
            the model without touching the network.

    The returned dict is JSON-serialisable and uses the snake_case keys that
    ``trusted_plan.execution_profile_to_metadata`` maps into task_metadata.json.
    """
    if not isinstance(tier, Tier):
        raise TypeError(f"tier must be a Tier, got {type(tier)!r}")

    if tier is Tier.LOW:
        resolver = low_model_resolver or resolve_low_tier_model
        model = resolver()
    else:
        model = _STATIC_MODEL[tier]

    block: dict = {
        "model": model,
        "skip_planning": _SKIP_PLANNING[tier],
        "review_tier": _REVIEW_TIER[tier],
        "complexity": _COMPLEXITY[tier],
        "autonomy_tier": tier.value,
    }
    if isinstance(change_mode, str) and change_mode.strip():
        block["change_mode"] = change_mode.strip().lower()
    return block
