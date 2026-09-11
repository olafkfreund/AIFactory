"""
QA Management Tools
===================

Tools for managing QA status and sign-off in implementation_plan.json.
"""

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents.gate_runner import (
    evidence_shows_an_executed_gate,
    gate_outcomes_include_a_failure,
    trailing_gate_evidence,
)

from .api_contract import missing_exports

try:
    from claude_agent_sdk import tool

    SDK_TOOLS_AVAILABLE = True
except ImportError:
    SDK_TOOLS_AVAILABLE = False
    tool = None


def _missing_contract_exports(spec_dir: Path, project_dir: Path) -> list[str]:
    """Required names from spec.md that this build did not define (#1421).

    Best-effort: an unreadable spec yields no contract and no refusal. An
    unreadable spec is not evidence that a contract was broken, and blocking
    sign-off on it would trade a false pass for a false failure — the same trade
    ``_nothing_was_built`` refuses to make when git cannot answer.
    """
    try:
        spec_text = (spec_dir / "spec.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return missing_exports(spec_text, project_dir)


def _nothing_was_built(project_dir: Path) -> str | None:
    """Explain why this worktree shows no build output, or None if it does.

    Two signals, either of which is evidence of work: a commit that is not yet
    on any origin branch, or an uncommitted change. Both empty means the build
    produced nothing.

    ``--not --remotes=origin`` is what makes this work without knowing the base
    branch. The worktree is cut from a base commit that IS on origin, so that
    commit is excluded and only what this build added is counted. Asking for the
    base by name would mean resolving it here, and a wrong guess would make the
    check silently vacuous -- the failure mode being fixed.

    Returns a human-readable reason (truthy) when nothing was built, so the
    caller can put it in the refusal. Returns None when there is output, and
    also when git itself cannot answer: an unavailable git is not evidence of an
    empty build, and blocking every sign-off on it would trade a false pass for
    a false failure.
    """
    try:
        commits = subprocess.run(
            ["git", "rev-list", "--count", "HEAD", "--not", "--remotes=origin"],  # noqa: S607
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],  # noqa: S607
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if commits.returncode != 0 or dirty.returncode != 0:
        return None

    n = commits.stdout.strip()
    if n and n != "0":
        return None
    if dirty.stdout.strip():
        return None

    return f"{project_dir} has no commits beyond its base and no uncommitted changes."


def _approval_refusal_reason(spec_dir: Path, project_dir: Path) -> str | None:
    """Why `update_qa_status(status="approved")` must be refused, or None.

    Every guard an "approved" write has to pass, in one place, so
    `update_qa_status` itself stays a thin dispatcher rather than a wall of
    early returns (the ratchet's complexity caps exist for exactly that).

    #1396: nothing was built -- a build with no diff cannot have been tested,
    whatever `tests_passed` claims.

    #1421: the spec enumerates an API this build does not export -- a green
    suite against an invented API is indistinguishable from one against the
    right one.

    #1496: no evidence a verification command actually ran. Observed live,
    twice: the coder agent said outright it could NOT run the suite ("no
    Gradle/JVM on PATH ... I cannot execute the suite") and this tool was
    still called with status="approved". "APPROVED ✓" is the same string a
    build gets when its tests ran and passed -- so the distinction between
    *verified* and *read carefully* was destroyed at exactly the point a
    human (or a downstream dashboard) reads the result. `trailing_gate_evidence`
    reads the one OBJECTIVE record of whether a verification command
    executed: the marker the coder's own trailing-gate step writes to this
    same spec_dir (agents/coder.py `_run_trailing_gates_if_build_complete`),
    written before QA ever runs by a different code path than the one asking
    to approve -- so it cannot be satisfied by an agent simply asserting
    `tests_passed`, the thing #1396 already proved cannot be trusted alone.

    #1545: the marker alone used to be enough, but it is persistent -- a
    worktree copy or a web-sync republish can carry a PRIOR build's passing
    marker into this one. `trailing_gate_evidence` now only returns evidence
    still bound to `project_dir`'s current git HEAD, so a marker recorded for
    a different tree reads as no evidence, not a stale pass.
    """
    unbuilt = _nothing_was_built(project_dir)
    if unbuilt:
        return (
            f"Refusing to approve: nothing was built. {unbuilt} Approving "
            "here would record a passing QA sign-off, and test results, for "
            "code that does not exist (#1396). Implement the change first; "
            "if the task genuinely requires no code change, say so rather "
            "than signing off."
        )

    missing = _missing_contract_exports(spec_dir, project_dir)
    if missing:
        return (
            "Refusing to approve: the spec enumerates an API this build "
            f"does not define — {', '.join(missing)}. The card named those "
            "so dependent cards could import them; with different names "
            "each one re-implements the whole thing instead (#1421). "
            "Export the names the spec asked for, or change the spec if "
            "they are wrong — do not sign off on an equivalent API under "
            "other names."
        )

    gate_evidence = trailing_gate_evidence(spec_dir, project_dir)
    if not evidence_shows_an_executed_gate(gate_evidence):
        why = gate_evidence or "the gate step never ran for this build"
        return (
            f"Refusing to approve: no verification command is recorded as "
            f"having run ({why}). An APPROVED sign-off must be backed by an "
            "executed gate, not an agent's own reading of the code (#1496). "
            "If the toolchain is genuinely unavailable here, say so and "
            "leave this build unapproved -- do not record a pass for a "
            "suite that never ran."
        )
    if gate_outcomes_include_a_failure(gate_evidence):
        return (
            "Refusing to approve: the recorded verification gates did not "
            f"all pass ({gate_evidence}). A failing gate is evidence the "
            "build does not work, not something QA can sign off over "
            "(#1496). See GATE_FAILURES.md and fix the failure before "
            "approving."
        )

    return None


def create_qa_tools(
    spec_dir: Path | Callable[[], Path],
    project_dir: Path | Callable[[], Path],
) -> list:
    """
    Create QA management tools.

    Accepts either a fixed Path or a callable returning Path (Issue #10).

    Args:
        spec_dir: Path or Callable[[], Path] to the spec directory
        project_dir: Path or Callable[[], Path] to the project root

    Returns:
        List of QA tool functions
    """
    if not SDK_TOOLS_AVAILABLE:
        return []

    get_spec_dir: Callable[[], Path] = (
        spec_dir if callable(spec_dir) else (lambda p=spec_dir: p)
    )

    # A plain def rather than the `lambda p=project_dir: p` used just above:
    # mypy cannot infer that lambda's type and the cq ratchet counts net-new
    # errors per changed file, so copying the older idiom would have failed the
    # gate for a fresh line.
    def get_project_dir() -> Path:
        return project_dir() if callable(project_dir) else project_dir

    tools = []

    # -------------------------------------------------------------------------
    # Tool: update_qa_status
    # -------------------------------------------------------------------------
    @tool(
        "update_qa_status",
        "Update the QA sign-off status in implementation_plan.json. Use after QA review.",
        {"status": str, "issues": str, "tests_passed": str},
    )
    async def update_qa_status(args: dict[str, Any]) -> dict[str, Any]:
        """Update QA status in the implementation plan."""
        status = args["status"]
        issues_str = args.get("issues", "[]")
        tests_str = args.get("tests_passed", "{}")

        valid_statuses = [
            "pending",
            "in_review",
            "approved",
            "rejected",
            "fixes_applied",
        ]
        if status not in valid_statuses:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Error: Invalid QA status '{status}'. Must be one of: {valid_statuses}",
                    }
                ]
            }

        plan_file = get_spec_dir() / "implementation_plan.json"
        if not plan_file.exists():
            return {
                "content": [
                    {
                        "type": "text",
                        "text": "Error: implementation_plan.json not found",
                    }
                ]
            }

        try:
            # Parse issues and tests
            try:
                issues = json.loads(issues_str) if issues_str else []
            except json.JSONDecodeError:
                issues = [{"description": issues_str}] if issues_str else []

            try:
                tests_passed = json.loads(tests_str) if tests_str else {}
            except json.JSONDecodeError:
                tests_passed = {}

            with open(plan_file) as f:
                plan = json.load(f)

            # Get current QA session number
            current_qa = plan.get("qa_signoff", {})
            qa_session = current_qa.get("qa_session", 0)
            if status in ["in_review", "rejected"]:
                qa_session += 1

            # An "approved" write is refused until every pre-approval
            # guard is satisfied -- see `_approval_refusal_reason` for what
            # each one catches and why (#1396, #1421, #1496). Pulled into one
            # helper (rather than inlined here) so this function's own
            # branch/return count stays readable -- the ratchet caught the
            # #1496 guard pushing update_qa_status over the complexity caps.
            if status == "approved":
                refusal = _approval_refusal_reason(get_spec_dir(), get_project_dir())
                if refusal:
                    return {"content": [{"type": "text", "text": refusal}]}

            plan["qa_signoff"] = {
                "status": status,
                "qa_session": qa_session,
                "issues_found": issues,
                "tests_passed": tests_passed,
                "timestamp": datetime.now(UTC).isoformat(),
                "ready_for_qa_revalidation": status == "fixes_applied",
            }

            # Update plan status to match QA result
            # This ensures the UI shows the correct column after QA
            if status == "approved":
                plan["status"] = "human_review"
                plan["planStatus"] = "review"
                plan["reviewReason"] = "completed"
            elif status == "rejected":
                plan["status"] = "human_review"
                plan["planStatus"] = "review"
                plan["reviewReason"] = "qa_issues"

            plan["last_updated"] = datetime.now(UTC).isoformat()

            with open(plan_file, "w") as f:
                json.dump(plan, f, indent=2)

            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Updated QA status to '{status}' (session {qa_session})",
                    }
                ]
            }

        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error updating QA status: {e}"}]
            }

    tools.append(update_qa_status)

    return tools
