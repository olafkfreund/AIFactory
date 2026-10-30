"""
QA Commands
===========

CLI commands for QA validation (run QA, check status)
"""

import asyncio
import sys
from pathlib import Path

# Ensure parent directory is in path for imports (before other imports)
_PARENT_DIR = Path(__file__).parent.parent
if str(_PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(_PARENT_DIR))

from progress import count_subtasks
from qa_loop import (
    is_qa_approved,
    print_qa_status,
    run_qa_validation_loop,
    should_run_qa,
)
from review import ReviewState, display_review_status
from ui import (
    Icons,
    icon,
    info,
    success,
    warning,
)

from .utils import print_banner, validate_environment


def _source_project_dir(project_dir: Path) -> Path | None:
    """The project owning ``project_dir`` (#1955).

    A task worktree root maps to its source project; any other path under
    ``.aifactory/worktrees`` maps to None, so the caller fails closed.
    """
    parts = project_dir.resolve().parts
    index = next(
        (
            i
            for i in range(len(parts) - 1)
            if parts[i : i + 2] == (".aifactory", "worktrees")
        ),
        None,
    )
    if index is None:
        return project_dir
    if len(parts) == index + 4 and parts[index + 2] == "tasks":
        return Path(*parts[:index])
    return None


def handle_qa_status_command(spec_dir: Path) -> None:
    """
    Handle the --qa-status command.

    Args:
        spec_dir: Spec directory path
    """
    print_banner()
    print(f"\nSpec: {spec_dir.name}\n")
    print_qa_status(spec_dir)


def handle_review_status_command(spec_dir: Path) -> None:
    """
    Handle the --review-status command.

    Args:
        spec_dir: Spec directory path
    """
    print_banner()
    print(f"\nSpec: {spec_dir.name}\n")
    display_review_status(spec_dir)
    # Also show if approval is valid for build
    review_state = ReviewState.load(spec_dir)
    print()
    if review_state.is_approval_valid(spec_dir):
        print(success(f"{icon(Icons.SUCCESS)} Ready to build - approval is valid."))
    elif review_state.approved:
        print(
            warning(
                f"{icon(Icons.WARNING)} Spec changed since approval - re-review required."
            )
        )
    else:
        print(info(f"{icon(Icons.INFO)} Review required before building."))
    print()


def handle_qa_command(
    project_dir: Path,
    spec_dir: Path,
    model: str,
    verbose: bool = False,
) -> None:
    """
    Handle the --qa command (run QA validation loop).

    Args:
        project_dir: Project root directory
        spec_dir: Spec directory path
        model: Model to use for QA
        verbose: Enable verbose output
    """
    print_banner()
    print(f"\nRunning QA validation for: {spec_dir.name}")
    if not validate_environment(spec_dir):
        sys.exit(1)

    # Check if there's pending human feedback that needs to be processed
    # Human feedback takes priority over "already approved" status
    fix_request_file = spec_dir / "QA_FIX_REQUEST.md"
    has_human_feedback = fix_request_file.exists()

    if not should_run_qa(spec_dir) and not has_human_feedback:
        if is_qa_approved(spec_dir):
            print("\n✅ Build already approved by QA.")
        else:
            completed, total = count_subtasks(spec_dir)
            print(f"\n❌ Build not complete ({completed}/{total} subtasks).")
            print("Complete all subtasks before running QA validation.")
        return

    if has_human_feedback:
        print("\n📝 Human feedback detected - processing fix request...")

    # A fresh process no longer inherits plan grants from the profile file (#1944),
    # so seed them here, from the plan only while a required review is still valid.
    try:
        from project.analyzer import (  # noqa: PLC0415 - match the coder loop's local import
            seed_profile_with_plan_commands,
        )
        from review.state import (  # noqa: PLC0415 - same
            requires_review_before_coding,
        )
        from spec.pipeline import get_specs_dir  # noqa: PLC0415 - same

        # An approval valid now means the live plan is the approved one.
        # #1955: gate worktree plans against the source spec, never an agent-writable copy.
        root = _source_project_dir(project_dir)
        gate_dir = root and get_specs_dir(root) / spec_dir.name
        plan: dict[str, object] | None = (
            None
            if gate_dir
            and gate_dir.is_dir()
            and (
                not requires_review_before_coding(gate_dir)
                or ReviewState.load(gate_dir).is_approval_valid(spec_dir)
            )
            else {}
        )
        seed_profile_with_plan_commands(
            project_dir, spec_dir / "implementation_plan.json", plan=plan
        )
    except Exception as exc:  # noqa: BLE001 - never let allowlist seeding break QA
        print(f"plan-commands seed skipped: {exc}")  # noqa: T201 - console UX

    try:
        approved = asyncio.run(
            run_qa_validation_loop(
                project_dir=project_dir,
                spec_dir=spec_dir,
                model=model,
                verbose=verbose,
            )
        )
        if approved:
            print("\n✅ QA validation passed. Ready for merge.")
        else:
            print("\n❌ QA validation incomplete. See reports for details.")
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nQA validation paused.")
        print(f"Resume with: python aifactory/run.py --spec {spec_dir.name} --qa")
