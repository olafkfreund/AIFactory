"""Backward-compatibility shim — re-exports from ``providers.antigravity_agentic``.

The agentic Gemini CLI provider was renamed to ``AntigravityAgenticProvider``
(the Antigravity CLI is the post-Gemini-CLI successor that AIFactory actually
drives via ``--yolo``).  This module preserves the legacy import path::

    from providers.gemini_agentic import GeminiAgenticProvider, get_gemini_binary

so existing configs and callers keep working.
"""

from providers.antigravity_agentic import (  # noqa: F401
    AntigravityAgenticProvider,
    GeminiAgenticProvider,
    get_antigravity_binary,
    get_gemini_binary,
)

__all__ = [
    "AntigravityAgenticProvider",
    "GeminiAgenticProvider",
    "get_antigravity_binary",
    "get_gemini_binary",
]
