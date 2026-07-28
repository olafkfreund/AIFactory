"""
Magestic AI CLI - Main Entry Point
===================================

Command-line interface for the Magestic AI autonomous coding framework.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

# Ensure parent directory is in path for imports (before other imports)
_PARENT_DIR = Path(__file__).parent.parent
if str(_PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(_PARENT_DIR))


from .batch_commands import (
    handle_batch_cleanup_command,
    handle_batch_create_command,
    handle_batch_status_command,
)
from .build_commands import handle_build_command
from .followup_commands import handle_followup_command
from .qa_commands import (
    handle_qa_command,
    handle_qa_status_command,
    handle_review_status_command,
)
from .spec_commands import print_specs_list
from .utils import (
    DEFAULT_MODEL,
    find_spec,
    get_project_dir,
    print_banner,
    setup_environment,
)
from .workspace_commands import (
    handle_cleanup_worktrees_command,
    handle_discard_command,
    handle_list_worktrees_command,
    handle_merge_command,
    handle_review_command,
)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Magestic AI Framework - Autonomous multi-session coding agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all specs
  python aifactory/run.py --list

  # Run a specific spec (by number or full name)
  python aifactory/run.py --spec 001
  python aifactory/run.py --spec 001-initial-app

  # Workspace management (after build completes)
  python aifactory/run.py --spec 001 --merge     # Add build to your project
  python aifactory/run.py --spec 001 --review    # See what was built
  python aifactory/run.py --spec 001 --discard   # Delete build (with confirmation)

  # Advanced options
  python aifactory/run.py --spec 001 --direct       # Skip workspace isolation
  python aifactory/run.py --spec 001 --isolated     # Force workspace isolation

  # Status checks
  python aifactory/run.py --spec 001 --review-status  # Check human review status
  python aifactory/run.py --spec 001 --qa-status      # Check QA validation status

Prerequisites:
  1. Create a spec first: claude /spec
  2. Run 'claude setup-token' and set CLAUDE_CODE_OAUTH_TOKEN

Environment Variables:
  CLAUDE_CODE_OAUTH_TOKEN  Your Claude Code OAuth token (required)
                           Get it by running: claude setup-token
  AUTO_BUILD_MODEL         Override default model (optional)
        """,
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="List all available specs and their status",
    )

    parser.add_argument(
        "--spec",
        type=str,
        default=None,
        help="Spec to run (e.g., '001' or '001-feature-name')",
    )

    parser.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help="Project directory (default: current working directory)",
    )

    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Maximum number of agent sessions (default: unlimited)",
    )

    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=f"Claude model to use (default: {DEFAULT_MODEL})",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output",
    )

    # Workspace options
    workspace_group = parser.add_mutually_exclusive_group()
    workspace_group.add_argument(
        "--isolated",
        action="store_true",
        help="Force building in isolated workspace (safer)",
    )
    workspace_group.add_argument(
        "--direct",
        action="store_true",
        help="Build directly in your project (no isolation)",
    )

    # Build management commands
    build_group = parser.add_mutually_exclusive_group()
    build_group.add_argument(
        "--merge",
        action="store_true",
        help="Merge an existing build into your project",
    )
    build_group.add_argument(
        "--review",
        action="store_true",
        help="Review what an existing build contains",
    )
    build_group.add_argument(
        "--discard",
        action="store_true",
        help="Discard an existing build (requires confirmation)",
    )

    # Parallel execution options (#376): run independent subtasks concurrently
    # in dependency-graph waves instead of strictly one-at-a-time.
    parser.add_argument(
        "--parallel",
        action="store_true",
        help=(
            "Run independent subtasks concurrently in dependency-graph waves "
            "(requires a parallel_safe phase in the plan; falls back to serial "
            "for unsafe phases). See --workers."
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Max subtasks to run concurrently when --parallel is set "
            "(default: 3). Ignored without --parallel."
        ),
    )

    # Merge options
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="With --merge: stage changes but don't commit (review in IDE first)",
    )
    parser.add_argument(
        "--merge-preview",
        action="store_true",
        help="Preview merge conflicts without actually merging (returns JSON)",
    )

    # QA options
    parser.add_argument(
        "--qa",
        action="store_true",
        help="Run QA validation loop on a completed build",
    )
    parser.add_argument(
        "--qa-status",
        action="store_true",
        help="Show QA validation status for a spec",
    )
    parser.add_argument(
        "--skip-qa",
        action="store_true",
        help="Skip automatic QA validation after build completes",
    )
    parser.add_argument(
        "--stop-after-planning",
        action="store_true",
        help="Run only the planner phase, then exit (used by Copilot delegation)",
    )
    parser.add_argument(
        "--remote-control",
        type=str,
        default=None,
        metavar="SESSION_NAME",
        help=(
            "Claude Code Remote Control session name. Passed through to the "
            "underlying claude CLI via the SDK's extra_args, which registers "
            "a Remote Control session with this name on Anthropic's API. The "
            "session appears in claude.ai/code's session list under this "
            "name so the user can drive the same conversation from any "
            "device. Requires a full-scope claude auth login on the host "
            '(see CLAUDE.md "Remote Control" section).'
        ),
    )

    # Follow-up options
    parser.add_argument(
        "--followup",
        action="store_true",
        help="Add follow-up tasks to a completed spec (extends existing implementation plan)",
    )

    # Review options
    parser.add_argument(
        "--review-status",
        action="store_true",
        help="Show human review/approval status for a spec",
    )

    # Non-interactive mode (for UI/automation)
    parser.add_argument(
        "--auto-continue",
        action="store_true",
        help="Non-interactive mode: auto-continue existing builds, skip prompts (for UI integration)",
    )

    # MCP diagnostics
    parser.add_argument(
        "--mcp-doctor",
        action="store_true",
        help="Print MCP catalog × credentials matrix (use with --project-dir for per-project markers)",
    )

    # Worktree management
    parser.add_argument(
        "--list-worktrees",
        action="store_true",
        help="List all spec worktrees and their status",
    )
    parser.add_argument(
        "--cleanup-worktrees",
        action="store_true",
        help="Remove all spec worktrees and their branches (with confirmation)",
    )

    # Force bypass
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip approval check and start build anyway (for debugging)",
    )

    # Base branch for worktree creation
    parser.add_argument(
        "--base-branch",
        type=str,
        default=None,
        help="Base branch for creating worktrees (default: auto-detect or current branch)",
    )

    # Batch task management
    parser.add_argument(
        "--batch-create",
        type=str,
        default=None,
        metavar="FILE",
        help="Create multiple tasks from a batch JSON file",
    )
    parser.add_argument(
        "--batch-status",
        action="store_true",
        help="Show status of all specs in the project",
    )
    parser.add_argument(
        "--batch-cleanup",
        action="store_true",
        help="Clean up completed specs (dry-run by default)",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Actually delete files in cleanup (not just preview)",
    )

    return parser.parse_args()


