"""
Subtask Management Tools
========================

Tools for managing subtask status in implementation_plan.json.
"""

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from claude_agent_sdk import tool

    SDK_TOOLS_AVAILABLE = True
except ImportError:
    SDK_TOOLS_AVAILABLE = False
    tool = None  # type: ignore[assignment]


# How far above a spec dir its project root sits, in ``Path.parents`` steps:
# ``<root>/.aifactory/specs/<spec>`` puts ``.aifactory`` at parents[1] and the
# root at parents[2]. The same number bounds the guard and picks the result, so
# the layout is stated once.
_ROOT_PARENTS_ABOVE_SPEC = 2


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}


def _project_root(spec_dir: Path, project_dir: Path | None) -> Path:
    """The repo the build is writing to. Callers pass it; when they do not, derive
    it from the ``<root>/.aifactory/specs/<spec>`` layout. Falling back to
    ``spec_dir`` is safe — the #1111 check finds no test files and stays inert."""
    if project_dir is not None:
        return project_dir
    parents = spec_dir.resolve().parents
    if len(parents) > _ROOT_PARENTS_ABOVE_SPEC and parents[1].name == ".aifactory":
        return parents[_ROOT_PARENTS_ABOVE_SPEC]
    return spec_dir


async def apply_subtask_status_update(
    spec_dir: Path,
    subtask_id: str,
    status: str,
    notes: str = "",
    project_dir: Path | None = None,
) -> dict[str, Any]:
    """Update a subtask's status in implementation_plan.json, enforcing the
    honesty gates in :mod:`agents.completion_gate` (#851, #1111, #1113). Plain
    (SDK-free) so it is directly testable; the ``update_subtask_status`` tool is
    a thin wrapper resolving the spec and project dirs.
    """
    valid_statuses = ["pending", "in_progress", "completed", "failed"]
    if status not in valid_statuses:
        return _text(
            f"Error: Invalid status '{status}'. Must be one of: {valid_statuses}"
        )

    plan_file = spec_dir / "implementation_plan.json"
    if not plan_file.exists():
        return _text("Error: implementation_plan.json not found")

    try:
        with open(plan_file) as f:
            plan = json.load(f)

        # Find and update the subtask (the record is the one IN ``plan``, so
        # mutating it here is what the write below persists).
        target_subtask: dict[str, Any] | None = next(
            (
                st
                for phase in plan.get("phases", [])
                for st in phase.get("subtasks", [])
                if st.get("id") == subtask_id
            ),
            None,
        )
        if target_subtask is None:
            return _text(
                f"Error: Subtask '{subtask_id}' not found in implementation plan"
            )

        now = datetime.now(timezone.utc).isoformat()
        target_subtask["status"] = status
        if notes:
            target_subtask["notes"] = notes
        target_subtask["updated_at"] = now

        # #1195: stamp ``started_at`` with the SAME write that sets the status,
        # because CFactory's live execution diagram reads it —
        # ``taskFlow.nodeElapsedSeconds`` returns null without it, so every
        # subtask's timer chip in the cockpit rendered empty on every build.
        # Only on the first transition: a retried subtask keeps its original
        # start, so the clock does not reset under the reviewer mid-build.
        if status == "in_progress" and not target_subtask.get("started_at"):
            target_subtask["started_at"] = now

        # The honesty gates (#851 test evidence, #1111 deliverable coverage,
        # #1113 pipeline evidence) live in agents.completion_gate because the
        # parallel wave path has to pass the SAME ones — it completes subtasks
        # itself, without this tool (#1177).
        # Add a gate there, not here. The plan is not written until AFTER this,
        # so a refusal leaves implementation_plan.json untouched.
        if status == "completed":
            from agents.completion_gate import completion_refusal  # noqa: PLC0415
            from agents.test_evidence import read_test_evidence  # noqa: PLC0415

            refusal = completion_refusal(
                target_subtask,
                _project_root(spec_dir, project_dir),
                # Scoped to THIS subtask (#1187): build-wide evidence let the
                # second verification subtask in a build ride the first one's
                # green run and complete having executed nothing.
                read_test_evidence(spec_dir, subtask_id),
            )
            if refusal:
                return _text(refusal)

        # Update plan metadata
        plan["last_updated"] = datetime.now(timezone.utc).isoformat()

        with open(plan_file, "w") as f:
            json.dump(plan, f, indent=2)

        if status == "completed":
            # Close this subtask's evidence window — AFTER the gate accepted it,
            # so a refusal leaves the runs for the retry to use (#1187).
            from agents.test_evidence import record_subtask_completed  # noqa: PLC0415

            record_subtask_completed(spec_dir, subtask_id)

        return _text(
            f"Successfully updated subtask '{subtask_id}' to status '{status}'"
        )

    except json.JSONDecodeError as e:
        return _text(f"Error: Invalid JSON in implementation_plan.json: {e}")
    except Exception as e:  # noqa: BLE001
        return _text(f"Error updating subtask status: {e}")


def create_subtask_tools(
    spec_dir: Path | Callable[[], Path],
    project_dir: Path | Callable[[], Path],
) -> list[Any]:
    """
    Create subtask management tools.

    Accepts either a fixed Path (in-process callers — agent sessions own a
    specific spec for their lifetime) or a callable returning Path (standalone
    MCP server callers — the active spec is resolved per tool call via env).
    Issue #10.

    Args:
        spec_dir: Path or Callable[[], Path] to the spec directory
        project_dir: Path or Callable[[], Path] to the project root

    Returns:
        List of subtask tool functions
    """
    if not SDK_TOOLS_AVAILABLE:
        return []

    # Normalise Path -> lambda once at factory build time; tool handlers
    # invoke get_spec_dir() per call so the standalone server picks up
    # AIFACTORY_SPEC_DIR changes between calls.
    if callable(spec_dir):
        get_spec_dir: Callable[[], Path] = spec_dir
    else:
        fixed: Path = spec_dir
        get_spec_dir = lambda: fixed  # noqa: E731 — tiny fixed-path accessor

    if callable(project_dir):
        get_project_dir: Callable[[], Path] = project_dir
    else:
        fixed_project: Path = project_dir
        get_project_dir = lambda: fixed_project  # noqa: E731 — fixed-path accessor

    tools = []

    # -------------------------------------------------------------------------
    # Tool: update_subtask_status
    # -------------------------------------------------------------------------
    @tool(
        "update_subtask_status",
        "Update the status of a subtask in implementation_plan.json. Use this when completing or starting a subtask.",
        {"subtask_id": str, "status": str, "notes": str},
    )
    async def update_subtask_status(args: dict[str, Any]) -> dict[str, Any]:
        """Update subtask status in the implementation plan (thin wrapper over
        :func:`apply_subtask_status_update`, which holds the logic + the #851
        and #1111 gates)."""
        return await apply_subtask_status_update(
            get_spec_dir(),
            args["subtask_id"],
            args["status"],
            args.get("notes", ""),
            get_project_dir(),
        )

    tools.append(update_subtask_status)

    return tools
