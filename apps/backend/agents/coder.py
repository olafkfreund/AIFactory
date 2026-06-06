"""
Coder Agent Module
==================

Main autonomous agent loop that runs the coder agent to implement subtasks.
"""

import asyncio
import json
import logging
from pathlib import Path

from core.client import create_client
from core.error_utils import (
    decide_rate_limit_resume,
    extract_rate_limit_cooldown,
)
from phase_config import (
    get_phase_model,
    get_phase_thinking_budget,
    get_provider_extra_kwargs,
    infer_provider_from_model,
)
from phase_event import ExecutionPhase, emit_phase
from progress import (
    count_subtasks,
    count_subtasks_detailed,
    get_current_phase,
    get_next_subtask,
    is_build_complete,
    print_build_complete_banner,
    print_progress_summary,
    print_session_header,
)
from prompt_generator import (
    format_context_for_prompt,
    generate_planner_prompt,
    generate_subtask_prompt,
    load_subtask_context,
)
from prompts import get_solo_prompt, is_first_run
from providers.factory import get_provider
from recovery import RecoveryManager
from solo_mode import is_solo_mode_enabled_for_spec
from task_logger import (
    LogPhase,
    get_task_logger,
)
from ui import (
    BuildState,
    Icons,
    StatusManager,
    bold,
    box,
    highlight,
    icon,
    muted,
    print_status,
)

from .base import AUTO_CONTINUE_DELAY_SECONDS, HUMAN_INTERVENTION_FILE
from .compaction_recovery import CompactionDetector, build_operational_context
from .inbox import drain_unread as drain_inbox
from .inbox import format_for_prompt as format_inbox_for_prompt
from .memory_manager import debug_memory_system_status, get_graphiti_context
from .session import post_session_processing, run_agent_session
from .token_attribution import PromptSegments, TurnUsage, record_turn
from .utils import (
    find_phase_for_subtask,
    get_commit_count,
    get_latest_commit,
    load_implementation_plan,
    sync_plan_to_source,
)

logger = logging.getLogger(__name__)

# Default number of subtasks to run concurrently in a parallel_safe wave (#376)
# when --parallel is requested without an explicit --workers value.
DEFAULT_PARALLEL_WORKERS = 3


def solo_session_plans_inline(solo: bool, parallel: bool) -> bool:
    """Whether the solo first session both plans AND implements in one turn.

    Solo mode (#276) collapses planning and implementation into a single
    session. That single-session collapse bypasses the parallel wave dispatch
    (#389) — waves operate on a pre-authored plan, and solo finishes the build
    before the per-subtask loop is ever reached.

    When the user opts into parallel execution we therefore author the plan
    first (a planner session) and let the wave dispatch implement independent
    subtasks concurrently. Trivial plans with no wave-eligible phase fall
    through to the serial loop (which skips the sub-worktree overhead), so this
    never makes small tasks slower. With ``parallel`` off, solo is unchanged.
    """
    return solo and not parallel