def main() -> None:
    """Main CLI entry point."""
    # Set up environment first
    setup_environment()

    # Parse arguments
    args = parse_args()

    # Import debug functions after environment setup
    from debug import debug, debug_error, debug_section, debug_success

    debug_section("run.py", "Starting Auto-Build Framework")
    debug("run.py", "Arguments parsed", args=vars(args))

    # Determine project directory
    project_dir = get_project_dir(args.project_dir)
    debug("run.py", f"Using project directory: {project_dir}")

    # Get model from CLI arg or env var (None if not explicitly set).
    # This allows get_phase_model() to fall back to task_metadata.json — so the
    # value is intentionally Optional[str], NOT defaulted to DEFAULT_MODEL (a
    # truthy cli_model would wrongly out-prioritise a non-auto profile's saved
    # model in get_phase_model). The handlers below accept this None via
    # get_phase_model(cli_model: str | None); the type: ignore[arg-type] markers
    # on those calls record that intentional contract.
    model = args.model or os.environ.get("AUTO_BUILD_MODEL")

    # Handle --mcp-doctor command (no spec needed, no banner — output is plain operator diagnostics)
    if args.mcp_doctor:
        from .mcp_commands import handle_mcp_doctor_command

        # If --project-dir wasn't explicitly passed, pass None so the doctor
        # skips per-project marker resolution (otherwise it'd scan the CWD,
        # which is rarely what an operator running mcp-doctor wants — they
        # want to see "what would work on this machine" first).
        explicit_project = project_dir if args.project_dir is not None else None
        rc = handle_mcp_doctor_command(explicit_project)
        sys.exit(rc)

    # Handle --list command
    if args.list:
        print_banner()
        print_specs_list(project_dir)
        return

    # Handle --list-worktrees command
    if args.list_worktrees:
        handle_list_worktrees_command(project_dir)
        return

    # Handle --cleanup-worktrees command
    if args.cleanup_worktrees:
        handle_cleanup_worktrees_command(project_dir)
        return

    # Handle batch commands
    if args.batch_create:
        handle_batch_create_command(args.batch_create, str(project_dir))
        return

    if args.batch_status:
        handle_batch_status_command(str(project_dir))
        return

    if args.batch_cleanup:
        handle_batch_cleanup_command(str(project_dir), dry_run=not args.no_dry_run)
        return

    # Require --spec if not listing
    if not args.spec:
        print_banner()
        print("\nError: --spec is required")
        print("\nUsage:")
        print("  python aifactory/run.py --list           # See all specs")
        print("  python aifactory/run.py --spec 001       # Run a spec")
        print("\nCreate a new spec with:")
        print("  claude /spec")
        sys.exit(1)

    # RFC-0017 #190 (consumer): a packed-workspace Job carries WORKSPACE_URI (set
    # by the build_backend producer when AIFACTORY_PACK_WORKSPACE is on). In that
    # case /work is NOT populated by an RWO co-mount — reconstitute it from object
    # storage BEFORE the spec is resolved and the build runs below. No-op (returns
    # False) on the single-node co-mount path, so today's behaviour is unchanged.
    from core.workspace_fetch import maybe_unpack_workspace  # noqa: PLC0415

    # Make the packed-path push/fetch telemetry VISIBLE in the Job log.
    #
    # Nothing configures logging in this entrypoint, so the root logger sits at
    # Python's default WARNING and every `_log.info` in workspace_fetch is
    # dropped. The build's own progress uses print(), which is why the log looks
    # complete while saying nothing about whether an artefact was pushed.
    #
    # That cost two builds during #1038: the only workspace_fetch line ever seen
    # was "branch push failed", purely because it is a warning. A diagnostic that
    # cannot be read is not a diagnostic, so this raises exactly one logger
    # rather than turning on INFO globally (which would drown the log in library
    # chatter).
    logging.getLogger("core.workspace_fetch").setLevel(logging.INFO)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    maybe_unpack_workspace(project_dir)

    # Find the spec
    debug("run.py", "Finding spec", spec_identifier=args.spec)
    spec_dir = find_spec(project_dir, args.spec)
    if not spec_dir:
        debug_error("run.py", "Spec not found", spec=args.spec)
        print_banner()
        print(f"\nError: Spec '{args.spec}' not found")
        print("\nAvailable specs:")
        # auto_create=False: never launch the interactive QUICK START wizard
        # here. The build runner was asked for a *specific* spec; if it's
        # missing it must fail fast. Leaving auto_create at its default (True)
        # made `print_specs_list` call input() — which blocks forever when run
        # headlessly by agent_service (no TTY, stdin never sends EOF), the root
        # cause of tasks hanging at 0% in "planning" with no progress.
        print_specs_list(project_dir, auto_create=False)
        sys.exit(1)

    debug_success("run.py", "Spec found", spec_dir=str(spec_dir))

    # Handle build management commands
    if args.merge_preview:
        from cli.workspace_commands import handle_merge_preview_command

        result = handle_merge_preview_command(
            project_dir, spec_dir.name, base_branch=args.base_branch
        )
        # Output as JSON for the UI to parse
        import json

        print(json.dumps(result))
        return

    if args.merge:
        success = handle_merge_command(
            project_dir,
            spec_dir.name,
            no_commit=args.no_commit,
            base_branch=args.base_branch,
        )
        if not success:
            sys.exit(1)
        return

    if args.review:
        handle_review_command(project_dir, spec_dir.name)
        return

    if args.discard:
        handle_discard_command(project_dir, spec_dir.name)
        return

    # Handle QA commands
    if args.qa_status:
        handle_qa_status_command(spec_dir)
        return

    if args.review_status:
        handle_review_status_command(spec_dir)
        return

    if args.qa:
        handle_qa_command(
            project_dir=project_dir,
            spec_dir=spec_dir,
            model=model,  # type: ignore[arg-type]  # None-able by design (see model assignment above)
            verbose=args.verbose,
        )
        return

    # Handle --followup command
    if args.followup:
        handle_followup_command(
            project_dir=project_dir,
            spec_dir=spec_dir,
            model=model,  # type: ignore[arg-type]  # None-able by design (see model assignment above)
            verbose=args.verbose,
        )
        return

    # Normal build flow
    handle_build_command(
        project_dir=project_dir,
        spec_dir=spec_dir,
        model=model,  # type: ignore[arg-type]  # None-able by design (see model assignment above)
        max_iterations=args.max_iterations,
        verbose=args.verbose,
        force_isolated=args.isolated,
        force_direct=args.direct,
        auto_continue=args.auto_continue,
        skip_qa=args.skip_qa,
        force_bypass_approval=args.force,
        base_branch=args.base_branch,
        stop_after_planning=args.stop_after_planning,
        remote_control_session=args.remote_control,
        parallel=args.parallel,
        workers=args.workers,
    )

    # RFC-0017 #190 (producer push-back): on the packed multi-node path ``/work``
    # is an ephemeral emptyDir that dies with the Job, so persist the built branch
    # to origin HERE — the control-plane handoff/PR-endgame push reads the
    # control-plane data-PVC worktree, which the packed path never populates, and
    # would otherwise degrade to ``main`` (losing the build). No-op on the co-mount
    # path (WORKSPACE_URI unset) where ``/work`` survives on the data PVC. Skipped
    # for planning-only runs (no build branch yet).
    if not args.stop_after_planning:
        from core.workspace_fetch import (  # noqa: PLC0415
            maybe_push_memory,
            maybe_push_plan,
            maybe_push_task_logs,
            maybe_push_usage,
            maybe_push_workspace_branch,
        )

        maybe_push_workspace_branch(project_dir, spec_dir.name)
        # #852: the same gap, for the file that decides whether the build is
        # considered successful at all. The plan here records each subtask
        # completed; the control plane counts them from the data-PVC copy the
        # packed path never touches, sees 0, and escalates every green build to
        # human_review (#287 guard on stale input).
        maybe_push_plan(spec_dir, spec_dir.name)
        # #1038: the SAME gap, for the spec's memory/ tree. Session insights are
        # written into the Job's ephemeral /work and, without this, die with the
        # pod — which is why the fleet's memory never accumulated and why three
        # earlier fixes (#1031/#1036/#1037) all failed: each assumed /work was
        # durable. It is an emptyDir on the packed path (core/job_dispatch.py).
        maybe_push_memory(spec_dir, spec_dir.name)
        # Same packed-path propagation gap: token_usage.json is written here in the
        # Job's ephemeral /work but the control-plane completion emitter reads the
        # data-PVC spec dir. Push it so CFactory gets the token usage (#190).
        maybe_push_usage(spec_dir, spec_dir.name)
        # W1 (Factory #218): task_logs.json carries the authoritative per-phase
        # status; push it so the control plane reports done/failed instead of
        # leaving the task stuck at backlog/queued.
        maybe_push_task_logs(spec_dir, spec_dir.name)


if __name__ == "__main__":
    main()
