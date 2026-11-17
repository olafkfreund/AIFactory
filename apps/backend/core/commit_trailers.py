"""Agent identity / originator / tier as git trailers on factory commits (Factory#1960).

Opt-in via ``AIFACTORY_AGENT_COMMIT_TRAILERS``. Values come from the spec's
``requirements.json`` exactly as recorded; nothing is invented.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# One format for the writer, the trailer and the deploy step (#2109).
PLAN_SLUG_RE = re.compile(r"[A-Za-z0-9._/#:-]{1,128}")


def trailers_enabled() -> bool:
    return os.environ.get("AIFACTORY_AGENT_COMMIT_TRAILERS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def commit_trailer_args(spec_dir: Path | None) -> list[str]:
    """Trailer argv items for a commit; ``[]`` when off or unreadable. Never raises."""
    if spec_dir is None or not trailers_enabled():
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
        # isprintable() refuses line breaks (forged trailers) and NUL (argv ValueError).
        if isinstance(value, str) and value and value.isprintable():
            out += ["--trailer", f"{key}: {value}"]
    slug = data.get("plan_slug")
    if isinstance(slug, str) and PLAN_SLUG_RE.fullmatch(slug):
        out += ["--trailer", f"Plan-Slug: {slug}"]
    return out


def stamp_plan_slug(spec_dir: Path, corr: object) -> bool:
    """Record a trusted plan's correlation key as ``plan_slug``; never raises."""
    key = (
        str(corr) if isinstance(corr, str | int) and not isinstance(corr, bool) else ""
    )
    if not PLAN_SLUG_RE.fullmatch(key):
        return False
    path = spec_dir / "requirements.json"
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return False
        data["plan_slug"] = key
        path.write_text(json.dumps(data, indent=2))
    except (OSError, ValueError):
        return False
    return True


def trailer_block(spec_dir: Path | None) -> str:
    """Trailer lines joined by newlines for a ``-m``/``--body`` text; ``""`` when none."""
    return "\n".join(commit_trailer_args(spec_dir)[1::2])


def commit_message_args(message: str, spec_dir: Path | None) -> list[str]:
    """``-m`` plus trailer argv for ``git commit``; exactly ``["-m", message]`` when off.

    With trailers on, the message is collapsed to one printable line: it embeds
    agent-written text (subtask ids), and a line break there could forge trailers.
    """
    trailers = commit_trailer_args(spec_dir)
    if not trailers:
        return ["-m", message]
    line = " ".join("".join(c if c.isprintable() else " " for c in message).split())
    return ["-m", line, *trailers]
