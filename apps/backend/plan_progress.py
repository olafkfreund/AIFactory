"""Runtime progress keys of ``implementation_plan.json`` (#1924).

ONE definition shared by the plan-approval hash (``review/state.py``) and the
plan-command allowlist (``security/plan_commands.py``). Both must ignore exactly
the same keys: a key the hash skips can change after approval, so a command
read from it would be granted without review.
"""

from __future__ import annotations

from typing import Any

# Runtime progress keys the approve route, the coder, QA, recovery and the
# contract gate write. They are not what a reviewer approves, so they are left
# out of the approval hash (#1924). Everything else stays bound, at any depth.
PLAN_PROGRESS_KEYS = frozenset(
    {
        "status",
        "planStatus",
        "reviewReason",
        "recoveryNote",
        "contractHold",
        "qa_signoff",
        "qa_iteration_history",
        "qa_stats",
        "last_updated",
        "created_at",
        "updated_at",
        "phase",
    }
)
SUBTASK_PROGRESS_KEYS = frozenset(
    {
        "status",
        "notes",
        "updated_at",
        "started_at",
        "completed_at",
        "actual_output",
        "critique_result",
    }
)


def strip_plan_progress(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan without its progress keys.

    ``chunks`` is ignored when ``subtasks`` is present: the plan model reads
    ``subtasks`` first and every save rewrites ``chunks`` as a copy of it.
    """
    content = {k: v for k, v in plan.items() if k not in PLAN_PROGRESS_KEYS}
    phases = content.get("phases")
    if not isinstance(phases, list):
        return content
    stripped = []
    for raw_phase in phases:
        if not isinstance(raw_phase, dict):
            stripped.append(raw_phase)
            continue
        phase = dict(raw_phase)
        if isinstance(phase.get("subtasks"), list):
            phase.pop("chunks", None)
        for key in ("subtasks", "chunks"):
            if isinstance(phase.get(key), list):
                phase[key] = [
                    {k: v for k, v in st.items() if k not in SUBTASK_PROGRESS_KEYS}
                    if isinstance(st, dict)
                    else st
                    for st in phase[key]
                ]
        stripped.append(phase)
    content["phases"] = stripped
    return content
