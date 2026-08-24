"""Spec-creation entry point — SpecCreationMixin (#703).

start_spec_creation (scaffolds a new spec dir, resolves a pooled credential,
spawns the spec-creation agent subprocess and wires up monitoring), lifted out
of the AgentService god-class into a mixin. AgentService inherits this mixin;
the method runs as a bound method via the MRO, so external callers
(agent.start_spec_creation(...)) are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from factory_common.logsafe import sanitize_log

from server.specpath import spec_dir_for

from ..utils.subprocess_env import make_subprocess_env
from .agent_task_models import TaskProgress
from .task_phase import TaskPhase

if TYPE_CHECKING:
    from collections.abc import Callable


class SpecCreationMixin:
    """Spec-creation entry point for AgentService."""

    if TYPE_CHECKING:
        # Attributes/methods provided by the concrete host (AgentService) and
        # sibling mixins; declared so mypy resolves the self.* refs (#703 pattern).
        backend_path: Any
        running_tasks: dict[str, Any]
        _spec_dirs: dict[str, Any]
        _task_profiles: dict[str, Any]
        _task_sequence_numbers: dict[str, Any]
        _task_start_times: dict[str, Any]
        _task_user_ids: dict[str, Any]
        _emit_progress: Callable[..., Any]
        _monitor_process: Callable[..., Any]
        _process_output: Callable[..., Any]
        _resolve_claude_token_pooled: Callable[..., Any]

    async def start_spec_creation(
        self,
        task_id: str,
        project_path: Path,
        title: str,
        description: str,
        complexity: str | None = None,
        auto_continue: bool = True,
        user_id: str = "",
    ) -> asyncio.subprocess.Process:
        """Start spec creation for a task."""

        logger = logging.getLogger(__name__)
        if task_id in self.running_tasks:
            raise ValueError(f"Task {task_id} is already running")

        # Parse spec_id from task_id (format: "project_id:spec_id")
        if ":" in task_id:
            _, spec_id = task_id.split(":", 1)
            spec_dir = spec_dir_for(project_path, spec_id)
        else:
            # Fallback: no project ID prefix (shouldn't happen in web mode)
            spec_dir = None
            spec_id = None

        # Fix 5: Check if task requires manual review before coding
        # If requireReviewBeforeCoding is true, DON'T auto-approve (let user review the plan)
        should_auto_approve = True  # Default for web mode
        spec_phase_model = None  # Model for spec creation phase
        if spec_dir:
            task_metadata_file = spec_dir / "task_metadata.json"
            if task_metadata_file.exists():
                try:
                    import json

                    metadata = json.loads(task_metadata_file.read_text())
                    if metadata.get("requireReviewBeforeCoding", False):
                        should_auto_approve = False
                        logger.info(
                            "[AgentService] Task %s requires manual review - NOT auto-approving spec",
                            sanitize_log(task_id),
                        )
                    # Read spec phase model from auto profile config
                    if metadata.get("isAutoProfile") and metadata.get("phaseModels"):
                        spec_phase_model = metadata["phaseModels"].get("spec")
                except (json.JSONDecodeError, OSError) as e:
                    # FAIL CLOSED. `should_auto_approve` defaults to True and
                    # the ONLY thing that clears it is
                    # `requireReviewBeforeCoding` above. Swallowing this left it
                    # True, so an unreadable task_metadata.json meant the spec
                    # was AUTO-APPROVED without review -- the opposite of what
                    # the flag exists to do (AIFactory#1384, TFactory#1139).
                    should_auto_approve = False
                    logger.warning(
                        "[AgentService] Could not read task_metadata.json (%s); "
                        "withholding auto-approval so a human decides",
                        sanitize_log(str(e)),
                    )

            # PFactory governed specs (epic #327 / #329): PFactory already ran its
            # architecture/security/best-practice/feasibility gates AND a human
            # approved the plan, so AIFactory skips its own up-front plan-review
            # gate and proceeds straight to execution planning — force
            # auto-approve, overriding any requireReviewBeforeCoding.
            requirements_file = spec_dir / "requirements.json"
            if requirements_file.exists():
                try:
                    import json

                    backend_path = str(self.backend_path)
                    if backend_path not in sys.path:
                        sys.path.insert(0, backend_path)
                    from pfactory.taxonomy import is_governed_requirements

                    requirements = json.loads(requirements_file.read_text())
                    if is_governed_requirements(requirements):
                        should_auto_approve = True
                        logger.info(
                            f"[AgentService] Task {sanitize_log(task_id)} is a governed PFactory "
                            "spec — auto-approving (skipping plan-review gate)"
                        )
                except (json.JSONDecodeError, OSError, ImportError) as e:
                    logger.warning(
                        "[AgentService] PFactory governance check failed for %s: %s",
                        sanitize_log(task_id),
                        sanitize_log(e),
                    )

        # Build command
        cmd = [
            sys.executable,
            str(self.backend_path / "runners" / "spec_runner.py"),
            "--task",
            f"{title}\n\n{description}",
            "--project-dir",
            str(project_path),
        ]

        # Pass spec phase model if configured (multi-model support)
        if spec_phase_model:
            cmd.extend(["--model", spec_phase_model])
            logger.info(
                "[AgentService] [Model: %s] Starting spec creation for %s",
                sanitize_log(spec_phase_model),
                sanitize_log(task_id),
            )
        else:
            logger.info(
                "[AgentService] [Model: sonnet] Starting spec creation for %s (default)",
                sanitize_log(task_id),
            )

        # Fix 1: Only auto-approve if task doesn't require manual review
        if should_auto_approve:
            cmd.append("--auto-approve")

        # Fix 4: Pass existing spec directory to prevent duplicate task creation
        if spec_dir:
            cmd.extend(["--spec-dir", str(spec_dir)])

        if complexity:
            cmd.extend(["--complexity", complexity])

        # Set environment — scrub ANTHROPIC_API_KEY so spawned subprocesses
        # can never silently bill the direct-API account (OAuth-only policy;
        # see apps/backend/core/auth.py).
        env = make_subprocess_env()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # Run Claude in non-interactive mode - bypass permission prompts
        env["CLAUDE_CODE_ENTRYPOINT"] = "cli"  # Signal non-interactive mode
        env["CI"] = "true"  # Many CLI tools use this to detect non-interactive mode

        # Quick Mode for simple tasks (safety net if simple task reaches spec creation)
        if complexity == "simple":
            env["QUICK_MODE"] = "true"
            logger.info(
                f"[AgentService] Quick Mode enabled for spec creation task {sanitize_log(task_id)}"
            )

        # Load backend .env file for graphiti and other settings
        backend_env_file = self.backend_path / ".env"
        if backend_env_file.exists():
            try:
                with open(backend_env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, value = line.split("=", 1)
                            key = key.strip()
                            value = value.strip()
                            # Don't override existing env vars
                            if key not in env:
                                env[key] = value
                logger.info("[AgentService] Loaded backend .env for spec creation")
            except Exception as e:
                logger.warning(f"[AgentService] Failed to load backend .env: {e}")

        # Load project .aifactory/.env for project-level settings (USE_CLAUDE_MD, etc.)
        project_env_file = project_path / ".aifactory" / ".env"
        if project_env_file.exists():
            try:
                with open(project_env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            key, value = line.split("=", 1)
                            key = key.strip()
                            value = value.strip()
                            if key not in env:
                                env[key] = value
                logger.info("[AgentService] Loaded project .env for spec creation")
            except Exception as e:
                logger.warning(f"[AgentService] Failed to load project .env: {e}")

        # Get OAuth token from the pool (#670) so concurrent builds draw DISTINCT
        # credentials; returned to the pool when this build ends.
        token, profile_id, profile_name = self._resolve_claude_token_pooled(task_id)
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            logger.info(
                f"[AgentService] Using Claude profile for spec creation: {profile_name} ({profile_id})"
            )
            # Store for potential retry tracking
            self._task_profiles[task_id] = {
                "profileId": profile_id,
                "profileName": profile_name,
                "attempt": 1,
                "model": spec_phase_model or "sonnet",
            }
        else:
            logger.warning(
                "[AgentService] No Claude OAuth token available for spec creation"
            )
            self._task_profiles[task_id] = {
                "attempt": 1,
                "model": spec_phase_model or "sonnet",
            }

        # Start subprocess with a pseudo-TTY to prevent "Stream closed" errors
        # Claude Code CLI expects a TTY for permission handling
        import pty

        master_fd, slave_fd = pty.openpty()

        # #363: optional OS sandbox — passthrough unless AIFACTORY_AGENT_SANDBOX
        # is set and bwrap is installed (zero behaviour change by default).
        from .sandbox import build_sandboxed_command

        cmd = build_sandboxed_command(cmd, project_path)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=slave_fd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(project_path),
            env=env,
            # Own session/process group so stop_task can kill the WHOLE tree —
            # run.py spawns coder subprocesses (Claude SDK, git); without this,
            # proc.terminate() only signals run.py and the children orphan and
            # keep running (the stop-resistance bug).
            start_new_session=True,
        )

        # Close slave fd in parent process
        os.close(slave_fd)

        self.running_tasks[task_id] = proc

        # Initialize tracking for sequence numbers and start time
        self._task_sequence_numbers[task_id] = 0
        self._task_start_times[task_id] = datetime.now().isoformat()
        if user_id:
            self._task_user_ids[task_id] = user_id
        # Store spec directory for reading implementation plans during progress updates
        self._spec_dirs[task_id] = spec_dir

        # Emit initial progress (50% within spec_creation phase → 10% overall)
        await self._emit_progress(
            TaskProgress(
                task_id=task_id,
                phase=TaskPhase.SPEC_CREATION,
                message="Starting spec creation...",
                percentage=50,
            )
        )

        # Start output processing in background
        asyncio.create_task(self._process_output(task_id, proc.stdout, is_stderr=False))
        asyncio.create_task(self._process_output(task_id, proc.stderr, is_stderr=True))

        # Start process monitor to clean up when finished
        # Pass project_path so monitor can detect created spec and check for review state
        # Pass cmd and env so model fallback can retry with a different model on failure
        asyncio.create_task(
            self._monitor_process(
                task_id, proc, project_path=project_path, cmd=cmd, env=env
            )
        )

        # Epic #44 — Live Console also covers the spec-creation phase, not just
        # the build phase, so the whole agent run is streamable. No-op when
        # rmux is off; _process_output tees this subprocess's output into the
        # passive FIFO. The build phase re-uses the same spec_id session.
        from ..rmux.integration import create_if_enabled as _rmux_create

        if spec_id is not None:
            try:
                await _rmux_create(spec_id, project_path, " ".join(cmd))
            except Exception:
                logger.warning(
                    "[AgentService] rmux create hook (spec creation) raised (ignored); "
                    "spec_id=%s",
                    sanitize_log(spec_id),
                )

        return proc
