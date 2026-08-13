"""
Post-Compact Context Recovery (#262)
====================================

Long autonomous builds run many turns inside a single SDK client. When the
runtime **compacts** (summarises) its context mid-run, fine-grained
operational grounding — the spec's intent, the exact current subtask, the
coordination/branching rules — can get lossily summarised away, and the agent
starts drifting (re-exploring, re-deciding settled questions, ignoring the
branch policy).

This module provides two things:

1. ``build_operational_context(...)`` — a small, deterministic "operational
   context" block (spec summary + current subtask + coordination rules) that
   is cheap to re-inject at a natural seam so the agent re-anchors.

2. Best-effort **compaction detection** across turns. The clean signal is the
   SDK's ``PreCompact`` hook / a ``SystemMessage(subtype="compact_boundary")``
   — see the TODO below for the precise wiring. Until that hook is threaded
   through the agent loop, ``CompactionDetector`` infers a compaction from a
   sharp drop in the turn's reported input-token total (the SDK rebuilds the
   prompt from a summary, so input tokens fall steeply), which is a reliable
   *best-effort* proxy.

The re-injection *primitive* (the context block + the detector) is the
foundation; wiring it to the authoritative SDK signal is a small, well-scoped
follow-up captured in the TODO so we never fake an SDK event.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# A turn whose input-token total falls below this fraction of the running
# peak is treated as a probable compaction (the SDK replaced full history
# with a summary). 0.5 = input more than halved vs. the peak seen so far.
DEFAULT_DROP_RATIO = 0.5

# Don't treat early, naturally-small turns as compactions. Only consider a
# drop meaningful once the peak input has grown past this floor.
MIN_PEAK_TOKENS = 20_000


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug(
            "compaction_recovery: could not read %s (%s), using empty context",
            path,
            exc,
        )
    return {}


def _current_subtask_summary(spec_dir: Path) -> str | None:
    """Return a one-line summary of the in-progress / next subtask, if any."""
    plan = _load_json(spec_dir / "implementation_plan.json")
    if not plan:
        return None

    # Plans use either {"phases":[{"subtasks":[...]}]} or {"subtasks":[...]}.
    def _iter_subtasks() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if isinstance(plan.get("phases"), list):
            for ph in plan["phases"]:
                out.extend(ph.get("subtasks", []) or [])
        out.extend(plan.get("subtasks", []) or [])
        return out

    subtasks = _iter_subtasks()
    in_progress = [s for s in subtasks if s.get("status") == "in_progress"]
    pending = [s for s in subtasks if s.get("status") == "pending"]
    candidate = (in_progress or pending or [None])[0]
    if not candidate:
        return None
    sid = candidate.get("id", "?")
    title = candidate.get("title") or candidate.get("description") or ""
    title = title.strip().splitlines()[0] if title else ""
    return f"{sid}: {title}".strip().rstrip(":")


def _spec_summary(spec_dir: Path, max_chars: int = 600) -> str | None:
    """A short summary of the feature being built (from spec.md or
    requirements.json)."""
    spec_md = spec_dir / "spec.md"
    if spec_md.exists():
        try:
            text = spec_md.read_text(encoding="utf-8").strip()
            # Prefer the Overview section if present.
            lower = text.lower()
            idx = lower.find("## overview")
            if idx != -1:
                text = text[idx:]
            return text[:max_chars].strip()
        except OSError:
            pass
    req = _load_json(spec_dir / "requirements.json")
    desc = req.get("description") or req.get("task")
    if isinstance(desc, str) and desc.strip():
        return desc.strip()[:max_chars]
    return None


# Coordination / operational rules that long builds must not lose. Kept
# terse on purpose — this is re-injected, so it must stay cheap.
COORDINATION_RULES = (
    "- Stay on the current spec branch; do NOT switch branches or push to remote.\n"
    "- Implement exactly the current subtask; update its status in "
    "implementation_plan.json when done.\n"
    "- Commit each completed subtask before moving on.\n"
    "- Re-read a file before editing it; verify changes with the project's "
    "tests/build where applicable."
)


def build_operational_context(
    spec_dir: Path,
    *,
    current_subtask: str | None = None,
    extra_rules: str | None = None,
) -> str:
    """Build the compact re-injection block.

    Pulls spec summary + current subtask from disk (cheap, deterministic) and
    appends the standing coordination rules. ``current_subtask`` overrides the
    on-disk lookup when the caller already knows it.
    """
    spec_dir = Path(spec_dir)
    summary = _spec_summary(spec_dir)
    subtask = current_subtask or _current_subtask_summary(spec_dir)

    lines = [
        "## Operational context (re-anchor after context compaction)",
        "",
        "Your context was just compacted. Re-ground yourself in the build "
        "before continuing — do not re-plan or re-explore settled work:",
        "",
    ]
    if summary:
        lines.append(f"**Building:** {summary}")
        lines.append("")
    if subtask:
        lines.append(f"**Current subtask:** {subtask}")
        lines.append("")
    lines.append("**Coordination rules:**")
    lines.append(COORDINATION_RULES)
    if extra_rules:
        lines.append(extra_rules.strip())
    return "\n".join(lines).strip()


@dataclass
class CompactionDetector:
    """Best-effort cross-turn compaction detector.

    Feed it each turn's real input-token total via ``observe``; it returns
    True on the turn where a compaction is inferred (sharp input drop vs. the
    running peak). State is intentionally in-memory and per-client-session;
    the agent loop owns one detector per SDK client.

    # TODO(#262): Replace this heuristic with the authoritative SDK signal.
    # The Claude Agent SDK emits a ``PreCompact`` hook
    # (``claude_agent_sdk.types.PreCompactHookInput``, hook_event_name
    # "PreCompact", trigger "auto"|"manual") and a post-compaction
    # ``SystemMessage`` with ``subtype == "compact_boundary"`` in the message
    # stream. The precise wiring is:
    #   1. In ``core/client.py`` add a ``PreCompact`` entry to ``hooks={}``
    #      whose callback writes a sentinel (e.g. ``<spec_dir>/.compacted``)
    #      or sets a flag on a shared object, OR
    #   2. In ``agents/session.py`` detect ``msg_type == "SystemMessage" and
    #      getattr(msg, "subtype", "") == "compact_boundary"`` while iterating
    #      ``safe_receive_messages`` and surface a ``compacted=True`` flag on
    #      the returned tuple.
    # Either gives an exact, non-heuristic trigger; this detector is the
    # interim fallback so recovery works today without faking an SDK event.
    """

    drop_ratio: float = DEFAULT_DROP_RATIO
    min_peak_tokens: int = MIN_PEAK_TOKENS
    peak_input_tokens: int = 0
    _last_input_tokens: int = 0

    def observe(self, input_tokens: int) -> bool:
        """Record a turn's input-token total; return True if it looks like a
        compaction just happened (steep drop from the peak)."""
        compacted = False
        if (
            self.peak_input_tokens >= self.min_peak_tokens
            and input_tokens > 0
            and input_tokens < self.peak_input_tokens * self.drop_ratio
        ):
            compacted = True
            # Reset the peak to the post-compaction level so we can detect a
            # *subsequent* compaction after the context regrows.
            self.peak_input_tokens = input_tokens
        else:
            self.peak_input_tokens = max(self.peak_input_tokens, input_tokens)
        self._last_input_tokens = input_tokens
        return compacted
