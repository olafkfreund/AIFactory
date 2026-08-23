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

try:
    from claude_agent_sdk import tool

    SDK_TOOLS_AVAILABLE = True
except ImportError:
    SDK_TOOLS_AVAILABLE = False
    tool = None


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
            ["git", "rev-list", "--count", "HEAD", "--not", "--remotes=origin"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
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

            # #1396: "approved" requires something to have been built.
            #
            # A build that produced nothing was signed off with
            # `tests_passed: {"unit": "1/1"}` and `verified_by: "qa_agent"`.
            # The branch had zero commits, the worktree was clean, and the
            # files existed on no branch and in no commit -- but the Job
            # reported SuccessCriteriaMet, the progress bar read 2/2, and every
            # layer that reports upward said done and tested. Only reading the
            # branch by hand contradicted it.
            #
            # `tests_passed` is the sharpest part: not a status someone forgot
            # to update, but a positive claim about a test run that never
            # happened, written into the artifact downstream consumers trust.
            if status == "approved":
                unbuilt = _nothing_was_built(get_project_dir())
                if unbuilt:
                    return {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Refusing to approve: nothing was built. "
                                    f"{unbuilt} Approving here would record a "
                                    "passing QA sign-off, and test results, for "
                                    "code that does not exist (#1396). Implement "
                                    "the change first; if the task genuinely "
                                    "requires no code change, say so rather than "
                                    "signing off."
                                ),
                            }
                        ]
                    }

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
