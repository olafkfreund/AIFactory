"""Shared antigravity/gemini CLI binary resolution.

A single source of truth for locating the ``antigravity`` binary so the plain
and agentic antigravity providers can't drift.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def get_antigravity_binary(custom_path: str | None = None) -> str:
    """Dynamically resolve the antigravity / gemini binary path.

    Prefers the ``antigravity`` binary, then the bundled
    ``~/.gemini/antigravity-cli/bin/antigravity`` install location, then the
    legacy ``gemini`` binary, falling back to ``antigravity`` (preinstalled by
    default).
    """
    if custom_path and custom_path not in ("antigravity", "gemini"):
        return custom_path
    if shutil.which("antigravity"):
        return "antigravity"
    custom_path_default = (
        Path.home() / ".gemini" / "antigravity-cli" / "bin" / "antigravity"
    )
    if custom_path_default.exists():
        return str(custom_path_default)
    if shutil.which("gemini"):
        return "gemini"
    # Fallback to antigravity since we preinstall it by default
    return "antigravity"
