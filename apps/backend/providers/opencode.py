"""
OpenCode provider (text-only entry point)
=========================================

OpenCode is a CLI coding-agent runtime whose non-interactive ``opencode run``
mode returns its final analysis as plain text.  Like the GitHub Copilot CLI,
OpenCode has no separate "text-only" execution mode distinct from its agentic
mode — the single ``run`` invocation both performs tool actions *and* returns
text — so the text-only registry entry simply reuses the agentic provider.

This thin module exists so the provider package layout stays symmetric with the
other providers (``codex`` / ``codex_agentic``, ``gemini`` / ``gemini_agentic``)
and so ``providers.factory`` can register an ``opencode`` text entry that points
at a real module path.

See ``providers.opencode_agentic.OpenCodeAgenticProvider`` for the implementation.
"""

from __future__ import annotations

from providers.opencode_agentic import OpenCodeAgenticProvider

# Text-only callers get the same adapter — ``opencode run`` returns text fine.
OpenCodeProvider = OpenCodeAgenticProvider

__all__ = ["OpenCodeAgenticProvider", "OpenCodeProvider"]