async def run_autonomous_agent(
    project_dir: Path,
    spec_dir: Path,
    model: str,
    max_iterations: int | None = None,
    verbose: bool = False,
    source_spec_dir: Path | None = None,
    stop_after_planning: bool = False,
    remote_control_session: str | None = None,
    parallel: bool = False,
    workers: int | None = None,
) -> None:
    """
    Run the autonomous agent loop with automatic memory management.

    The agent can use subagents (via Task tool) for parallel execution if needed.
    This is decided by the agent itself based on the task complexity.

    Args:
        project_dir: Root directory for the project
        spec_dir: Directory containing the spec (aifactory/specs/001-name/)
        model: Claude model to use
        max_iterations: Maximum number of iterations (None for unlimited)
        verbose: Whether to show detailed output
        source_spec_dir: Original spec directory in main project (for syncing from worktree)
        stop_after_planning: Return as soon as the planner phase finishes
            writing implementation_plan.json. Used by the Copilot delegation
            flow — AIFactory enriches the plan locally then hands the issue
            off to GitHub Copilot Coding Agent (#92, #94).
        parallel: When True, run independent subtasks of a ``parallel_safe``
            phase concurrently in dependency-graph waves (#376) instead of
            strictly one-at-a-time. Phases that are not parallel_safe still run
            serially, so this is always safe to enable.
        workers: Max concurrent subtasks per wave when ``parallel`` is set.
            Defaults to ``DEFAULT_PARALLEL_WORKERS`` (3) when None.
    """
    # Normalize parallelism config (#376). Concurrency is only ever attempted
    # for phases the planner marked parallel_safe; everything else stays serial.
    parallel_workers = max(1, workers or DEFAULT_PARALLEL_WORKERS)
    # Phases that stalled under parallel execution fall back to serial and must
    # not be retried in parallel (avoids a re-enter-stall loop).
    parallel_disabled_phases: set[int] = set()
    if parallel:
        print_status(
            f"Parallel execution enabled (up to {parallel_workers} concurrent "
            "subtasks in parallel_safe phases)",
            "info",
        )
    # Initialize recovery manager (handles memory persistence)
    recovery_manager = RecoveryManager(spec_dir, project_dir)

    # Initialize status manager for ccstatusline
    status_manager = StatusManager(project_dir)
    status_manager.set_active(spec_dir.name, BuildState.BUILDING)

    # Initialize task logger for persistent logging
    task_logger = get_task_logger(spec_dir)

    # Debug: Print memory system status at startup
    debug_memory_system_status()

    # Update initial subtask counts
    subtasks = count_subtasks_detailed(spec_dir)
    status_manager.update_subtasks(
        completed=subtasks["completed"],
        total=subtasks["total"],
        in_progress=subtasks["in_progress"],
    )

    # Check if this is a fresh start or continuation
    first_run = is_first_run(spec_dir)

    # Solo mode (#276): a single self-directed agent plans + implements +
    # verifies in one streamlined flow. When enabled, the first session uses
    # the solo prompt (and coder tools, which include update_subtask_status)
    # instead of a dedicated planner session, and the plan-review gate is
    # skipped. Default OFF — the full pipeline is unchanged when disabled.
    solo = is_solo_mode_enabled_for_spec(spec_dir)

    # #389: solo collapses plan+implement into one session, which bypasses the
    # wave dispatch. Under --parallel, author the plan first (planner session)
    # so independent subtasks can run concurrently in waves; trivial plans then
    # fall through to the serial loop. Solo behaviour is unchanged with parallel
    # off, and solo still skips the plan-review gate / QA loop either way.
    solo_plans_inline = solo_session_plans_inline(solo, parallel)
    if solo and parallel:
        print_status(
            "Solo + parallel: authoring the plan first so independent subtasks "
            "can run concurrently in waves (#389)",
            "info",
        )

    # Track which phase we're in for logging
    current_log_phase = LogPhase.CODING
    is_planning_phase = False

    if first_run:
        if solo_plans_inline:
            print_status(
                "Solo mode - a single self-directed agent will plan and build",
                "info",
            )
            content = [
                bold(f"{icon(Icons.GEAR)} SOLO SESSION"),
                "",
                f"Spec: {highlight(spec_dir.name)}",
                muted(
                    "One agent will author its own plan, implement it, and "
                    "verify its own work (no separate planner or QA)."
                ),
            ]
        else:
            print_status(
                "Fresh start - will use Planner Agent to create implementation plan",
                "info",
            )
            content = [
                bold(f"{icon(Icons.GEAR)} PLANNER SESSION"),
                "",
                f"Spec: {highlight(spec_dir.name)}",
                muted(
                    "The agent will analyze your spec and create a "
                    "subtask-based plan."
                ),
            ]
        print()
        print(box(content, width=70, style="heavy"))
        print()

        # Update status for planning phase
        status_manager.update(state=BuildState.PLANNING)
        emit_phase(ExecutionPhase.PLANNING, "Creating implementation plan")
        is_planning_phase = True
        current_log_phase = LogPhase.PLANNING

        # Start planning phase in task logger
        if task_logger:
            task_logger.start_phase(
                LogPhase.PLANNING, "Starting implementation planning..."
            )

    else:
        print(f"Continuing build: {highlight(spec_dir.name)}")
        print_progress_summary(spec_dir)

        # Check if already complete
        if is_build_complete(spec_dir):
            print_build_complete_banner(spec_dir)
            status_manager.update(state=BuildState.COMPLETE)
            return

        # Start/continue coding phase in task logger
        if task_logger:
            task_logger.start_phase(LogPhase.CODING, "Continuing implementation...")

        # Emit phase event when continuing build
        emit_phase(ExecutionPhase.CODING, "Continuing implementation")

    # Show human intervention hint
    content = [
        bold("INTERACTIVE CONTROLS"),
        "",
        f"Press {highlight('Ctrl+C')} once  {icon(Icons.ARROW_RIGHT)} Pause and optionally add instructions",
        f"Press {highlight('Ctrl+C')} twice {icon(Icons.ARROW_RIGHT)} Exit immediately",
    ]
    print(box(content, width=70, style="light"))
    print()

    # Per-session compaction detector (#262). One detector per autonomous run;
    # observes each turn's real input-token total to spot a context compaction.
    compaction_detector = CompactionDetector()
    # When a compaction is detected, re-anchor the NEXT turn's prompt with a
    # small operational-context block. Carried across iterations.
    pending_recovery_block: str | None = None

    # Rate-limit auto-resume state (#272): track how many times we've resumed
    # after a provider rate limit and how long we've cumulatively waited, so the
    # resume policy can enforce its max-retries / max-total-wait caps.
    rate_limit_attempt = 0
    rate_limit_total_wait = 0.0

    # Main loop
    iteration = 0

    while True:
        iteration += 1

        # Check for human intervention (PAUSE file)
        pause_file = spec_dir / HUMAN_INTERVENTION_FILE
        if pause_file.exists():
            print("\n" + "=" * 70)
            print("  PAUSED BY HUMAN")
            print("=" * 70)

            pause_content = pause_file.read_text().strip()
            if pause_content:
                print(f"\nMessage: {pause_content}")

            print("\nTo resume, delete the PAUSE file:")
            print(f"  rm {pause_file}")
            print("\nThen run again:")
            print(f"  python aifactory/run.py --spec {spec_dir.name}")
            return

        # Check max iterations
        if max_iterations and iteration > max_iterations:
            print(f"\nReached max iterations ({max_iterations})")
            print("To continue, run the script again without --max-iterations")
            break

        # Get the next subtask to work on
        next_subtask = get_next_subtask(spec_dir)
        subtask_id = next_subtask.get("id") if next_subtask else None
        phase_name = next_subtask.get("phase_name") if next_subtask else None

        # Update status for this session
        status_manager.update_session(iteration)
        if phase_name:
            current_phase = get_current_phase(spec_dir)
            if current_phase:
                status_manager.update_phase(
                    current_phase.get("name", ""),
                    current_phase.get("phase", 0),
                    current_phase.get("total", 0),
                )
        status_manager.update_subtasks(in_progress=1)

        # Print session header
        print_session_header(
            session_num=iteration,
            is_planner=first_run,
            subtask_id=subtask_id,
            subtask_desc=next_subtask.get("description") if next_subtask else None,
            phase_name=phase_name,
            attempt=recovery_manager.get_attempt_count(subtask_id) + 1
            if subtask_id
            else 1,
        )

        # Capture state before session for post-processing
        commit_before = get_latest_commit(project_dir)
        commit_count_before = get_commit_count(project_dir)

        # Get the phase-specific model and thinking level (respects task_metadata.json configuration)
        # first_run means we're in planning phase, otherwise coding phase
        current_phase = "planning" if first_run else "coding"
        phase_model = get_phase_model(spec_dir, current_phase, model)
        phase_thinking_budget = get_phase_thinking_budget(spec_dir, current_phase)

        # Per-subtask model override (#376 right-sizing): a coding subtask may
        # declare its own model (e.g. "haiku" for mechanical scaffolding) to run
        # cheaper/faster than the phase default. Planning is never overridden.
        if not first_run and next_subtask and next_subtask.get("model"):
            phase_model = next_subtask["model"]

        # Create client (fresh context) with phase-specific model and thinking
        # Route through provider factory for non-Claude models
        provider_name = infer_provider_from_model(phase_model)
        if provider_name == "claude":
            # Use existing create_client for Claude (preserves MCP, security, etc.)
            client = create_client(
                project_dir,
                spec_dir,
                phase_model,
                # Solo mode always uses the coder toolset (Write/Edit/Bash +
                # update_subtask_status) so the single agent can both author
                # and track its own plan. The planner toolset lacks
                # update_subtask_status.
                agent_type=("coder" if solo_plans_inline else "planner")
                if first_run
                else "coder",
                max_thinking_tokens=phase_thinking_budget,
                remote_control_session=remote_control_session,
            )
        else:
            # Use agentic provider for non-Claude models
            provider_kwargs = {
                "model": phase_model,
                "working_dir": project_dir,
                **get_provider_extra_kwargs(provider_name, phase_model),
            }
            client = get_provider(
                provider_name,
                phase=current_phase,
                **provider_kwargs,
            )

        # Token-attribution segments (#262): track the named prompt pieces as
        # we assemble them so per-category token usage can be attributed.
        seg_user_prompt = ""
        seg_file_context = ""
        seg_coordination = ""

        # Generate appropriate prompt
        if first_run:
            # Solo mode (#276): the single agent gets a self-directing prompt
            # that has it author its own plan AND implement it. Otherwise use
            # the dedicated planner prompt.
            if solo_plans_inline:
                prompt = get_solo_prompt(spec_dir)
            else:
                # Planner-only session: authors the plan without implementing,
                # so the wave dispatch can pick up independent subtasks (#389).
                prompt = generate_planner_prompt(spec_dir, project_dir)
            seg_user_prompt = prompt

            # Retrieve Graphiti memory context for planning phase
            # This gives the planner knowledge of previous patterns, gotchas, and insights
            planner_context = await get_graphiti_context(
                spec_dir,
                project_dir,
                {
                    "description": "Planning implementation for new feature",
                    "id": "planner",
                },
            )
            if planner_context:
                prompt += "\n\n" + planner_context
                seg_coordination += planner_context
                print_status("Graphiti memory context loaded for planner", "success")

            first_run = False
            current_log_phase = LogPhase.PLANNING

            # Set session info in logger
            if task_logger:
                task_logger.set_session(iteration)
        else:
            # Switch to coding phase after planning
            if is_planning_phase:
                is_planning_phase = False

                # Copilot delegation: planner is done — return so
                # auto_fix_service can post the enriched plan as a
                # comment and assign the Copilot bot (#94). Skip
                # the human-review gate and all downstream phases.
                if stop_after_planning:
                    if task_logger:
                        task_logger.end_phase(
                            LogPhase.PLANNING,
                            success=True,
                            message="Plan created — delegating to Copilot",
                        )
                    if source_spec_dir:
                        sync_plan_to_source(spec_dir, source_spec_dir)
                    return

                # Check if human review is required before coding.
                # Solo mode (#276) is the streamlined single-agent path and
                # never gates on plan review — the agent self-directs straight
                # from plan to implementation.
                require_review = not solo and _should_require_human_review(spec_dir)
                if require_review:
                    # Check if already approved
                    from review import ReviewState
                    review_state = ReviewState.load(spec_dir)
                    if not review_state.is_approval_valid(spec_dir):
                        # Pause for human review
                        logger.info("Plan review required - pausing execution for human approval")
                        emit_phase(ExecutionPhase.PLAN_REVIEW, "Waiting for plan approval")

                        # Update implementation_plan.json status
                        plan_file = spec_dir / "implementation_plan.json"
                        if plan_file.exists():
                            try:
                                plan = json.loads(plan_file.read_text())
                                plan["status"] = "human_review"
                                plan["reviewReason"] = "plan_review"
                                plan_file.write_text(json.dumps(plan, indent=2))
                            except (json.JSONDecodeError, OSError) as e:
                                logger.warning(f"Failed to update plan status: {e}")

                        # Also sync to source spec dir if in worktree
                        if source_spec_dir:
                            sync_plan_to_source(spec_dir, source_spec_dir)

                        if task_logger:
                            task_logger.end_phase(
                                LogPhase.PLANNING,
                                success=True,
                                message="Plan created - waiting for human approval",
                            )

                        print()
                        print(box([
                            bold(f"{icon(Icons.WARNING)} PLAN REVIEW REQUIRED"),
                            "",
                            "The implementation plan has been created and requires your approval.",
                            "Please review the plan in the web UI and click 'Approve Plan' to continue.",
                            "",
                            highlight("Task Status: human_review (plan_review)"),
                        ], width=70, style="heavy"))
                        print()

                        return  # Exit agent loop - task pauses for approval

                # Continue to coding phase
                current_log_phase = LogPhase.CODING
                emit_phase(ExecutionPhase.CODING, "Starting implementation")
                if task_logger:
                    task_logger.end_phase(
                        LogPhase.PLANNING,
                        success=True,
                        message="Implementation plan created",
                    )
                    task_logger.start_phase(
                        LogPhase.CODING, "Starting implementation..."
                    )

            if not next_subtask:
                print("No pending subtasks found - build may be complete!")
                break

            # === PARALLEL WAVE DISPATCH (#376) ===
            # If parallel execution is enabled and the phase owning this subtask
            # is parallel_safe with >=2 pending subtasks, run the whole phase
            # concurrently in dependency-graph waves, then continue the loop
            # (which picks up the next phase or finishes). Any non-parallel_safe
            # phase, or a stalled wave, falls through to the serial path below —
            # so this is always safe.
            if parallel:
                handled = await _maybe_run_parallel_phase(
                    spec_dir=spec_dir,
                    project_dir=project_dir,
                    subtask_id=subtask_id,
                    model=model,
                    parallel_workers=parallel_workers,
                    verbose=verbose,
                    source_spec_dir=source_spec_dir,
                    remote_control_session=remote_control_session,
                    status_manager=status_manager,
                    disabled_phases=parallel_disabled_phases,
                    recovery_manager=recovery_manager,
                )
                if handled:
                    # Progress was made in parallel; re-evaluate from the top.
                    print_progress_summary(spec_dir)
                    status_manager.update(state=BuildState.BUILDING)
                    await asyncio.sleep(1)
                    continue

            # Get attempt count for recovery context
            attempt_count = recovery_manager.get_attempt_count(subtask_id)
            recovery_hints = (
                recovery_manager.get_recovery_hints(subtask_id)
                if attempt_count > 0
                else None
            )

            # Find the phase for this subtask
            plan = load_implementation_plan(spec_dir)
            phase = find_phase_for_subtask(plan, subtask_id) if plan else {}

            # Generate focused, minimal prompt for this subtask
            prompt = generate_subtask_prompt(
                spec_dir=spec_dir,
                project_dir=project_dir,
                subtask=next_subtask,
                phase=phase or {},
                attempt_count=attempt_count,
                recovery_hints=recovery_hints,
            )
            seg_user_prompt = prompt

            # Load and append relevant file context
            context = load_subtask_context(spec_dir, project_dir, next_subtask)
            if context.get("patterns") or context.get("files_to_modify"):
                _file_ctx = format_context_for_prompt(context)
                prompt += "\n\n" + _file_ctx
                seg_file_context += _file_ctx

            # Retrieve and append Graphiti memory context (if enabled)
            graphiti_context = await get_graphiti_context(
                spec_dir, project_dir, next_subtask
            )
            if graphiti_context:
                # #369: memory is populated by past sessions and could be
                # poisoned cross-session. It is reference data, never
                # instructions — wrap it so an injected directive can't steer
                # this run.
                from security import wrap_untrusted

                framed_memory = wrap_untrusted(
                    graphiti_context,
                    source="knowledge-graph memory of past sessions",
                )
                prompt += "\n\n" + framed_memory
                seg_coordination += framed_memory
                print_status("Graphiti memory context loaded", "success")

            # Show what we're working on
            print(f"Working on: {highlight(subtask_id)}")
            print(f"Description: {next_subtask.get('description', 'No description')}")
            if attempt_count > 0:
                print_status(f"Previous attempts: {attempt_count}", "warning")
            print()

        # Set subtask info in logger
        if task_logger and subtask_id:
            task_logger.set_subtask(subtask_id)
            task_logger.set_session(iteration)

        # Between-turn inbox check (#264): deliver any user messages that
        # arrived while the previous turn was running. Drained exactly-once
        # and folded into this turn's prompt as high-priority directives.
        inbox_messages = drain_inbox(spec_dir)
        if inbox_messages:
            _inbox_block = format_inbox_for_prompt(inbox_messages)
            prompt += "\n\n" + _inbox_block
            seg_coordination += _inbox_block
            for _msg in inbox_messages:
                print_status(
                    f"Inbox message delivered to agent: {_msg.get('summary', '')}",
                    "info",
                )

        # Post-compact recovery (#262): if the previous turn looked compacted,
        # re-anchor this turn with a small operational-context block so the
        # agent doesn't lose grounding mid-build.
        if pending_recovery_block:
            prompt = pending_recovery_block + "\n\n" + prompt
            seg_coordination += pending_recovery_block
            print_status(
                "Context compaction detected - re-injected operational context",
                "info",
            )
            pending_recovery_block = None

        # Run session with async context manager
        async with client:
            status, response, error_info = await run_agent_session(
                client, prompt, spec_dir, verbose, phase=current_log_phase
            )

        # === PER-CATEGORY TOKEN ATTRIBUTION (#262) ===
        # Attribute the session's real SDK usage across source categories and
        # fold into the per-task aggregate. Also run best-effort compaction
        # detection so the NEXT turn can re-anchor. Never break a build on a
        # bookkeeping error.
        usage_payload = error_info.get("usage") if error_info else None
        if usage_payload:
            try:
                turn_usage = TurnUsage.from_sdk_usage(
                    {
                        "input_tokens": usage_payload.get("input_tokens", 0),
                        "output_tokens": usage_payload.get("output_tokens", 0),
                        "cache_read_input_tokens": usage_payload.get(
                            "cache_read_tokens", 0
                        ),
                        "cache_creation_input_tokens": usage_payload.get(
                            "cache_creation_tokens", 0
                        ),
                    },
                    cost_usd=usage_payload.get("cost_usd", 0.0),
                )
                segments = PromptSegments(
                    user_prompt=seg_user_prompt,
                    coordination_context=seg_coordination,
                    file_context=seg_file_context,
                    tool_output_chars=usage_payload.get("tool_output_chars", 0),
                )
                record_turn(spec_dir, segments, turn_usage, model=phase_model)
                # Sync the usage file back to the source spec dir (worktree mode)
                # so the web-server reader sees it.
                if source_spec_dir:
                    try:
                        from .token_attribution import usage_file_path
                        src = usage_file_path(spec_dir)
                        if src.exists():
                            (source_spec_dir / src.name).write_text(
                                src.read_text(encoding="utf-8"), encoding="utf-8"
                            )
                    except OSError:
                        pass

                # Best-effort compaction detection on the real input total.
                if compaction_detector.observe(turn_usage.total_input_tokens):
                    pending_recovery_block = build_operational_context(
                        spec_dir, current_subtask=subtask_id
                    )
            except Exception as e:  # noqa: BLE001 - bookkeeping must not crash a build
                logger.debug(f"Token attribution skipped: {e}")

        # === POST-SESSION PROCESSING (100% reliable) ===
        if subtask_id and not first_run:
            success = await post_session_processing(
                spec_dir=spec_dir,
                project_dir=project_dir,
                subtask_id=subtask_id,
                session_num=iteration,
                commit_before=commit_before,
                commit_count_before=commit_count_before,
                recovery_manager=recovery_manager,
                status_manager=status_manager,
                source_spec_dir=source_spec_dir,
            )

            # Check for stuck subtasks
            attempt_count = recovery_manager.get_attempt_count(subtask_id)
            if not success and attempt_count >= 3:
                recovery_manager.mark_subtask_stuck(
                    subtask_id, f"Failed after {attempt_count} attempts"
                )
                print()
                print_status(
                    f"Subtask {subtask_id} marked as STUCK after {attempt_count} attempts",
                    "error",
                )
                print(muted("Consider: manual intervention or skipping this subtask"))
        elif is_planning_phase and source_spec_dir:
            # After planning phase, sync the newly created implementation plan back to source
            if sync_plan_to_source(spec_dir, source_spec_dir):
                print_status("Implementation plan synced to main project", "success")

        # Handle session status
        if status == "complete":
            # Don't emit COMPLETE here - subtasks are done but QA hasn't run yet
            # QA loop will emit COMPLETE after actual approval
            print_build_complete_banner(spec_dir)
            status_manager.update(state=BuildState.COMPLETE)

            if task_logger:
                task_logger.end_phase(
                    LogPhase.CODING,
                    success=True,
                    message="All subtasks completed successfully",
                )

            break

        elif status == "continue":
            # A clean turn means we've recovered from any prior rate limit;
            # reset the resume budget so a later, unrelated rate limit gets a
            # fresh set of retries instead of inheriting an exhausted cap (#272).
            rate_limit_attempt = 0
            rate_limit_total_wait = 0.0
            print(
                muted(
                    f"\nAgent will auto-continue in {AUTO_CONTINUE_DELAY_SECONDS}s..."
                )
            )
            print_progress_summary(spec_dir)

            # Update state back to building
            status_manager.update(state=BuildState.BUILDING)

            # Show next subtask info
            next_subtask = get_next_subtask(spec_dir)
            if next_subtask:
                subtask_id = next_subtask.get("id")
                print(
                    f"\nNext: {highlight(subtask_id)} - {next_subtask.get('description')}"
                )

                attempt_count = recovery_manager.get_attempt_count(subtask_id)
                if attempt_count > 0:
                    print_status(
                        f"WARNING: {attempt_count} previous attempt(s)", "warning"
                    )

            await asyncio.sleep(AUTO_CONTINUE_DELAY_SECONDS)

        elif status == "error":
            # Rate-limit auto-resume (#272): if the provider rate-limited us and
            # exposed (or we can default) a cooldown, wait it out and continue
            # rather than burning a retry immediately. Bounded by the resume
            # policy's max-retries / max-total-wait caps.
            if error_info.get("type") == "rate_limit":
                cooldown = extract_rate_limit_cooldown(error_info.get("message", ""))
                rate_limit_attempt += 1
                decision = decide_rate_limit_resume(
                    cooldown_seconds=cooldown,
                    attempt=rate_limit_attempt,
                    elapsed_wait_seconds=rate_limit_total_wait,
                )
                if decision.should_resume:
                    logger.warning(
                        "Rate limit hit — auto-resuming: %s", decision.reason
                    )
                    print_status(
                        f"Rate limited — auto-resuming in "
                        f"{decision.wait_seconds:.0f}s ({decision.reason})",
                        "warning",
                    )
                    status_manager.update(state=BuildState.ERROR)
                    rate_limit_total_wait += decision.wait_seconds
                    await asyncio.sleep(decision.wait_seconds)
                    # The while-loop re-invokes a fresh session next iteration,
                    # which is the "resume" — no extra orchestration needed.
                    continue
                # Caps exhausted — fall through to the normal error path.
                logger.warning(
                    "Rate limit auto-resume giving up: %s", decision.reason
                )
                print_status(
                    f"Rate limit auto-resume stopped: {decision.reason}", "error"
                )

            emit_phase(ExecutionPhase.FAILED, "Session encountered an error")
            print_status("Session encountered an error", "error")
            print(muted("Will retry with a fresh session..."))
            status_manager.update(state=BuildState.ERROR)
            await asyncio.sleep(AUTO_CONTINUE_DELAY_SECONDS)

        # Small delay between sessions
        if max_iterations is None or iteration < max_iterations:
            print("\nPreparing next session...\n")
            await asyncio.sleep(1)

    # Final summary
    content = [
        bold(f"{icon(Icons.SESSION)} SESSION SUMMARY"),
        "",
        f"Project: {project_dir}",
        f"Spec: {highlight(spec_dir.name)}",
        f"Sessions completed: {iteration}",
    ]
    print()
    print(box(content, width=70, style="heavy"))
    print_progress_summary(spec_dir)

    # Show stuck subtasks if any
    stuck_subtasks = recovery_manager.get_stuck_subtasks()
    if stuck_subtasks:
        print()
        print_status("STUCK SUBTASKS (need manual intervention):", "error")
        for stuck in stuck_subtasks:
            print(f"  {icon(Icons.ERROR)} {stuck['subtask_id']}: {stuck['reason']}")

    # Instructions
    completed, total = count_subtasks(spec_dir)
    if completed < total:
        content = [
            bold(f"{icon(Icons.PLAY)} NEXT STEPS"),
            "",
            f"{total - completed} subtasks remaining.",
            f"Run again: {highlight(f'python aifactory/run.py --spec {spec_dir.name}')}",
        ]
    else:
        content = [
            bold(f"{icon(Icons.SUCCESS)} NEXT STEPS"),
            "",
            "All subtasks completed!",
            "  1. Review the aifactory/* branch",
            "  2. Run manual tests",
            "  3. Merge to main",
        ]

    print()
    print(box(content, width=70, style="light"))
    print()

    # Set final status
    if completed == total:
        status_manager.update(state=BuildState.COMPLETE)
    else:
        status_manager.update(state=BuildState.PAUSED)


