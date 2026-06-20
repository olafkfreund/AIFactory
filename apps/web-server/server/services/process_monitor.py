"""Subprocess lifecycle monitor for :class:`AgentService` (#649).

This module holds the ~670-line ``_monitor_process`` body that used to live
inline in ``agent_service.py``. It was extracted verbatim (a pure, behaviour-
preserving move) to shrink that god-file; ``AgentService._monitor_process``
now delegates here.

The function is intentionally written as a free function that takes the live
:class:`AgentService` instance as its first argument (``service``). Every
``self.<x>`` reference in the original method is preserved exactly as
``service.<x>`` — no attribute, branch, or ordering was changed — so the
runtime behaviour is identical. The recursive failover/continuation restarts
go back through ``service._monitor_process`` (the thin delegating method),
which routes here again, exactly as before.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .agent_service import (
    TaskPhase,
    TaskProgress,
    is_failed_build,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .agent_service import AgentService

logger = logging.getLogger(__name__)


async def monitor_process(
    service: AgentService,
    task_id: str,
    proc: asyncio.subprocess.Process,
    project_path: Path | None = None,
    spec_id: str | None = None,
    cmd: list[str] | None = None,
    env: dict | None = None,
) -> None:
    """Monitor subprocess and clean up when it finishes.

    Also periodically syncs files from worktree to main spec dir if project_path
    and spec_id are provided. Supports profile failover on early failures when
    cmd and env are provided.
    """
    try:
        # Periodic sync loop while process is running. The interval and the
        # auto-continuation cap come from the pydantic-settings boundary (#651);
        # defaults (3.0s / 10 rounds) match the prior hardcoded behaviour.
        sync_interval = service.settings.AGENT_MONITOR_SYNC_INTERVAL
        max_continuation_rounds = service.settings.AGENT_MAX_CONTINUATION_ROUNDS

        rate_limit_forced_restart = False
        return_code: int | None = None

        while True:
            # Check if process has finished
            try:
                return_code = await asyncio.wait_for(proc.wait(), timeout=sync_interval)
                # Process finished
                break
            except asyncio.TimeoutError:
                # Process still running, sync files
                if project_path and spec_id:
                    await service._sync_worktree_files(project_path, spec_id, task_id)

                    # #260: re-drive a peer review that was requested but
                    # never started — first an inbox nudge to the running
                    # reviewer, then escalation to human_review. Idempotent
                    # within its back-off window; never raises.
                    try:
                        from . import review_redrive_service

                        await asyncio.to_thread(
                            review_redrive_service.check_review_obligation,
                            project_path,
                            spec_id,
                        )
                    except Exception as redrive_exc:  # noqa: BLE001
                        logger.debug(
                            f"[AgentService] review re-drive check skipped: {redrive_exc}"
                        )

                # Fix Bug #3: For spec creation, check if review checkpoint reached while process is running
                if project_path and not spec_id:
                    # Detect if spec_runner created plan_review.html (review checkpoint reached)
                    # Parse spec_id from task_id (format: "project_id:spec_id")
                    detected_spec_id = None
                    if ":" in task_id:
                        _, detected_spec_id = task_id.split(":", 1)

                    if detected_spec_id:
                        detected_spec_dir = (
                            project_path / ".aifactory" / "specs" / detected_spec_id
                        )
                        plan_review_file = detected_spec_dir / "plan_review.html"

                        # Check if plan_review.html exists (indicates review checkpoint reached)
                        if plan_review_file.exists():
                            # Check if we've already emitted PLAN_REVIEW for this task
                            current_phase = service._task_current_phases.get(task_id)
                            if current_phase != TaskPhase.PLAN_REVIEW:
                                logger.info(
                                    f"[AgentService] Detected review checkpoint for {detected_spec_id} (plan_review.html exists)"
                                )

                                # Update plan status to human_review
                                await service._update_plan_status(
                                    project_path,
                                    detected_spec_id,
                                    "human_review",
                                    task_id,
                                )

                                # Emit PLAN_REVIEW phase (maps to "human_review" status) — plan_review always scales to 20%
                                await service._emit_progress(
                                    TaskProgress(
                                        task_id=task_id,
                                        phase=TaskPhase.PLAN_REVIEW,
                                        message="Spec created - waiting for human approval",
                                        percentage=100,
                                    ),
                                    previous_phase=TaskPhase.SPEC_CREATION,  # Enable status event emission
                                )

                                # Mark phase as emitted
                                service._task_current_phases[task_id] = (
                                    TaskPhase.PLAN_REVIEW
                                )
                                logger.info(
                                    f"[AgentService] Emitted PLAN_REVIEW status for {task_id}"
                                )

                # If we detect a rate limit and failover is enabled, don't wait for the process to exit.
                if cmd and env:
                    profile_info = service._task_profiles.get(task_id, {})
                    attempt = profile_info.get("attempt", 1)
                    rate_limit_detected = service._task_rate_limits.get(task_id, False)

                    if (
                        rate_limit_detected
                        and attempt == 1
                        and service._should_retry_with_failover()
                    ):
                        logger.warning(
                            f"[AgentService] Rate limit detected for {task_id} while running; terminating process to trigger profile failover"
                        )
                        rate_limit_forced_restart = True
                        try:
                            proc.terminate()
                        except Exception as terminate_exc:  # noqa: BLE001
                            logger.debug(
                                f"[AgentService] terminate() raised (ignored): {terminate_exc}"
                            )
                        try:
                            return_code = await proc.wait()
                        except Exception as wait_exc:  # noqa: BLE001
                            logger.debug(
                                f"[AgentService] proc.wait() after terminate raised (ignored): {wait_exc}"
                            )
                            return_code = 1
                        break

        if return_code is None:
            return_code = 1
        if rate_limit_forced_restart and return_code == 0:
            # Ensure we trigger the retry path.
            return_code = 1

        # Process exited - do final sync
        if project_path and spec_id:
            await service._sync_worktree_files(project_path, spec_id, task_id)

        exit_model = service._task_profiles.get(task_id, {}).get("model", "unknown")
        logger.info(
            f"[AgentService] [Model: {exit_model}] Task {task_id} process exited with code {return_code}"
        )

        # Early model fallback: if a non-Claude model failed, retry with Sonnet
        # before any other processing (spec detection, plan status, etc.)
        if return_code != 0 and cmd and env:
            _fb_info = service._task_profiles.get(task_id, {})
            _fb_model = _fb_info.get("model", "")
            _fb_attempt = _fb_info.get("attempt", 1)
            _fb_is_non_claude = (
                _fb_model
                and not _fb_model.startswith("claude-")
                and _fb_model not in ("haiku", "sonnet", "opus", "opus-1m")
            )
            logger.info(
                f"[AgentService] Fallback check: model={_fb_model!r}, attempt={_fb_attempt}, is_non_claude={_fb_is_non_claude}, cmd={'yes' if cmd else 'no'}, env={'yes' if env else 'no'}"
            )
            if _fb_is_non_claude and _fb_attempt <= 1:
                new_proc = await service._retry_task_with_fallback_model(
                    task_id, project_path, spec_id, cmd, env
                )
                if new_proc:
                    service._task_rate_limits.pop(task_id, None)
                    service.running_tasks[task_id] = new_proc

                    log_writer = None
                    main_log_writer = None
                    if task_id in service._task_log_writers:
                        log_writer, main_log_writer = service._task_log_writers[task_id]

                    asyncio.create_task(
                        service._process_output(
                            task_id,
                            new_proc.stdout,
                            is_stderr=False,
                            log_writer=log_writer,
                            spec_id=spec_id,
                        )
                    )
                    asyncio.create_task(
                        service._process_output(
                            task_id,
                            new_proc.stderr,
                            is_stderr=True,
                            log_writer=log_writer,
                            spec_id=spec_id,
                        )
                    )
                    asyncio.create_task(
                        service._monitor_process(
                            task_id, new_proc, project_path, spec_id, cmd=None, env=None
                        )
                    )
                    logger.info(
                        f"[AgentService] Task {task_id} restarted with fallback model (sonnet)"
                    )
                    return

        # Special case: Spec creation (project_path provided, spec_id is None)
        # Need to detect the created spec_id and check if it requires review
        if project_path and not spec_id:
            logger.info(
                "[AgentService] Spec creation completed, detecting created spec..."
            )
            try:
                specs_dir = project_path / ".aifactory" / "specs"
                if specs_dir.exists():
                    # Find the newest spec directory (just created)
                    spec_dirs = sorted(
                        [d for d in specs_dir.iterdir() if d.is_dir()],
                        key=lambda d: d.stat().st_mtime,
                        reverse=True,
                    )
                    if spec_dirs:
                        detected_spec_dir = spec_dirs[0]
                        detected_spec_id = detected_spec_dir.name
                        logger.info(
                            f"[AgentService] Detected created spec: {detected_spec_id}"
                        )

                        # Check if this spec requires review
                        review_state_file = detected_spec_dir / "review_state.json"
                        if review_state_file.exists():
                            review_data = json.loads(review_state_file.read_text())
                            if not review_data.get("approved", False):
                                # Spec creation completed, now waiting for review
                                logger.info(
                                    f"[AgentService] Spec {detected_spec_id} requires human review"
                                )

                                # Update plan status to human_review
                                await service._update_plan_status(
                                    project_path,
                                    detected_spec_id,
                                    "human_review",
                                    task_id,
                                )

                                # Clean up tracking data
                                if task_id in service.running_tasks:
                                    del service.running_tasks[task_id]
                                service._task_sequence_numbers.pop(task_id, None)
                                service._last_emitted_task_update.pop(task_id, None)
                                service._task_start_times.pop(task_id, None)
                                service._task_current_phases.pop(task_id, None)
                                service._task_profiles.pop(task_id, None)
                                service._task_subtask_states.pop(task_id, None)

                                # Emit PLAN_REVIEW phase (maps to "human_review" status) — plan_review always scales to 20%
                                await service._emit_progress(
                                    TaskProgress(
                                        task_id=task_id,
                                        phase=TaskPhase.PLAN_REVIEW,
                                        message="Spec created - waiting for human approval",
                                        percentage=100,
                                    ),
                                    previous_phase=TaskPhase.SPEC_CREATION,  # Enable status event emission
                                )

                                logger.info(
                                    f"[AgentService] Spec {detected_spec_id} transitioned to PLAN_REVIEW phase"
                                )
                                return  # Exit early - not a failure

                        # If we reach here, spec was created but doesn't need review
                        # Auto-start task execution immediately
                        logger.info(
                            f"[AgentService] Spec {detected_spec_id} created successfully (no review required) — auto-starting execution"
                        )

                        # Clean up tracking data from spec creation
                        if task_id in service.running_tasks:
                            del service.running_tasks[task_id]
                        service._task_sequence_numbers.pop(task_id, None)
                        service._last_emitted_task_update.pop(task_id, None)
                        service._task_start_times.pop(task_id, None)
                        service._task_current_phases.pop(task_id, None)
                        service._task_profiles.pop(task_id, None)
                        service._task_rate_limits.pop(task_id, None)
                        service._task_subtask_states.pop(task_id, None)

                        # Auto-start task execution
                        try:
                            _par, _wrk = service._read_parallel_opts(
                                project_path, detected_spec_id
                            )
                            await service.start_task_execution(
                                task_id=task_id,
                                project_path=project_path,
                                spec_id=detected_spec_id,
                                auto_continue=True,
                                parallel=_par,
                                workers=_wrk,
                            )
                            logger.info(
                                f"[AgentService] Task execution auto-started for {detected_spec_id}"
                            )
                        except Exception as exec_err:
                            logger.error(
                                f"[AgentService] Failed to auto-start execution for {detected_spec_id}: {exec_err}"
                            )
                            # Fall back to human_review status so user can start manually
                            await service._update_plan_status(
                                project_path, detected_spec_id, "completed", task_id
                            )
                        return  # Exit early
            except Exception as e:
                logger.warning(f"[AgentService] Failed to detect created spec: {e}")
                # Fall through to normal completion handling

        # Check if task is waiting for review (can exit with code 0 or 1)
        # Code 0: auto_continue mode (web UI) - exits cleanly after saving review state
        # Code 1: CLI mode - exits with error when blocked (legacy behavior)
        if project_path and spec_id:
            spec_dir = project_path / ".aifactory" / "specs" / spec_id
            review_state_file = spec_dir / "review_state.json"

            # If review_state.json exists with approved=false, task is waiting for human review
            if review_state_file.exists():
                try:
                    review_data = json.loads(review_state_file.read_text())
                    if not review_data.get("approved", False):
                        # This is NOT a failure - it's waiting for human review!
                        logger.info(
                            f"[AgentService] Task {task_id} awaiting human review (not a failure)"
                        )

                        # Get actual phase BEFORE cleanup
                        actual_phase = service._get_current_phase(task_id)

                        # Finalize log writers for the phase we were in
                        if task_id in service._task_log_writers:
                            log_writer, main_log_writer = service._task_log_writers[
                                task_id
                            ]
                            if spec_id:
                                log_writer.finalize(spec_id, actual_phase)
                                log_writer.set_phase_status(
                                    spec_id, actual_phase, "completed"
                                )
                                main_log_writer.finalize(spec_id, actual_phase)
                                main_log_writer.set_phase_status(
                                    spec_id, actual_phase, "completed"
                                )
                            del service._task_log_writers[task_id]

                        # Update plan status to human_review
                        await service._update_plan_status(
                            project_path, spec_id, "human_review", task_id
                        )

                        # Clean up tracking data
                        if task_id in service.running_tasks:
                            del service.running_tasks[task_id]
                        service._task_sequence_numbers.pop(task_id, None)
                        service._last_emitted_task_update.pop(task_id, None)
                        service._task_start_times.pop(task_id, None)
                        service._task_current_phases.pop(task_id, None)
                        service._task_profiles.pop(task_id, None)
                        service._task_subtask_states.pop(task_id, None)
                        service._spec_dirs.pop(task_id, None)

                        # Determine emit phase based on what phase the task was actually in
                        # If task was coding/QA, it finished implementation → show 100% progress
                        # If task was still planning, it just finished planning → show 20% progress
                        if actual_phase in (
                            TaskPhase.CODING,
                            TaskPhase.QA_REVIEW,
                            TaskPhase.QA_FIXING,
                            TaskPhase.COMPLETED,
                        ):
                            emit_phase = TaskPhase.COMPLETED
                            emit_message = "Task completed - waiting for human review"
                            emit_overall = 100
                        else:
                            emit_phase = TaskPhase.PLAN_REVIEW
                            emit_message = "Plan created - waiting for human approval"
                            emit_overall = None  # Let scale_progress handle it (20%)

                        await service._emit_progress(
                            TaskProgress(
                                task_id=task_id,
                                phase=emit_phase,
                                message=emit_message,
                                percentage=100,
                                overall_progress=emit_overall,
                            ),
                            previous_phase=actual_phase,  # Enable status event emission
                        )

                        logger.info(
                            f"[AgentService] Task {task_id} transitioned to {emit_phase.value} phase (was {actual_phase.value})"
                        )
                        return  # Exit early - not a failure

                except (json.JSONDecodeError, OSError) as e:
                    logger.debug(
                        f"[AgentService] Could not read review_state.json: {e}"
                    )
                    # Fall through to treat as actual failure

        # Check for early failure and attempt profile failover
        if return_code != 0 and project_path and spec_id and cmd and env:
            spec_dir = project_path / ".aifactory" / "specs" / spec_id

            # Check if this is an early failure (no logs written)
            is_early = service._is_early_failure(spec_dir, return_code)
            rate_limit_detected = service._task_rate_limits.get(task_id, False)

            # Check if we should retry (settings enabled + first attempt)
            profile_info = service._task_profiles.get(task_id, {})
            attempt = profile_info.get("attempt", 1)
            should_retry = (
                (is_early or rate_limit_detected)
                and attempt == 1  # Only retry once
                and service._should_retry_with_failover()
            )

            if should_retry:
                failed_profile_id = profile_info.get("profileId")
                reason = "rate_limit" if rate_limit_detected else "early_failure"
                logger.info(
                    f"[AgentService] {reason.replace('_', ' ')} detected for {task_id}, attempting profile failover"
                )

                # Attempt retry with different profile
                if not failed_profile_id:
                    logger.warning(
                        f"[AgentService] No failed profile recorded for {task_id}; cannot failover"
                    )
                    new_proc = None
                else:
                    new_proc = await service._retry_task_with_profile(
                        task_id,
                        project_path,
                        spec_id,
                        cmd,
                        env,
                        failed_profile_id,
                        reason,
                    )

                if new_proc:
                    # Clear the flag for the new attempt so it can detect rate limits again.
                    service._task_rate_limits.pop(task_id, None)

                    # Update running task reference
                    service.running_tasks[task_id] = new_proc

                    # Get log writers for output processing
                    log_writer = None
                    main_log_writer = None
                    if task_id in service._task_log_writers:
                        log_writer, main_log_writer = service._task_log_writers[task_id]

                    # Restart output processing for new subprocess
                    asyncio.create_task(
                        service._process_output(
                            task_id,
                            new_proc.stdout,
                            is_stderr=False,
                            log_writer=log_writer,
                            spec_id=spec_id,
                        )
                    )
                    asyncio.create_task(
                        service._process_output(
                            task_id,
                            new_proc.stderr,
                            is_stderr=True,
                            log_writer=log_writer,
                            spec_id=spec_id,
                        )
                    )

                    # Restart monitoring for new subprocess (without cmd/env to prevent infinite retry)
                    asyncio.create_task(
                        service._monitor_process(
                            task_id,
                            new_proc,
                            project_path,
                            spec_id,
                            cmd=None,  # Prevent second retry
                            env=None,  # Prevent second retry
                        )
                    )

                    logger.info(
                        f"[AgentService] Task {task_id} restarted with alternate profile"
                    )
                    return  # Exit this monitor instance
                else:
                    logger.warning(
                        f"[AgentService] No alternate profile available for task {task_id}, trying model fallback"
                    )

        # If stop_task() already handled cleanup, skip duplicate processing
        if task_id in service._task_stopped:
            service._task_stopped.discard(task_id)
            logger.info(
                f"[AgentService] Task {task_id} was stopped by user, skipping _monitor_process cleanup"
            )
            return

        # Get actual phase BEFORE cleanup (needed for proper status emission)
        actual_phase = service._get_current_phase(task_id)

        # Issue #287: a clean process exit (return_code == 0) does NOT mean
        # the build succeeded. The coder loop exits 0 even when every
        # subtask failed/stuck and no code was produced. Treat a finished
        # build that made zero progress (0 completed + >=1 failed/stuck) as
        # a FAILED build so it lands in human_review + reviewReason
        # "errors" (needs attention) instead of being masked as
        # "completed". Builds with at least one completed subtask keep the
        # genuine success / partial-review path untouched.
        build_succeeded = return_code == 0
        if build_succeeded and spec_id and project_path:
            plan_file = (
                project_path
                / ".aifactory"
                / "specs"
                / spec_id
                / "implementation_plan.json"
            )
            if plan_file.exists():
                try:
                    if is_failed_build(json.loads(plan_file.read_text())):
                        build_succeeded = False
                        logger.warning(
                            f"[AgentService] Build {spec_id} exited cleanly but no "
                            f"subtask completed and at least one failed — marking "
                            f"the build as FAILED (needs attention), not completed."
                        )
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(
                        f"[AgentService] Could not evaluate build success for {spec_id}: {e}"
                    )

        final_status = "completed" if build_succeeded else "failed"

        # Finalize and clean up log writers
        if task_id in service._task_log_writers:
            log_writer, main_log_writer = service._task_log_writers[task_id]

            # Finalize both log writers - set status on the phase the task was actually in
            if spec_id:
                log_writer.finalize(spec_id, actual_phase)
                log_writer.set_phase_status(spec_id, actual_phase, final_status)
                main_log_writer.finalize(spec_id, actual_phase)
                main_log_writer.set_phase_status(spec_id, actual_phase, final_status)

            del service._task_log_writers[task_id]
            logger.debug(f"[AgentService] Finalized task logs for {task_id}")

        # Auto-continuation: if process exited successfully but subtasks remain,
        # restart execution instead of marking as completed (max 10 continuation rounds)
        if return_code == 0 and spec_id and project_path and cmd and env:
            plan_file = (
                project_path
                / ".aifactory"
                / "specs"
                / spec_id
                / "implementation_plan.json"
            )
            if plan_file.exists():
                try:
                    plan_data = json.loads(plan_file.read_text())
                    pending_count = 0
                    completed_count = 0
                    total_count = 0
                    for phase in plan_data.get("phases", []):
                        for subtask in phase.get("subtasks", []):
                            total_count += 1
                            st = subtask.get("status", "pending")
                            if st in ("pending", "in_progress"):
                                pending_count += 1
                            elif st == "completed":
                                completed_count += 1

                    # Track continuation rounds to prevent infinite loops
                    continuation_key = f"_continuation_{task_id}"
                    round_num = getattr(service, continuation_key, 0) + 1

                    if pending_count > 0 and round_num <= max_continuation_rounds:
                        setattr(service, continuation_key, round_num)
                        logger.info(
                            f"[AgentService] Auto-continuation round {round_num}: "
                            f"{completed_count}/{total_count} subtasks done, "
                            f"{pending_count} remaining for {spec_id}"
                        )

                        # Clean up current run tracking
                        if task_id in service.running_tasks:
                            del service.running_tasks[task_id]
                        service._task_sequence_numbers.pop(task_id, None)
                        service._last_emitted_task_update.pop(task_id, None)
                        service._task_start_times.pop(task_id, None)
                        service._task_current_phases.pop(task_id, None)
                        service._task_profiles.pop(task_id, None)
                        service._task_rate_limits.pop(task_id, None)
                        service._task_subtask_states.pop(task_id, None)
                        if task_id in service._task_log_writers:
                            log_writer, main_log_writer = service._task_log_writers[
                                task_id
                            ]
                            if spec_id:
                                actual_phase_for_logs = service._get_current_phase(
                                    task_id
                                )
                                log_writer.finalize(spec_id, actual_phase_for_logs)
                                main_log_writer.finalize(spec_id, actual_phase_for_logs)
                            del service._task_log_writers[task_id]

                        # Restart execution
                        try:
                            _par, _wrk = service._read_parallel_opts(
                                project_path, spec_id
                            )
                            await service.start_task_execution(
                                task_id=task_id,
                                project_path=project_path,
                                spec_id=spec_id,
                                auto_continue=True,
                                parallel=_par,
                                workers=_wrk,
                            )
                            logger.info(
                                f"[AgentService] Auto-continuation started for {spec_id} (round {round_num})"
                            )
                            return  # Exit this monitor — new monitor will take over
                        except Exception as e:
                            logger.error(
                                f"[AgentService] Auto-continuation failed for {spec_id}: {e}"
                            )
                            # Fall through to normal completion
                    elif pending_count > 0 and round_num > max_continuation_rounds:
                        logger.warning(
                            f"[AgentService] Auto-continuation limit reached "
                            f"({max_continuation_rounds} rounds) for {spec_id}, "
                            f"{pending_count} subtasks still pending"
                        )
                    else:
                        # All subtasks done — clean up continuation tracker
                        if hasattr(service, continuation_key):
                            delattr(service, continuation_key)
                        logger.info(
                            f"[AgentService] All {total_count} subtasks completed for {spec_id}"
                        )
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(
                        f"[AgentService] Could not check subtask status for auto-continuation: {e}"
                    )

        # Update implementation_plan.json status for frontend display.
        # emit_events=False (Issue #14): the subsequent _emit_progress
        # call at lines ~1830/1856 is the SINGLE canonical terminal
        # emission. Letting _update_plan_status also emit produced the
        # 5-event flurry + phase:N/A blip — kept the file write here,
        # moved the WebSocket events to the explicit _emit_progress.
        if spec_id and project_path:
            status = "completed" if build_succeeded else "failed"
            logger.info(
                f"[AgentService._monitor_process] About to call _update_plan_status: spec_id={spec_id}, status={status}, task_id={task_id}, project_path={project_path}"
            )
            await service._update_plan_status(
                project_path, spec_id, status, task_id, emit_events=False
            )
            logger.info(
                "[AgentService._monitor_process] _update_plan_status call completed"
            )

        # Send email/in-app notifications on task completion or failure
        _notif_user_id = service._task_user_ids.pop(task_id, "")

        # Emit completion/failure progress with previous_phase to trigger status event
        # NOTE: Cleanup is deferred until AFTER these emissions so _emit_progress
        # can still read _spec_dirs (for plan file), _task_sequence_numbers, and _task_start_times
        # Use build_succeeded (not raw return_code): a clean exit with no
        # successful subtask (Issue #287) is emitted as FAILED so the
        # frontend lands in human_review + "errors" (needs attention)
        # rather than human_review + "completed".
        if build_succeeded:
            await service._emit_progress(
                TaskProgress(
                    task_id=task_id,
                    phase=TaskPhase.COMPLETED,
                    message="Task completed successfully",
                    percentage=100,
                    overall_progress=100,
                ),
                previous_phase=actual_phase,  # Enable status event emission
            )
            if _notif_user_id:
                try:
                    from .notification_service import notification_service

                    _proj_name = project_path.name if project_path else ""
                    _proj_id = task_id.split(":")[0] if ":" in task_id else ""
                    await notification_service.notify(
                        user_id=_notif_user_id,
                        type="task_complete",
                        title=f"Task completed: {spec_id}",
                        message=f"Task {spec_id} in project {_proj_name} completed successfully.",
                        data={"task_id": task_id, "project_id": _proj_id},
                    )
                except Exception:
                    logger.debug(
                        "Failed to send task completion notification", exc_info=True
                    )
        else:
            if return_code == 0:
                fail_message = (
                    "Build finished but no subtask completed — needs attention"
                )
                logger.error(
                    f"[AgentService] Task {task_id} exited cleanly but produced no "
                    f"completed subtasks — treating as failed build (#287)"
                )
            else:
                fail_message = f"Task failed with exit code {return_code}"
                logger.error(
                    f"[AgentService] Task {task_id} failed with exit code {return_code}"
                )
            await service._emit_progress(
                TaskProgress(
                    task_id=task_id,
                    phase=TaskPhase.FAILED,
                    message=fail_message,
                ),
                previous_phase=actual_phase,  # Enable status event emission
            )
            if _notif_user_id:
                try:
                    from .notification_service import notification_service

                    _proj_name = project_path.name if project_path else ""
                    _proj_id = task_id.split(":")[0] if ":" in task_id else ""
                    await notification_service.notify(
                        user_id=_notif_user_id,
                        type="task_failed",
                        title=f"Task failed: {spec_id}",
                        message=f"Task {spec_id} in project {_proj_name} failed: {fail_message}.",
                        data={"task_id": task_id, "project_id": _proj_id},
                    )
                except Exception:
                    logger.debug(
                        "Failed to send task failure notification", exc_info=True
                    )

        # Epic #44 R1 — reap the rmux session if the feature was on.
        # Idempotent + no-op when flag is unset, so safe on every path.
        from ..rmux.integration import reap_if_enabled as _rmux_reap

        _reap_spec_id = task_id.split(":", 1)[1] if ":" in task_id else task_id
        try:
            await _rmux_reap(_reap_spec_id)
        except Exception:
            logger.warning(
                f"[AgentService] rmux reap hook raised (ignored); spec_id={_reap_spec_id}"
            )

        # Clean up tracking data AFTER all emissions are complete
        # This must happen after _emit_progress so it can still read
        # _spec_dirs, _task_sequence_numbers, and _task_start_times
        if task_id in service.running_tasks:
            del service.running_tasks[task_id]
        service._task_sequence_numbers.pop(task_id, None)
        service._last_emitted_task_update.pop(task_id, None)
        service._task_start_times.pop(task_id, None)
        service._task_current_phases.pop(task_id, None)
        service._task_profiles.pop(task_id, None)
        service._task_rate_limits.pop(task_id, None)
        service._task_subtask_states.pop(task_id, None)
        service._spec_dirs.pop(task_id, None)
    except asyncio.CancelledError:
        # Task was cancelled, cleanup already handled by stop_task
        pass
    except Exception as e:
        # Unexpected error, ensure cleanup
        if task_id in service.running_tasks:
            del service.running_tasks[task_id]
        service._task_sequence_numbers.pop(task_id, None)
        service._last_emitted_task_update.pop(task_id, None)
        service._task_start_times.pop(task_id, None)
        service._task_current_phases.pop(task_id, None)
        service._task_user_ids.pop(task_id, None)
        service._task_profiles.pop(task_id, None)
        service._task_rate_limits.pop(task_id, None)
        service._task_subtask_states.pop(task_id, None)
        service._spec_dirs.pop(task_id, None)
        await service._emit_progress(
            TaskProgress(
                task_id=task_id,
                phase=TaskPhase.FAILED,
                message=f"Task monitoring error: {e}",
            )
        )
