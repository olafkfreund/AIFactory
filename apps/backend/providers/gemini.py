"""Backward-compatibility shim — re-exports from ``providers.antigravity``.

The Gemini CLI text-only provider was renamed to ``AntigravityCLIProvider``
(the Antigravity CLI is the post-Gemini-CLI successor that AIFactory actually
drives).  This module preserves the legacy import path::

    from providers.gemini import GeminiCLIProvider, get_gemini_binary

so existing configs and callers keep working.
"""

from providers.antigravity import (  # noqa: F401
    AntigravityCLIProvider,
    GeminiCLIProvider,
    _emit_sunset_warning,
    get_antigravity_binary,
    get_gemini_binary,
)

__all__ = [
    "AntigravityCLIProvider",
    "GeminiCLIProvider",
    "get_antigravity_binary",
    "get_gemini_binary",
    "_emit_sunset_warning",
]