def _should_require_human_review(spec_dir: Path) -> bool:
    """
    Check if human review is required before coding.

    Returns True if:
    - task_metadata.json has requireReviewBeforeCoding=true
    - OR task_metadata.json has mode="quick" (Quick Mode always requires review)

    Args:
        spec_dir: Path to the spec directory

    Returns:
        True if human review is required, False otherwise
    """
    task_metadata_file = spec_dir / "task_metadata.json"
    if not task_metadata_file.exists():
        return False

    try:
        metadata = json.loads(task_metadata_file.read_text())

        # Explicit requireReviewBeforeCoding flag
        if metadata.get("requireReviewBeforeCoding", False):
            return True

        # Quick Mode always requires human review
        if metadata.get("mode") == "quick":
            return True

        return False
    except (json.JSONDecodeError, OSError):
        return False


async def _run_trailing_gates_if_build_complete(
    spec_dir: Path, project_dir: Path
) -> None:
    """Run lint/type/test gates once when all subtasks are complete (#376 D).

    Best-effort: detects project gates, runs them as direct subprocesses (not
    agent turns), logs a one-line summary, and on failure writes a
    ``GATE_FAILURES.md`` marker the QA/fix loop can pick up. Never raises and
    never fails the build — a clean run simply removes any stale marker.
    """
    try:
        from .gate_runner import (
            detect_gates,
            failing_gates,
            run_gates,
            summarize_gates,
        )

        # Only run when the whole plan is complete — "once at the end".
        plan_path = spec_dir / "implementation_plan.json"
        from implementation_plan.enums import SubtaskStatus
        from implementation_plan.plan import ImplementationPlan

        plan = ImplementationPlan.load(plan_path)
        pending = [
            s
            for p in plan.phases
            for s in p.subtasks
            if s.status != SubtaskStatus.COMPLETED
        ]
        if pending:
            return  # more work remains; gates run after the final wave

        gates = detect_gates(project_dir)
        if not gates:
            return
        print_status(
            f"Running trailing gates once: {', '.join(g.name for g in gates)}",
            "info",
        )
        results = await run_gates(project_dir, gates)
        summary = summarize_gates(results)
        failures = failing_gates(results)
        marker = spec_dir / "GATE_FAILURES.md"
        if failures:
            lines = [f"# Gate failures\n\nSummary: {summary}\n"]
            for r in failures:
                lines.append(f"\n## {r.name} (exit {r.exit_code})\n\n```\n{r.output_tail}\n```\n")
            marker.write_text("".join(lines), encoding="utf-8")
            print_status(f"Trailing gates failed: {summary}", "warning")
        else:
            if marker.exists():
                marker.unlink()  # clear any stale failures from a prior run
            print_status(f"Trailing gates passed: {summary}", "success")
    except Exception as exc:  # noqa: BLE001 - gates are best-effort, never break a build
        logger.debug("Trailing gate run skipped: %s", exc)


