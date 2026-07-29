"""Write a task's status to both stores the board reads.

``implementation_plan.json`` is what the task list renders from;
``task_control.json`` is the agent-immutable control store and is authoritative
for the board column (#259). Writing only one leaves the two disagreeing, which
is how a task ends up displayed in two states at once.

Extracted so the orphan reaper (#1064) and the approve/merge path (#1071) share
one implementation. They previously would have had two, and two copies of a
"write both stores" rule is exactly how one of them ends up writing one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from server.services import task_control

logger = logging.getLogger(__name__)


def write_status(
    spec_dir: Path,
    *,
    status: str,
    reason: str,
    updated_by: str,
) -> str | None:
    """Set *status* in both stores. Returns None on success, else why not.

    The control store is written FIRST and unconditionally, because it is the
    authoritative one: if only one write can land, it must be that one.

    A corrupt ``implementation_plan.json`` does not abort the update. Those
    exist in the wild -- a build emitted one with escaped quotes (#1069) -- and
    refusing to record a completed merge because a sibling file is malformed
    would strand the task in the state this function exists to leave.

    The failure is RETURNED rather than swallowed. Callers must surface it: a
    status write that silently does nothing is the defect class this codebase
    keeps rediscovering (#1069, #1070, PFactory#384).
    """
    task_control.write_control(
        spec_dir,
        status=status,
        review_reason=reason,
        updated_by=updated_by,
    )

    plan_file = spec_dir / "implementation_plan.json"
    if not plan_file.is_file():
        return None

    try:
        plan = json.loads(plan_file.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("could not read the plan file to record status: %s", exc)
        return f"{plan_file.name} is unreadable ({exc}); task_control.json was updated"

    plan["status"] = status
    plan["reviewReason"] = reason
    try:
        plan_file.write_text(json.dumps(plan, indent=2))
    except OSError as exc:
        logger.warning("could not write the plan file: %s", exc)
        return f"{plan_file.name} could not be written ({exc}); task_control.json was updated"

    return None
