"""Read and write a task's status across the stores the board reads.

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
from typing import Any

from factory_common.logsafe import sanitize_log

from server.services import task_control

logger = logging.getLogger(__name__)

# Status token for "the plan file exists but could not be parsed". Deliberately
# NOT a lifecycle value: every lifecycle value asserts something about the work
# (queued, running, finished) that a failed read cannot support. See #1069 and
# the fleet-wide silent-fallback epic Factory#431.
PLAN_UNREADABLE = "plan_unreadable"


def read_plan(plan_file: Path) -> tuple[dict[str, Any], str | None]:
    """Parse a plan file. Returns ``(plan, error)``; ``error`` is None on success.

    The single reader for ``implementation_plan.json`` so every caller reports
    the same fault the same way. A corrupt plan is a FAULT, and the failure is
    RETURNED rather than swallowed: the callers that used to ``pass`` on
    ``JSONDecodeError`` left ``plan`` empty and let the ``backlog`` default win,
    so a task that had been built successfully was reported as never started
    (#1069).

    Logs at ERROR with the path and the parse offset, because a build that emits
    unparseable JSON is a defect someone has to fix, not a degraded read.
    """
    try:
        plan = json.loads(plan_file.read_text())
    except json.JSONDecodeError as exc:
        logger.error(
            "%s is not valid JSON at line %s column %s (char %s): %s",
            sanitize_log(plan_file),
            sanitize_log(exc.lineno),
            sanitize_log(exc.colno),
            sanitize_log(exc.pos),
            sanitize_log(exc.msg),
        )
        return {}, f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
    except (OSError, ValueError) as exc:  # unreadable file / undecodable bytes
        logger.error(
            "%s could not be read: %s", sanitize_log(plan_file), sanitize_log(exc)
        )
        return {}, f"could not be read: {exc}"

    if not isinstance(plan, dict):
        # Valid JSON, but not a plan. Same fault class: every caller indexes it
        # as a mapping, so a list/scalar is just as unusable as a parse error.
        logger.error("%s is valid JSON but not an object", sanitize_log(plan_file))
        return {}, f"expected a JSON object, got {type(plan).__name__}"
    return plan, None


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