async def _maybe_run_parallel_phase(
    *,
    spec_dir: Path,
    project_dir: Path,
    subtask_id: str | None,
    model: str,
    parallel_workers: int,
    verbose: bool,
    source_spec_dir: Path | None,
    remote_control_session: str | None,
    status_manager,
    disabled_phases: set[int],
    recovery_manager=None,
) -> bool:
    """Run the subtask's phase as parallel waves if it is eligible (#376).

    Returns True if a parallel phase ran and made progress (caller should
    `continue` the loop). Returns False if the phase is not eligible or could
    not progress, so the caller falls back to the serial path for this subtask.
    """
    if not subtask_id:
        return False

    plan_path = spec_dir / "implementation_plan.json"
    if not plan_path.exists():
        return False

    try:
        from implementation_plan.plan import ImplementationPlan

        from .parallel_integration import run_parallel_coding_phase
        from .parallel_runner import is_phase_parallel_eligible

        plan = ImplementationPlan.load(plan_path)
        phase = next(
            (
                p
                for p in plan.phases
                for s in p.subtasks
                if s.id == subtask_id
            ),
            None,
        )
        if phase is None:
            return False
        if phase.phase in disabled_phases:
            return False
        if not is_phase_parallel_eligible(phase, parallel_workers):
            return False

        print_status(
            f"Running phase '{phase.name}' in parallel "
            f"({parallel_workers} workers)",
            "info",
        )
        result = await run_parallel_coding_phase(
            plan=plan,
            phase=phase,
            project_dir=project_dir,
            spec_dir=spec_dir,
            model=model,
            workers=parallel_workers,
            verbose=verbose,
            source_spec_dir=source_spec_dir,
            remote_control_session=remote_control_session,
            recovery_manager=recovery_manager,
        )

        # A stalled phase (cycle / unresolved deps / merge failures) drops to the
        # serial path for whatever is left — and must not be retried in parallel.
        if result.stalled or result.failed_ids:
            disabled_phases.add(phase.phase)

        # (#376 solution D) Collapse trailing gates: when this wave finished the
        # last of the plan's subtasks, run lint/type/test gates ONCE as direct
        # subprocesses instead of as separate agent turns. Failures are recorded
        # for the QA/fix loop; this never fails the build itself.
        if result.completed_ids and not result.stalled:
            await _run_trailing_gates_if_build_complete(spec_dir, project_dir)

        # "Made progress" => at least one subtask completed; caller continues.
        return bool(result.completed_ids)
    except Exception as exc:  # noqa: BLE001 - parallel is best-effort; serial is the safety net
        logger.warning(
            "Parallel phase execution failed (%s); falling back to serial", exc
        )
        return False
