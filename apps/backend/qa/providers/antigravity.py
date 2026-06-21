"""Compatibility shim — re-exports from providers.antigravity."""

from providers.antigravity import (  # noqa: F401
    AntigravityCLIProvider,
    GeminiCLIProvider,
)

__all__ = ["AntigravityCLIProvider", "GeminiCLIProvider"]
