"""
Build Commands
==============

CLI commands for building specs and handling the main build flow.
"""

import asyncio
import json
import sys
from pathlib import Path

# Ensure parent directory is in path for imports (before other imports)
_PARENT_DIR = Path(__file__).parent.parent
if str(_PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(_PARENT_DIR))

# Import only what we need at module level
# Heavy imports are lazy-loaded in functions to avoid import errors
from progress import count_subtasks, print_paused_banner
from review import ReviewState, requires_review_before_coding
from ui import (
    BuildState,
    Icons,
    MenuOption,
    StatusManager,
    bold,
    box,
    highlight,
    icon,
    muted,
    print_status,
    select_menu,
    success,
    warning,
)
from workspace import (
    WorkspaceMode,
    check_existing_build,
    choose_workspace,
    finalize_workspace,
    get_existing_build_worktree,
    handle_workspace_choice,
    setup_workspace,
)

from .input_handlers import (
    read_from_file,
    read_multiline_input,
)


def build_is_silent_noop(spec_dir: Path, work_dir: Path | None = None) -> bool:
    """True when a finished agent run implemented NOTHING (#779, #1422).

    A run whose plan has subtasks but ZERO completed produced no code — every
    subtask failed or was never started. Exiting 0 in that state makes a
    headless build Job report Complete with an empty branch, which is worse
    than a hard failure. Legitimate 0-completed exits are excluded:

    - PAUSE file present (human paused the build)
    - plan status ``human_review`` (paused for plan approval)

    #1422: the subtask count alone is not enough. A run reported "All subtasks
    completed!", pushed its branch and advanced its card while the branch tip was
    byte-identical to its base — the bookkeeping said done and the worktree had
    gained nothing. Counting completed subtasks measures what the agent claimed;
    it does not measure what it produced. So when ``work_dir`` is given, a run
    that completed subtasks must ALSO show git output to escape this guard.
    """
    completed, total = count_subtasks(spec_dir)
    if total == 0:
        return False
    if completed > 0:
        if work_dir is None:
            return False
        # Imported here, not at module level: importing ``agents`` at import time
        # regresses the strict-import ratchet (see the PAUSE note below). Reused
        # rather than reimplemented so the two callers cannot drift apart.
        from agents.tools_pkg.tools.qa import _nothing_was_built

        if _nothing_was_built(work_dir) is None:
            return False
    # "PAUSE" mirrors agents.base.HUMAN_INTERVENTION_FILE (not imported here:
    # module-level agents imports would regress the strict-import ratchet).
    if (spec_dir / "PAUSE").exists():
        return False
    try:
        plan: dict = json.loads(
            (spec_dir / "implementation_plan.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        plan = {}
    return plan.get("status") != "human_review"


def handle_build_command(
    project_dir: Path,
    spec_dir: Path,
    model: str,
    max_iterations: int | None,
    verbose: bool,
    force_isolated: bool,
    force_direct: bool,
    auto_continue: bool,
    skip_qa: bool,
    force_bypass_approval: bool,
    base_branch: str | None = None,
    stop_after_planning: bool = False,
    remote_control_session: str | None = None,
    parallel: bool = False,
    workers: int | None = None,
) -> None:
    """
    Handle the main build command.

    Args:
        project_dir: Project root directory
        spec_dir: Spec directory path
        model: Model to use (used as default; may be overridden by task_metadata.json)
        max_iterations: Maximum number of iterations (None for unlimited)
        verbose: Enable verbose output
        force_isolated: Force isolated workspace mode
        force_direct: Force direct workspace mode
        auto_continue: Auto-continue mode (non-interactive)
        skip_qa: Skip automatic QA validation
        force_bypass_approval: Force bypass approval check
        base_branch: Base branch for worktree creation (default: current branch)
        stop_after_planning: Exit cleanly after the planner phase writes
            implementation_plan.json. Used by the Copilot delegation flow,
            where AIFactory generates the plan locally and then hands off
            implementation to GitHub Copilot Coding Agent (#92, #94).
        parallel: Run independent subtasks concurrently in dependency-graph
            waves (#376). Falls back to serial execution for phases that are
            not marked parallel_safe.
        workers: Max concurrent subtasks when ``parallel`` is set (default 3).
    """
    # Lazy imports to avoid loading heavy modules
    from agent import run_autonomous_agent, sync_plan_to_source
    from debug import (
        debug,
        debug_info,
        debug_section,
        debug_success,
    )
    from phase_config import get_phase_model
    from phase_event import ExecutionPhase, emit_phase
    from qa_loop import is_qa_approved, run_qa_validation_loop, should_run_qa
    from solo_mode import is_solo_mode_enabled_for_spec

    from .utils import print_banner, validate_environment

    # Solo mode (#276): the single self-directed agent is its own QA, so the
    # separate QA validation loop is skipped. This is the token-saving path.
    # Default OFF — when disabled the full planner -> coder -> QA flow runs
    # exactly as before.
    if is_solo_mode_enabled_for_spec(spec_dir):
        skip_qa = True

    # Get the resolved model for the planning phase (first phase of build)
    # This respects task_metadata.json phase configuration from the UI
    planning_model = get_phase_model(spec_dir, "planning", model)
    coding_model = get_phase_model(spec_dir, "coding", model)
    qa_model = get_phase_model(spec_dir, "qa", model)

    print_banner()
    print(f"\nProject directory: {project_dir}")
    print(f"Spec: {spec_dir.name}")
    # Show phase-specific models if they differ
    if planning_model != coding_model or coding_model != qa_model:
        print(
            f"Models: Planning={planning_model.split('-')[1] if '-' in planning_model else planning_model}, "
            f"Coding={coding_model.split('-')[1] if '-' in coding_model else coding_model}, "
            f"QA={qa_model.split('-')[1] if '-' in qa_model else qa_model}"
        )
    else:
        print(f"Model: {planning_model}")

    if max_iterations:
        print(f"Max iterations: {max_iterations}")
    else:
        print("Max iterations: Unlimited (runs until all subtasks complete)")

    print()

    # Validate environment (credential presence + spec.md, etc.)
    if not validate_environment(spec_dir):
        sys.exit(1)

    # Auth pre-flight (#611 / RFC-0008 §3.2a): a live, generation-free probe of
    # the provider credential before the (expensive) build. Catches an expired
    # token that is *present* but invalid — the silent-empty-build failure from
    # the 2026-06-18 taskboard demo. Mode via AIFACTORY_AUTH_PREFLIGHT:
    # off / warn (default, never blocks) / enforce (abort on a definitive 401).
    from core.auth_preflight import preflight_mode, run_auth_preflight

    _pf_mode = preflight_mode()
    if _pf_mode != "off":
        for _r in run_auth_preflight([planning_model, coding_model, qa_model]):
            if _r.status == "ok":
                print(f"Auth pre-flight: {_r.provider} OK")
            elif _r.is_auth_failure:
                print(f"Auth pre-flight: {_r.provider} FAILED — {_r.detail}")
                if _pf_mode == "enforce":
                    print(
                        "Aborting before build (AIFACTORY_AUTH_PREFLIGHT=enforce). "
                        "Rotate/refresh the credential and retry."
                    )
                    sys.exit(1)
            elif _r.status == "inconclusive":
                print(
                    f"Auth pre-flight: {_r.provider} inconclusive "
                    f"({_r.detail}) — proceeding"
                )

    # Check human review approval
    review_state = ReviewState.load(spec_dir)
    if not review_state.is_approval_valid(spec_dir):
        if force_bypass_approval:
            # #916: only cry "bypassing approval" when there is genuinely an
            # approval requirement to bypass. A spec with no
            # requireReviewBeforeCoding has nothing to approve, so --force on it
            # skips a gate that would never have fired — announcing that as a
            # WARNING on every such build is noise that trains readers to ignore
            # the one case that matters. Same question the coder gates on
            # (requires_review_before_coding), so the two cannot drift.
            if requires_review_before_coding(spec_dir):
                # A real requirement, deliberately bypassed here. Be precise
                # about scope: --force only skips THIS pre-flight check; the
                # coder re-checks approval before writing code and will still
                # pause (agents/coder.py), so this is not a free pass.
                print()
                print(
                    warning(
                        f"{icon(Icons.WARNING)} WARNING: Bypassing approval check with --force"
                    )
                )
                print(
                    muted(
                        "This spec requires review before coding and has not been "
                        "approved. --force skips this pre-flight check only — the "
                        "coder still gates on approval before writing code."
                    )
                )
                print()
        else:
            print()
            content = [
                bold(f"{icon(Icons.WARNING)} BUILD BLOCKED - REVIEW REQUIRED"),
                "",
                "This spec requires human approval before building.",
            ]

            if review_state.approved and not review_state.is_approval_valid(spec_dir):
                # Spec changed after approval
                content.append("")
                content.append(warning("The spec has been modified since approval."))
                content.append("Please re-review and re-approve.")

            content.extend(
                [
                    "",
                    highlight("To review and approve:"),
                    f"  python aifactory/review.py --spec-dir {spec_dir}",
                    "",
                    muted("Or use --force to bypass this check (not recommended)."),
                ]
            )
            print(box(content, width=70, style="heavy"))
            print()

            # If auto_continue mode (web UI), save pending review state and exit cleanly
            if auto_continue:
                # Save review state indicating spec is waiting for approval
                review_state.save(spec_dir)
                # Exit with success code - web UI will handle the human_review transition
                sys.exit(0)
            else:
                # CLI mode - exit with error to block execution
                sys.exit(1)
    else:
        debug_success(
            "run.py", "Review approval validated", approved_by=review_state.approved_by
        )

    # Check for existing build
    if get_existing_build_worktree(project_dir, spec_dir.name):
        if auto_continue:
            # Non-interactive mode: auto-continue with existing build
            debug("run.py", "Auto-continue mode: continuing with existing build")
            print("Auto-continue: Resuming existing build...")
        else:
            continue_existing = check_existing_build(project_dir, spec_dir.name)
            if continue_existing:
                # Continue with existing worktree
                pass
            else:
                # User chose to start fresh or merged existing
                pass

    # Choose workspace (skip for parallel mode - it always uses worktrees)
    working_dir = project_dir
    worktree_manager = None
    source_spec_dir = None  # Track original spec dir for syncing back from worktree

    # Let user choose workspace mode (or auto-select if --auto-continue)
    workspace_mode = choose_workspace(
        project_dir,
        spec_dir.name,
        force_isolated=force_isolated,
        force_direct=force_direct,
        auto_continue=auto_continue,
    )

    if workspace_mode == WorkspaceMode.ISOLATED:
        # Keep reference to original spec directory for syncing progress back
        source_spec_dir = spec_dir

        working_dir, worktree_manager, localized_spec_dir = setup_workspace(
            project_dir,
            spec_dir.name,
            workspace_mode,
            source_spec_dir=spec_dir,
            base_branch=base_branch,
        )
        # Use the localized spec directory (inside worktree) for AI access
        if localized_spec_dir:
            spec_dir = localized_spec_dir

        # RFC-0010: for a language migration, mount the legacy source as a
        # read-only reference oracle and scaffold the target crate/dir, so the
        # coder generates in the target language against the original instead of
        # editing it in place. No-op + never fatal for non-migration builds.
        try:
            from core.migration_mapper import (
                is_migration,
                load_contract,
                prepare_migration_workspace,
            )

            _contract = load_contract(spec_dir)
            if is_migration(_contract):
                summary = prepare_migration_workspace(
                    working_dir, project_dir, _contract
                )
                print_status(
                    f"RFC-0010 migration: target={summary.get('target_language')}, "
                    f"oracle mounted, "
                    f"{len(summary.get('scaffolded', []))} target file(s) scaffolded",
                    "progress",
                )
        except Exception as exc:  # noqa: BLE001 — migration prep must not break a build
            debug("run.py", "migration workspace prep skipped", error=str(exc))

    # Run the autonomous agent
    debug_section("run.py", "Starting Build Execution")
    debug(
        "run.py",
        "Build configuration",
        model=model,
        workspace_mode=str(workspace_mode),
        working_dir=str(working_dir),
        spec_dir=str(spec_dir),
    )

    try:
        debug("run.py", "Starting agent execution")

        asyncio.run(
            run_autonomous_agent(
                project_dir=working_dir,  # Use worktree if isolated
                spec_dir=spec_dir,
                model=model,
                max_iterations=max_iterations,
                verbose=verbose,
                source_spec_dir=source_spec_dir,  # For syncing progress back to main project
                stop_after_planning=stop_after_planning,
                remote_control_session=remote_control_session,
                parallel=parallel,
                workers=workers,
            )
        )
        debug_success("run.py", "Agent execution completed")

        # Delegation mode: planner has written implementation_plan.json and
        # we hand off to the vendor agent (Copilot) from auto_fix_service.
        # No QA, no finalization — auto_fix_service drives the rest.
        if stop_after_planning:
            debug_info(
                "run.py",
                "Stop-after-planning: planner done, returning to delegation caller",
            )
            return

        # Silent no-op guard (#779): a run that finished with 0/N subtasks
        # completed and is not paused produced no code. Fail the process so a
        # headless build Job is marked Failed instead of Complete — a silent
        # no-op build (codex agentic auth mismatch, dead provider, ...) must
        # surface as a failure, not as a green build with an empty branch.
        if build_is_silent_noop(spec_dir, working_dir):
            _completed, _total = count_subtasks(spec_dir)
            emit_phase(
                ExecutionPhase.FAILED,
                f"Build produced no code: {_completed}/{_total} subtasks completed",
            )
            # Two distinct shapes, and saying which one saves the reader a guess:
            # nothing ran at all, or things claimed to run and left no trace.
            _detail = (
                f"no subtasks completed ({_completed}/{_total})"
                if _completed == 0
                else (
                    f"{_completed}/{_total} subtasks reported complete but the "
                    "worktree has no commits beyond its base and no uncommitted "
                    "changes (#1422)"
                )
            )
            print_status(
                f"BUILD FAILED - {_detail}. "
                "The coder implemented nothing; check the coding-provider "
                "credentials/model and the session logs.",
                "error",
            )
            sys.exit(1)

        # Run QA validation BEFORE finalization (while worktree still exists)
        # QA must sign off before the build is considered complete
        qa_approved = True  # Default to approved if QA is skipped
        if not skip_qa and should_run_qa(spec_dir):
            print("\n" + "=" * 70)
            print("  SUBTASKS COMPLETE - STARTING QA VALIDATION")
            print("=" * 70)
            print("\nAll subtasks completed. Now running QA validation loop...")
            print("This ensures production-quality output before sign-off.\n")

            try:
                qa_approved = asyncio.run(
                    run_qa_validation_loop(
                        project_dir=working_dir,
                        spec_dir=spec_dir,
                        model=model,
                        verbose=verbose,
                    )
                )

                if qa_approved:
                    print("\n" + "=" * 70)
                    print("  ✅ QA VALIDATION PASSED")
                    print("=" * 70)
                    print("\nAll acceptance criteria verified.")
                    print("The implementation is production-ready.\n")
                else:
                    print("\n" + "=" * 70)
                    print("  ⚠️  QA VALIDATION INCOMPLETE")
                    print("=" * 70)
                    print("\nSome issues require manual attention.")
                    print(f"See: {spec_dir / 'qa_report.md'}")
                    print(f"Or:  {spec_dir / 'QA_FIX_REQUEST.md'}")
                    print(
                        f"\nResume QA: python aifactory/run.py --spec {spec_dir.name} --qa\n"
                    )

                # Sync implementation plan to main project after QA
                # This ensures the main project has the latest status (human_review)
                if sync_plan_to_source(spec_dir, source_spec_dir):
                    debug_info(
                        "run.py", "Implementation plan synced to main project after QA"
                    )
            except KeyboardInterrupt:
                print("\n\nQA validation paused.")
                print(f"Resume: python aifactory/run.py --spec {spec_dir.name} --qa")
                qa_approved = False

        elif not skip_qa and is_qa_approved(spec_dir):
            # QA was pre-approved by coder agent - emit phase events for proper logging
            emit_phase(
                ExecutionPhase.QA_REVIEW, "QA pre-approved by coder agent", progress=100
            )

            print("\n" + "=" * 70)
            print("  QA PRE-APPROVED BY CODER")
            print("=" * 70)
            print("\nThe coder agent has validated all acceptance criteria.")
            print(
                "Implementation meets requirements - no additional QA review needed.\n"
            )

            emit_phase(ExecutionPhase.COMPLETE, "QA validation passed (pre-approved)")

            # Sync implementation plan to main project
            if sync_plan_to_source(spec_dir, source_spec_dir):
                debug_info(
                    "run.py",
                    "Implementation plan synced to main project after pre-approved QA",
                )

        # Post-build finalization (only for isolated sequential mode)
        # This happens AFTER QA validation so the worktree still exists
        if worktree_manager:
            choice = finalize_workspace(
                project_dir,
                spec_dir.name,
                worktree_manager,
                auto_continue=auto_continue,
            )
            handle_workspace_choice(
                choice, project_dir, spec_dir.name, worktree_manager
            )

    except KeyboardInterrupt:
        _handle_build_interrupt(
            spec_dir=spec_dir,
            project_dir=project_dir,
            worktree_manager=worktree_manager,
            working_dir=working_dir,
            model=model,
            max_iterations=max_iterations,
            verbose=verbose,
        )
    except Exception as e:
        print(f"\nFatal error: {e}")
        if verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)


def _handle_build_interrupt(
    spec_dir: Path,
    project_dir: Path,
    worktree_manager: object,
    working_dir: Path,
    model: str,
    max_iterations: int | None,
    verbose: bool,
) -> None:
    """
    Handle keyboard interrupt during build.

    Args:
        spec_dir: Spec directory path
        project_dir: Project root directory
        worktree_manager: Worktree manager instance (if using isolated mode)
        working_dir: Current working directory
        model: Model being used
        max_iterations: Maximum iterations
        verbose: Verbose mode flag
    """
    from agent import run_autonomous_agent

    # Print paused banner
    print_paused_banner(spec_dir, spec_dir.name, has_worktree=bool(worktree_manager))

    # Update status file
    status_manager = StatusManager(project_dir)
    status_manager.update(state=BuildState.PAUSED)

    # Offer to add human input with enhanced menu
    try:
        options = [
            MenuOption(
                key="type",
                label="Type instructions",
                icon=Icons.EDIT,
                description="Enter guidance for the agent's next session",
            ),
            MenuOption(
                key="paste",
                label="Paste from clipboard",
                icon=Icons.CLIPBOARD,
                description="Paste text you've copied (Cmd+V / Ctrl+Shift+V)",
            ),
            MenuOption(
                key="file",
                label="Read from file",
                icon=Icons.DOCUMENT,
                description="Load instructions from a text file",
            ),
            MenuOption(
                key="skip",
                label="Continue without instructions",
                icon=Icons.SKIP,
                description="Resume the build as-is",
            ),
            MenuOption(
                key="quit",
                label="Quit",
                icon=Icons.DOOR,
                description="Exit without resuming",
            ),
        ]

        choice = select_menu(
            title="What would you like to do?",
            options=options,
            subtitle="Progress saved. You can add instructions for the agent.",
            allow_quit=False,  # We have explicit quit option
        )

        if choice == "quit" or choice is None:
            print()
            print_status("Exiting...", "info")
            status_manager.set_inactive()
            sys.exit(0)

        human_input: str | None = ""

        if choice == "file":
            # Read from file
            human_input = read_from_file()
            if human_input is None:
                human_input = ""

        elif choice in ["type", "paste"]:
            human_input = read_multiline_input("Enter/paste your instructions below.")
            if human_input is None:
                print()
                print_status("Exiting without saving instructions...", "warning")
                status_manager.set_inactive()
                sys.exit(0)

        if human_input:
            # Save to HUMAN_INPUT.md
            input_file = spec_dir / "HUMAN_INPUT.md"
            input_file.write_text(human_input)

            content = [
                success(f"{icon(Icons.SUCCESS)} INSTRUCTIONS SAVED"),
                "",
                f"Saved to: {highlight(str(input_file.name))}",
                "",
                muted(
                    "The agent will read and follow these instructions when you resume."
                ),
            ]
            print()
            print(box(content, width=70, style="heavy"))
        elif choice != "skip":
            print()
            print_status("No instructions provided.", "info")

        # If 'skip' was selected, actually resume the build
        if choice == "skip":
            print()
            print_status("Resuming build...", "info")
            status_manager.update(state=BuildState.RUNNING)
            asyncio.run(
                run_autonomous_agent(
                    project_dir=working_dir,
                    spec_dir=spec_dir,
                    model=model,
                    max_iterations=max_iterations,
                    verbose=verbose,
                )
            )
            # Build completed or was interrupted again - exit
            sys.exit(0)

    except KeyboardInterrupt:
        # User pressed Ctrl+C again during input prompt - exit immediately
        print()
        print_status("Exiting...", "warning")
        status_manager = StatusManager(project_dir)
        status_manager.set_inactive()
        sys.exit(0)
    except EOFError:
        # stdin closed
        pass

    # Resume instructions (shown when user provided instructions or chose file/type/paste)
    print()
    content = [
        bold(f"{icon(Icons.PLAY)} TO RESUME"),
        "",
        f"Run: {highlight(f'python aifactory/run.py --spec {spec_dir.name}')}",
    ]
    if worktree_manager:
        content.append("")
        content.append(muted("Your build is in a separate workspace and is safe."))
    print(box(content, width=70, style="light"))
    print()
