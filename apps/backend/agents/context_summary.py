"""Structured active-task context summary for long Act runs (#475, Hermes-inspired).

Long Act runs accumulate conversation + tool output until they hit the model's
context window. The Claude Agent SDK owns the actual token compaction, but
SDK 0.2.82 fires a **PreCompact** hook at the (authoritative) compaction
boundary — the clean signal ``compaction_recovery.py`` was waiting for. So the
feasible, valuable adoption of Hermes' ``ContextCompressor`` here is:

  - At the PreCompact boundary, build/refresh a **structured 9-section
    "active task" summary** from the spec artifacts (deterministic — no model
    call required) and persist it, so the existing post-compaction *re-injection*
    (``compaction_recovery.build_operational_context``) re-anchors the agent with
    a principled summary rather than a thin heuristic.
  - The same structured block doubles as CFactory's live WorkItem summary.
  - **Token budgeting** (floor/ceiling) + an **anti-thrash guard** (skip a
    refresh when recent passes barely helped) + a **deterministic fallback** so
    a missing artifact never produces an empty anchor.

This is intentionally dependency-free and does NOT try to replace the SDK's
internal compaction (not possible): the SDK compacts the token stream; we own
the structured grounding that survives it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = [
    "SECTIONS",
    "build_active_task_summary",
    "should_refresh",
    "write_active_context",
    "summary_enabled",
]

# The 9-section template (Hermes' "Active Task" shape, adapted to AIFactory).
SECTIONS = (
    "Active Task",
    "Goal",
    "Current Subtask",
    "Completed Actions",
    "Pending / Next Step",
    "Key Files",
    "Constraints & Rules",
    "Recent Decisions",
    "Open Questions",
)

_CEILING_CHARS = 12000  # ~ Hermes' 12k-token ceiling, char-proxied
_FLOOR_CHARS = 600


def summary_enabled() -> bool:
    """Off by default; opt in with AIFACTORY_CONTEXT_SUMMARY=true."""
    return (os.environ.get("AIFACTORY_CONTEXT_SUMMARY") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _read_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_text(p: Path, limit: int = 4000) -> str:
    try:
        return p.read_text()[-limit:]
    except OSError:
        return ""


def _first_line(text: str, default: str = "") -> str:
    for ln in text.splitlines():
        ln = ln.strip().lstrip("# ").strip()
        if ln:
            return ln
    return default


def _plan_sections(plan: dict) -> tuple[str, list[str], list[str]]:
    """(current_subtask, completed[], pending[]) from implementation_plan.json."""
    current, done, pending = "", [], []
    phases = plan.get("phases") or [{"subtasks": plan.get("subtasks", [])}]
    for ph in phases:
        for st in ph.get("subtasks", []) or []:
            desc = (
                st.get("description") or st.get("title") or st.get("id") or ""
            ).strip()
            if not desc:
                continue
            status = (st.get("status") or "pending").lower()
            if status in {"completed", "done", "passed"}:
                done.append(desc)
            elif status in {"in_progress", "running", "active"} and not current:
                current = desc
            else:
                pending.append(desc)
    if not current and pending:
        current = pending[0]
    return current, done, pending


def build_active_task_summary(
    spec_dir: Path | str, *, budget_chars: int = _CEILING_CHARS
) -> str:
    """Build the structured 9-section summary from spec artifacts. Deterministic;
    a missing artifact degrades to a clearly-marked fallback, never an empty
    section. Truncated to ``budget_chars`` (ceiling), with a hard floor."""
    spec = Path(spec_dir)
    reqs = _read_json(spec / "requirements.json")
    plan = _read_json(spec / "implementation_plan.json")
    spec_md = _read_text(spec / "spec.md", 3000)
    progress = _read_text(spec / "build-progress.txt", 2000)

    title = (
        (reqs.get("metadata") or {}).get("title")
        or reqs.get("title")
        or _first_line(spec_md)
        or spec.name
    )
    goal = (reqs.get("description") or _first_line(spec_md) or title).strip()
    current, done, pending = _plan_sections(plan)

    progress_lines = [ln.strip() for ln in progress.splitlines() if ln.strip()][-6:]

    content = {
        "Active Task": title,
        "Goal": goal,
        "Current Subtask": current or "(none in progress)",
        "Completed Actions": done[-12:] or ["(none recorded yet)"],
        "Pending / Next Step": pending[:8] or ["(plan complete or not yet generated)"],
        "Key Files": sorted(
            {
                f
                for st in (plan.get("phases") or [{}])
                for sub in (st.get("subtasks", []) or [])
                for f in (sub.get("files_to_modify") or sub.get("files") or [])
            }
        )[:12]
        or ["(see plan)"],
        "Constraints & Rules": [
            "Stay within the worktree; follow CLAUDE.md + code style.",
            "Do not weaken or delete existing test assertions (#467).",
        ],
        "Recent Decisions": progress_lines or ["(no recent progress log)"],
        "Open Questions": ["(none flagged)"],
    }

    out = ["# Active task context (re-anchor after compaction)", ""]
    for sec in SECTIONS:
        val = content.get(sec)
        out.append(f"## {sec}")
        if isinstance(val, list):
            out.extend(f"- {v}" for v in val)
        else:
            out.append(str(val).strip() or "(n/a)")
        out.append("")
    text = "\n".join(out).strip() + "\n"

    ceiling = max(budget_chars, _FLOOR_CHARS)  # never truncate below the floor
    if len(text) > ceiling:
        text = text[:ceiling].rstrip() + "\n…(truncated)\n"
    return text


def should_refresh(recent_savings: list[float], *, min_savings: float = 0.10) -> bool:
    """Anti-thrash: skip a refresh when the last two passes EACH saved < 10%.

    ``recent_savings`` is a list of fractional savings (0..1) from prior passes,
    most-recent last. Mirrors Hermes' "skip if the last two passes saved <10%".
    """
    if len(recent_savings) >= 2 and all(s < min_savings for s in recent_savings[-2:]):
        return False
    return True


def write_active_context(spec_dir: Path | str) -> Path | None:
    """Build + persist the summary to ``<spec_dir>/active_context.md`` for the
    post-compaction re-injection to pick up. Best-effort; returns the path."""
    spec = Path(spec_dir)
    try:
        target = spec / "active_context.md"
        target.write_text(build_active_task_summary(spec))
        return target
    except OSError:
        return None
