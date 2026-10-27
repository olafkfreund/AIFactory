"""Agent identity / originator / tier as git trailers on factory commits (Factory#1960).

Opt-in via ``AIFACTORY_AGENT_COMMIT_TRAILERS``. Values come from the spec's
``requirements.json`` exactly as recorded; nothing is invented.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _enabled() -> bool:
    return os.environ.get("AIFACTORY_AGENT_COMMIT_TRAILERS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def commit_trailer_args(spec_dir: Path | None) -> list[str]:
    """Trailer argv items for a commit; ``[]`` when off or unreadable. Never raises."""
    if spec_dir is None or not _enabled():
        return []
    try:
        data = json.loads((spec_dir / "requirements.json").read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    intake = data.get("intake")
    tier = intake.get("autonomy_tier") if isinstance(intake, dict) else None
    out: list[str] = []
    for key, value in (
        ("Agent-Identity", data.get("agent_identity")),
        ("Autonomy-Tier", tier),
        ("Originator", data.get("originator")),
    ):
        if isinstance(value, str) and value and "\n" not in value and "\r" not in value:
            out += ["--trailer", f"{key}: {value}"]
    return out
