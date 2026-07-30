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


def _text(msg: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}]}


def _project_root(spec_dir: Path, project_dir: Path | None) -> Path:
    """The repo the build is writing to. Callers pass it; when they do not, derive
    it from the ``<root>/.aifactory/specs/<spec>`` layout. Falling back to
    ``spec_dir`` is safe — the #1111 check finds no test files and stays inert."""
    if project_dir is not None:
        return project_dir
    parents = spec_dir.resolve().parents
    if len(parents) >= 3 and parents[1].name == ".aifactory":
        return parents[2]
    return spec_dir


async def apply_subtask_status_update(
    spec_dir: Path,
    subtask_id: str,
    status: str,
    notes: str = "",
    project_dir: Path | None = None,
) -> dict[str, Any]:
    """Update a subtask's status in implementation_plan.json, enforcing the #851
    honest-verification gate and the #1111 deliverable-coverage gate. Plain
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

        # Find and update the subtask
        subtask_found = False
        target_subtask: dict[str, Any] | None = None
        for phase in plan.get("phases", []):
            for subtask in phase.get("subtasks", []):
                if subtask.get("id") == subtask_id:
                    subtask["status"] = status
                    if notes:
                        subtask["notes"] = notes
                    subtask["updated_at"] = datetime.now(timezone.utc).isoformat()
                    subtask_found = True
                    target_subtask = subtask
                    break
            if subtask_found:
                break

        if not subtask_found:
            return _text(
                f"Error: Subtask '{subtask_id}' not found in implementation plan"
            )

        # #851 honest-verification gate: a test/verification subtask may not be
        # reported "completed" unless a real test command actually ran this build
        # (captured tamper-evidently by the PostToolUse hook). No run — or a run
        # that clearly failed — is refused with actionable guidance, so the coder
        # can no longer self-report a green checkbox for tests it never executed
        # (RFC-0006). The plan is not written until AFTER this, so a refusal
        # leaves implementation_plan.json untouched.
        if status == "completed":
            from agents.test_evidence import (  # noqa: PLC0415
                deliverable_evidence_gap,
                gate_enabled,
                is_verification_subtask,
                read_test_evidence,
            )

            # #1111: "a test ran" is not "a test ran against the deliverable".
            # A subtask that promises an HTTP path may not be completed when the
            # only tests naming that path assert against an app built inside the
            # test file — genuinely green, and blind to a route nobody
            # registered. Inert unless the subtask names a path and a Python
            # test mentions it, so pure-function work is untouched.
            if gate_enabled():
                gap = deliverable_evidence_gap(
                    target_subtask or {}, _project_root(spec_dir, project_dir)
                )
                if gap:
                    return _text(gap)

            if gate_enabled() and is_verification_subtask(target_subtask or {}):
                ev = read_test_evidence(spec_dir)
                if not ev["ran"]:
                    return _text(
                        f"Refused: subtask '{subtask_id}' is a test/verification subtask, "
                        "but no test command ran this build. Run the tests now (e.g. "
                        "pytest / go test / npm test) — the build records the run "
                        "automatically — then mark it completed. If this repo has NO "
                        "runnable test environment, mark this subtask 'failed' with a note "
                        "saying tests could not be executed. Do NOT report it completed "
                        "unverified (RFC-0006: never claim verification that did not happen)."
                    )
                if ev["last_failed"]:
                    return _text(
                        f"Refused: the last recorded test run failed (command: "
                        f"{ev['last_command']!r}). Fix the failures and re-run the tests "
                        f"green before completing subtask '{subtask_id}', or mark it "
                        "'failed' with the reason. Do NOT report it completed over failing "
                        "tests."
                    )

        # Update plan metadata
        plan["last_updated"] = datetime.now(timezone.utc).isoformat()

        with open(plan_file, "w") as f:
            json.dump(plan, f, indent=2)

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
