"""Backward compatibility shim — re-exports from providers.antigravity.

``GeminiCLIProvider`` was renamed to ``AntigravityCLIProvider``; this legacy
path keeps working via the alias.
"""
from providers.antigravity import (  # noqa: F401
    AntigravityCLIProvider,
    GeminiCLIProvider,
)

__all__ = ["GeminiCLIProvider", "AntigravityCLIProvider"]
