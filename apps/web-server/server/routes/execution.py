"""
Task execution routes.

Handles starting, stopping, and monitoring task execution.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..services import task_control
from ..services.agent_service import get_agent_service
from ..websockets.events import emit_task_status
from .project_authz import require_project_access, require_task_access
from .projects import load_projects
from .tasks import _resolve_task, get_next_spec_id, sync_worktree_to_main_spec

# Add the backend dir to sys.path so backend seams (e.g. qa.correction) resolve.
# Mirrors the module-level pattern used by routes/mcp.py et al (the web-server
# PYTHONPATH may not include backend).
_BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from qa.correction import apply_correction  # noqa: E402 — needs sys.path above
from trusted_plan import ingest_trusted_plan  # noqa: E402 — needs sys.path above (#390)

router = APIRouter()


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class StartTaskRequest(BaseModel):
    """Request to start task execution."""

    auto_continue: bool = Field(True, description="Auto-continue to next phase")
    complexity: str | None = Field(
        None, description="Complexity override for spec creation"
    )
    # Task execution options (matches frontend TaskStartOptions)
    parallel: bool | None = Field(None, description="Enable parallel execution")
    workers: int | None = Field(None, description="Number of parallel workers")
    model: str | None = Field(None, description="Model override for execution")
    auto_handover_tfactory: bool = Field(
        False,
        description="Hand the finished build to TFactory for testing on completion (#496)",
    )
    baseBranch: str | None = Field(
        None, description="Base branch for worktree creation"
    )
    mode: str | None = Field(
        "full",
        description="Execution mode: 'quick' for simplified prompts, 'full' for comprehensive",
    )


class ApplyCorrectionRequest(BaseModel):
    """A correction hand-back from an external test tool (e.g. TFactory, #317).

    Fields below ``confirm`` are the additive #467 typed-handback contract. They
    are all optional, so the legacy markdown-only POST keeps working untouched
    until TFactory cuts over to sending the structured ``triage`` block.
    """

    fix_request_md: str = Field(..., description="QA_FIX_REQUEST.md body to apply")
    source: str | None = Field(
        None, description="Origin, e.g. 'triage' or 'visual_inspection'"
    )
    confirm: bool = Field(
        False, description="Required true to write + run the QA Fixer"
    )
    # Typed handback triage validation (#467) — additive / backward-compatible.
    triage: dict | None = Field(
        None,
        description="Structured triage report (TFactory#283). When present it is "
        "schema-validated before the QA Fixer runs; malformed reports are rejected.",
    )
    manifest_hash: str | None = Field(
        None, description="TFactory assertion-manifest hash, recorded for audit"
    )
    correlation_key: str | None = Field(
        None, description="RFC-0001 correlation key for keyed observability"
    )
    # Echoed by TFactory for the auto-loop callback (accepted, not yet acted on).
    tfactory_task_id: str | None = Field(None, description="TFactory workspace id")
    tfactory_callback_url: str | None = Field(
        None, description="URL AIFactory calls back when the fix completes"
    )


class TaskProvenance(BaseModel):
    """Upstream provenance for a handed-off task (e.g. from PFactory, #332).

    Persisted onto the spec so the correlation chain (PFactory plan/session →
    GitHub issue → AIFactory spec) is traversable downstream.
    """

    session_id: str | None = Field(None, description="Upstream session/plan id")
    issue_number: int | None = Field(
        None, description="Originating GitHub issue number"
    )
    repo: str | None = Field(None, description="Originating repo, e.g. 'owner/name'")
    source: str | None = Field(None, description="Origin system, e.g. 'pfactory'")
    # Trusted Plan Handoff (#390): a verifiable approval over a handed-off
    # implementation_plan.json. When present and verified, the build skips the
    # spec pipeline. A plain label is spoofable; the signature is tamper-evident.
    trusted_plan: bool = Field(
        False, description="Whether this task carries a verified trusted plan"
    )
    approved_by: str | None = Field(
        None, description="Signing authority, e.g. 'cfactory' or 'pfactory'"
    )
    approval_timestamp: str | None = Field(
        None, description="ISO-8601 time the plan was approved/signed"
    )
    plan_contract_version: str | None = Field(
        None, description="Trusted-plan contract version the signer used"
    )
    signature: str | None = Field(
        None, description="HMAC signature over the canonical plan JSON"
    )


class CreateAndRunRequest(StartTaskRequest):
    """Body for create-and-run; adds optional upstream provenance (#332)."""

    provenance: TaskProvenance | None = Field(
        None, description="Upstream provenance (session_id, issue#) to persist"
    )


class FromPlanRequest(StartTaskRequest):
    """Trusted Plan Handoff (#390): build directly from a signed, vetted plan.

    The plan must embed an ``approval`` envelope (see ``trusted_plan.sign_plan``)
    that verifies against an authority key in the environment. On success the
    spec pipeline is skipped and the build starts immediately.
    """

    plan: dict = Field(
        ..., description="Signed implementation_plan.json (incl. 'approval')"
    )
    provenance: TaskProvenance | None = Field(
        None, description="Upstream provenance to persist alongside the approval"
    )


class RecoverTaskRequest(BaseModel):
    """Request to recover a stuck task."""

    targetStatus: str | None = Field(
        "backlog", description="Target status after recovery"
    )
    autoRestart: bool = Field(False, description="Auto-restart the task after recovery")


class CopilotDispatchRequest(BaseModel):
    """Request to delegate a task to the GitHub Copilot cloud agent."""

    repo_full_name: str = Field(
        ..., description="GitHub repository in 'owner/repo' format"
    )
    issue_number: int = Field(
        ..., description="GitHub issue number to assign to Copilot"
    )
    fallback_to_local: bool = Field(
        True,
        description="If True, fall back to the local AIFactory pipeline on dispatch failure",
    )


class TaskExecutionStatus(BaseModel):
    """Task execution status response."""

    task_id: str
    is_running: bool
    phase: str | None = None
    message: str | None = None


class RunningTasksResponse(BaseModel):
    """Response listing all running tasks."""

    tasks: list[str]
    count: int


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.get("/running", response_model=RunningTasksResponse)
async def get_running_tasks():
    """Get list of all currently running tasks."""
    agent_service = get_agent_service()
    running = agent_service.get_running_tasks()
    return RunningTasksResponse(tasks=running, count=len(running))


@router.get("/{task_id}/status", response_model=TaskExecutionStatus)
async def get_task_status(
    task_id: str,
    _access: dict = Depends(require_task_access("viewer")),
):
    """Get execution status for a specific task."""
    agent_service = get_agent_service()
    is_running = agent_service.is_running(task_id)

    return TaskExecutionStatus(
        task_id=task_id,
        is_running=is_running,
    )


@router.get("/{task_id}/running")
async def is_task_running(
    task_id: str,
    _access: dict = Depends(require_task_access("viewer")),
):
    """Check if a specific task is currently running."""
    agent_service = get_agent_service()
    is_running = agent_service.is_running(task_id)

    return {
        "task_id": task_id,
        "is_running": is_running,
    }


@router.post("/{task_id}/start")
async def start_task(
    task_id: str,
    request: StartTaskRequest,
    raw_request: Request,
    _access: dict = Depends(require_task_access("member")),
):
    """Start execution of a task.

    The task must already exist (have a spec directory).
    This will run the planner, coder, and QA agents.
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.info(f"[StartTask] ===== START ENDPOINT CALLED ===== task_id: {task_id}")

    # Parse task ID
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format. Expected 'project_id:spec_id'",
        )

    project_id, spec_id = task_id.split(":", 1)
    projects = load_projects()

    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_path = Path(projects[project_id]["path"])
    spec_dir = project_path / ".aifactory" / "specs" / spec_id

    if not spec_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task/spec not found",
        )

    # PFactory taxonomy routing (epic #327 / #331): a governed child labelled
    # `handoff:tfactory` (or `type:testing`) is test-generation work — route it
    # to TFactory instead of running the AIFactory coder. We record the handoff
    # as a marker on the spec and return early; the coder is never spawned.
    if str(_BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(_BACKEND_DIR))
    try:
        import json as _json

        from pfactory.metadata import load_pfactory_metadata
        from pfactory.routing import TFACTORY, routing_target
        from pfactory.taxonomy import classify_requirements
        from pfactory.tfactory_client import (
            build_handoff_payload,
            load_tfactory_block,
            send_handoff,
        )

        _req_file = spec_dir / "requirements.json"
        if _req_file.exists():
            _req = _json.loads(_req_file.read_text())
            _classification = classify_requirements(_req)
            if routing_target(_classification) == TFACTORY:
                # Outbound transport (#337): POST the spec + pfactory:meta to
                # TFactory. Graceful — when TFACTORY_BASE_URL is unset this is a
                # no-op ("not_configured") and we still record the local marker.
                _meta = load_pfactory_metadata(spec_dir, _req)
                # RFC-0002: carry the contract's tfactory test profile so TFactory
                # plans from declared lanes/frameworks/endpoints, not inference.
                _tf = load_tfactory_block(spec_dir)
                _payload = build_handoff_payload(
                    spec_id,
                    _req,
                    _classification,
                    _meta,
                    tfactory=_tf,
                    spec_dir=spec_dir,  # #476: carry the mutation ledger as evidence
                )
                transport = await send_handoff(_payload)

                marker = spec_dir / "TFACTORY_HANDOFF.md"
                marker.write_text(
                    "# Routed to TFactory\n\n"
                    "This spec is test-generation work (`handoff:tfactory` / "
                    "`type:testing`) and was routed to TFactory rather than the "
                    "AIFactory coder.\n\n"
                    f"- handoff: {_classification.handoff}\n"
                    f"- types: {', '.join(_classification.types) or '(none)'}\n"
                    f"- transport: sent={transport.get('sent')} "
                    f"reason={transport.get('reason')}\n"
                )
                logger.info(
                    f"[StartTask] {task_id} routed to TFactory "
                    f"(coder not started); transport sent={transport.get('sent')} "
                    f"reason={transport.get('reason')}"
                )
                return {
                    "success": True,
                    "task_id": task_id,
                    "routed_to": "tfactory",
                    "transport": transport,
                    "message": (
                        "Routed to TFactory for test generation; the AIFactory "
                        "coder was not started."
                    ),
                }
    except (json.JSONDecodeError, OSError, ImportError) as e:
        logger.warning(f"[StartTask] PFactory routing check failed for {task_id}: {e}")

    # Fix 3: Check if a VALID implementation_plan.json exists - if not, run spec creation first
    # This handles the case where projects.py created the spec directory but spec_runner.py hasn't run yet
    # A valid plan MUST have "phases" array - minimal plans with just {"status": "..."} are invalid
    import logging

    logger = logging.getLogger(__name__)
    implementation_plan = spec_dir / "implementation_plan.json"
    logger.info(
        f"[StartTask] Checking for implementation_plan.json at {implementation_plan}"
    )
    logger.info(
        f"[StartTask] implementation_plan.json exists: {implementation_plan.exists()}"
    )

    # Check if plan is valid (has phases/subtasks structure)
    plan_is_valid = False
    if implementation_plan.exists():
        try:
            import json

            plan_data = json.loads(implementation_plan.read_text())
            # Valid plan must have "phases" key (even if empty array)
            plan_is_valid = "phases" in plan_data and isinstance(
                plan_data.get("phases"), (list, dict)
            )
            logger.info(
                f"[StartTask] Plan validity check: has_phases={plan_is_valid}, keys={list(plan_data.keys())}"
            )

            # Guard against re-starting a completed task
            if plan_data.get("status") == "done":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot start a completed task. Reset the task status first.",
                )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[StartTask] Failed to parse implementation_plan.json: {e}")
            plan_is_valid = False

    if not implementation_plan.exists() or not plan_is_valid:
        # Need to run spec creation first - read title/description from requirements.json
        import json
        from datetime import datetime

        logger.info(
            f"[StartTask] No valid implementation plan found, will run spec creation for {task_id}"
        )
        requirements_file = spec_dir / "requirements.json"
        if not requirements_file.exists():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Task has no implementation plan and no requirements.json for spec creation",
            )

        try:
            requirements = json.loads(requirements_file.read_text())
            title = requirements.get("title", spec_id)
            description = requirements.get("description", "")
        except json.JSONDecodeError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid requirements.json format",
            )

        # Read complexity from request, or fall back to task metadata
        complexity = request.complexity
        if not complexity:
            metadata = requirements.get("metadata", {})
            meta_complexity = metadata.get("complexity")
            if meta_complexity in ("simple", "standard", "complex"):
                complexity = meta_complexity

        # === FAST PATH: Simple tasks skip spec creation entirely ===
        if complexity == "simple":
            logger.info(
                f"[StartTask] Simple task fast path: generating spec + plan programmatically for {task_id}"
            )

            # 1. Generate minimal spec.md
            spec_file = spec_dir / "spec.md"
            if not spec_file.exists():
                spec_content = f"# {title}\n\n{description}\n"
                spec_file.write_text(spec_content)

            # 2. Generate implementation_plan.json with 1 subtask
            plan_data = {
                "feature": title,
                "workflow_type": "feature",
                "status": "in_progress",
                "current_phase": "coding",
                "phases": [
                    {
                        "phase": 1,
                        "name": "Implementation",
                        "subtasks": [
                            {
                                "id": "1.1",
                                "description": f"{title}: {description}"
                                if description
                                else title,
                                "status": "pending",
                            }
                        ],
                    }
                ],
                "last_updated": datetime.now().isoformat(),
            }
            implementation_plan.write_text(json.dumps(plan_data, indent=2))

            # 3. Pre-approve (skip review gate)
            review_state_file = spec_dir / "review_state.json"
            review_state_file.write_text(
                json.dumps(
                    {
                        "approved": True,
                        "approved_by": "auto-simple",
                        "approved_at": datetime.now().isoformat(),
                    },
                    indent=2,
                )
            )

            # 4. Set task_metadata for quick mode + reduced thinking
            task_metadata_file = spec_dir / "task_metadata.json"
            task_metadata = {}
            if task_metadata_file.exists():
                try:
                    task_metadata = json.loads(task_metadata_file.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            task_metadata["complexity"] = "simple"
            task_metadata["mode"] = "quick"
            task_metadata["isAutoProfile"] = True
            task_metadata["phaseThinking"] = {
                "spec": "low",
                "planning": "low",
                "coding": "medium",
                "qa": "low",
            }
            task_metadata_file.write_text(json.dumps(task_metadata, indent=2))

            # Mark plan as valid so we fall through to the execution path below
            plan_is_valid = True
            logger.info(
                "[StartTask] Fast path: spec + plan generated, proceeding to execution"
            )

        else:
            # === STANDARD PATH: Run full spec creation ===
            agent_service = get_agent_service()

            try:
                await agent_service.start_spec_creation(
                    task_id=task_id,
                    project_path=project_path,
                    title=title,
                    description=description,
                    complexity=complexity,
                    auto_continue=request.auto_continue,
                )

                # Persist status to implementation_plan.json for page refresh survival
                # Create minimal plan file if it doesn't exist
                try:
                    if implementation_plan.exists():
                        plan = json.loads(implementation_plan.read_text())
                    else:
                        plan = {}
                    plan["status"] = "in_progress"
                    plan["phase"] = "spec_creation"
                    implementation_plan.write_text(json.dumps(plan, indent=2))
                    logger.info(
                        f"[StartTask] Persisted status=in_progress (spec creation) to {implementation_plan}"
                    )
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(
                        f"[StartTask] Failed to persist spec creation status: {e}"
                    )

                # Emit status to show spec creation in progress
                await emit_task_status(task_id, "in_progress")
                return {
                    "success": True,
                    "task_id": task_id,
                    "message": "Spec creation started (no implementation plan found)",
                }
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to start spec creation: {str(e)}",
                )

    # Sync runtime options to task_metadata.json for backend to read
    # This ensures model/thinking/baseBranch overrides are available to run.py
    import json

    task_metadata_file = spec_dir / "task_metadata.json"
    task_metadata = {}
    if task_metadata_file.exists():
        try:
            task_metadata = json.loads(task_metadata_file.read_text())
        except json.JSONDecodeError:
            pass

    # Apply runtime overrides
    if request.model:
        task_metadata["model"] = request.model
    if request.baseBranch:
        task_metadata["baseBranch"] = request.baseBranch
    # Persist parallel/workers so the spec→plan→build auto-continue honors them
    # (#392) — the handoff reads these from task_metadata via _read_parallel_opts.
    if request.parallel is not None:
        task_metadata["parallel"] = request.parallel
    if request.workers is not None:
        task_metadata["workers"] = request.workers
    # Opt-in: auto-handover the finished build to TFactory for testing (#496).
    if getattr(request, "auto_handover_tfactory", False):
        task_metadata["auto_handover_tfactory"] = True

    # Write updated task_metadata.json if we have any settings
    if task_metadata:
        task_metadata_file.write_text(json.dumps(task_metadata, indent=2))

    # Determine mode: use request mode if provided, otherwise fall back to task_metadata
    effective_mode = request.mode
    if not effective_mode or effective_mode == "full":
        # Check if mode was set during task creation
        effective_mode = task_metadata.get("mode", "full")

    # Auto-derive quick mode from simple complexity
    if effective_mode == "full":
        task_complexity = task_metadata.get("complexity")
        if task_complexity == "simple":
            effective_mode = "quick"
            logger.info("[StartTask] Auto-derived quick mode from simple complexity")

    # Resolve parallel execution options (#376). Prefer the explicit request
    # value, then fall back to task_metadata (set at task creation). These were
    # previously accepted by StartTaskRequest but never threaded to the executor.
    effective_parallel = request.parallel
    if effective_parallel is None:
        effective_parallel = task_metadata.get("parallel", False)
    effective_workers = request.workers
    if effective_workers is None:
        effective_workers = task_metadata.get("workers")
    if effective_parallel:
        logger.info(
            f"[StartTask] Parallel execution requested (workers={effective_workers or 'default'})"
        )

    agent_service = get_agent_service()

    # Check if plan was manually approved - if so, use --force to bypass review check
    force_execution = False
    review_state_file = spec_dir / "review_state.json"
    if review_state_file.exists():
        try:
            review_data = json.loads(review_state_file.read_text())
            if review_data.get("approved", False):
                force_execution = True
                logger.info(
                    f"[StartTask] Plan was manually approved for {task_id}, using --force"
                )
        except (json.JSONDecodeError, OSError):
            pass

    if agent_service.is_running(task_id):
        if force_execution:
            # Plan was approved — clean up stale spec creation process before starting execution
            logger.info(
                f"[StartTask] Cleaning up stale spec creation process for approved task {task_id}"
            )
            try:
                await agent_service.stop_task(task_id)
            except Exception as stop_err:
                logger.warning(f"[StartTask] Failed to stop stale process: {stop_err}")
                agent_service.running_tasks.pop(task_id, None)
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Task is already running",
            )

    # If review is required but not yet approved, set human_review status
    # and return early WITHOUT starting the subprocess (which would just exit)
    if not force_execution:
        task_metadata_file = spec_dir / "task_metadata.json"
        require_review = False
        if task_metadata_file.exists():
            try:
                tm = json.loads(task_metadata_file.read_text())
                require_review = tm.get("requireReviewBeforeCoding", False)
            except (json.JSONDecodeError, OSError):
                pass

        if require_review:
            try:
                if implementation_plan.exists():
                    plan = json.loads(implementation_plan.read_text())
                    plan["status"] = "human_review"
                    plan["reviewReason"] = "plan_review"
                    implementation_plan.write_text(json.dumps(plan, indent=2))
                    logger.info(
                        f"[StartTask] Plan requires approval for {task_id}, set human_review"
                    )
                # Issue #259: control-plane state is authoritative in the
                # agent-immutable store.
                task_control.write_control(
                    spec_dir,
                    status="human_review",
                    review_reason="plan_review",
                    updated_by="web_server",
                )
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    f"[StartTask] Failed to persist human_review status: {e}"
                )

            await emit_task_status(task_id, "human_review", "plan_review")

            return {
                "success": True,
                "task_id": task_id,
                "message": "Task requires plan approval before coding can begin",
                "status": "human_review",
                "reviewReason": "plan_review",
            }

    # Extract user_id from auth context for email notifications
    _user = getattr(raw_request.state, "user", None)
    _user_id = _user["id"] if isinstance(_user, dict) and _user.get("id") else ""

    # Delegation branch (gap #1 from #144) — when the task asks for
    # Copilot delegation AND the project's git provider is GitHub AND we
    # know which issue this task came from, hand off to the shared
    # delegation runner instead of running the local coder/QA pipeline.
    settings = projects[project_id].get("settings") or {}
    provider_type = (settings.get("gitProvider") or "github").lower()
    wants_delegation = bool(task_metadata.get("enableDelegation"))
    issue_number = task_metadata.get("githubIssueNumber")
    if isinstance(issue_number, str) and issue_number.isdigit():
        issue_number = int(issue_number)

    if (
        wants_delegation
        and provider_type in ("github", "gitlab")
        and isinstance(issue_number, int)
    ):
        from ..services.auto_fix_service import _provider_for
        from ..services.delegation_runner import run_delegation

        try:
            provider = _provider_for(project_id)
            result = await run_delegation(
                project_id=project_id,
                project_path=project_path,
                spec_id=spec_id,
                issue_number=int(issue_number),
                provider=provider,
            )
        except Exception as e:
            logger.exception(f"[StartTask] Delegation failed for {task_id}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Delegation failed: {e}",
            )
        return {
            "success": True,
            "task_id": task_id,
            "status": "delegated",
            "delegatedAt": result["delegatedAt"],
            "commentPosted": result["commentPosted"],
            "commentSkippedAsDuplicate": result["commentSkippedAsDuplicate"],
            "copilotAssigned": result["copilotAssigned"],
        }

    if wants_delegation and not isinstance(issue_number, int):
        logger.warning(
            "[StartTask] Task %s has enableDelegation=true but no githubIssueNumber "
            "in metadata — falling through to local execution",
            task_id,
        )

    try:
        proc = await agent_service.start_task_execution(
            task_id=task_id,
            project_path=project_path,
            spec_id=spec_id,
            auto_continue=request.auto_continue,
            base_branch=request.baseBranch,
            mode=effective_mode,
            force=force_execution,
            user_id=_user_id,
            parallel=effective_parallel,
            workers=effective_workers,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start task: {str(e)}",
        )

    # RFC-0016 #668: a None return means the build was admitted to the
    # concurrency queue (at the global cap) rather than started immediately.
    # It is NOT a failure — the queued status is already persisted/emitted by
    # the service, and the exit monitor auto-starts it FIFO when a slot frees.
    if proc is None:
        return {
            "success": True,
            "task_id": task_id,
            "status": "queued",
            "message": "Task queued — at concurrency cap, will start when a slot frees",
        }

    # Persist status to implementation_plan.json for page refresh survival
    # This ensures the task shows as "in_progress" even after browser refresh
    try:
        if implementation_plan.exists():
            plan = json.loads(implementation_plan.read_text())
            plan["status"] = "in_progress"
            implementation_plan.write_text(json.dumps(plan, indent=2))
            logger.info(
                f"[StartTask] Persisted status=in_progress to {implementation_plan}"
            )
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[StartTask] Failed to persist status: {e}")

    # Emit status change for real-time frontend update
    await emit_task_status(task_id, "in_progress")

    return {
        "success": True,
        "task_id": task_id,
        "message": "Task execution started",
    }


@router.post("/{task_id}/handoff-tfactory")
async def handoff_to_tfactory(
    task_id: str,
    _access: dict = Depends(require_task_access("member")),
) -> dict:
    """Push the finished build branch + hand the spec off to TFactory for
    SOURCE-AWARE verification (PARR seam).

    Reuses the same machinery as the auto-handoff on completion
    (``build_ingest_payload`` pushes the build branch to origin and assembles the
    ``git_url`` / ``source_branch`` / ``contract`` payload; ``send_handoff`` POSTs
    it to TFactory's ``/api/specs/ingest``), but is callable ON DEMAND — e.g. for
    a build parked at ``human_review`` (auto-merge off), which never fires the
    COMPLETED-only auto-handoff. Without this, the only way to verify such a build
    was a text-only spec-ingest with no SUT to run tests against (hollow verify).

    Returns the send result plus the TFactory spec/project id + the pushed branch
    so the caller can poll TFactory for the verdict.
    """
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format. Expected 'project_id:spec_id'",
        )
    project_id, spec_id = task_id.split(":", 1)
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    project_path = Path(projects[project_id]["path"])
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    if not spec_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task/spec not found"
        )

    from pfactory.tfactory_client import build_ingest_payload, send_handoff

    payload = build_ingest_payload(spec_dir, spec_id)
    result = await send_handoff(payload)
    try:
        (spec_dir / "tfactory_handoff.json").write_text(json.dumps(result, indent=2))
    except OSError:
        pass
    return {
        **result,
        "tfactory_spec_id": payload.get("spec_id"),
        "tfactory_project_id": payload.get("project_id"),
        "source_branch": payload.get("source_branch"),
        "git_url": payload.get("git_url"),
    }


@router.post("/{task_id}/stop")
async def stop_task(
    task_id: str,
    _access: dict = Depends(require_task_access("member")),
):
    """Stop a running task."""
    agent_service = get_agent_service()

    if not agent_service.is_running(task_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task is not running",
        )

    success = await agent_service.stop_task(task_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to stop task",
        )

    # Emit status change for real-time frontend update
    await emit_task_status(task_id, "backlog")

    return {
        "success": True,
        "task_id": task_id,
        "message": "Task stopped",
    }


@router.post("/{task_id}/recover")
async def recover_task(
    task_id: str,
    request: RecoverTaskRequest = RecoverTaskRequest(),
    _access: dict = Depends(require_task_access("member")),
):
    """Recover a stuck task by resetting its status.

    Use this when a task shows as running but the process has died.
    Optionally auto-restart the task after recovery.
    """

    # Parse task ID
    if ":" not in task_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid task ID format. Expected 'project_id:spec_id'",
        )

    project_id, spec_id = task_id.split(":", 1)
    projects = load_projects()

    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_path = Path(projects[project_id]["path"])
    spec_dir = project_path / ".aifactory" / "specs" / spec_id

    if not spec_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task/spec not found",
        )

    # Clean up from running_tasks if present
    agent_service = get_agent_service()
    if task_id in agent_service.running_tasks:
        try:
            proc = agent_service.running_tasks[task_id]
            proc.terminate()
            await proc.wait()
        except Exception:
            pass
        # Only delete if still present (might have been cleaned up by monitor)
        if task_id in agent_service.running_tasks:
            del agent_service.running_tasks[task_id]

    # Sync from worktree to main spec first to preserve progress
    sync_worktree_to_main_spec(project_path, spec_id)

    # Reset status in implementation_plan.json
    plan_file = spec_dir / "implementation_plan.json"
    plan = {}
    if plan_file.exists():
        try:
            plan = json.loads(plan_file.read_text())
        except json.JSONDecodeError:
            pass

    # Reset status from request body or default to backlog
    reset_status = request.targetStatus or "backlog"
    auto_restart = request.autoRestart
    auto_restarted = False
    auto_restart_error = None

    # Reset any reviewReason when moving out of human review states
    clears_review = reset_status in ("backlog", "in_progress", "ai_review", "done")
    if clears_review:
        plan.pop("reviewReason", None)

    plan["status"] = reset_status
    plan_file.write_text(json.dumps(plan, indent=2))

    # Issue #259: control-plane state is authoritative in the agent-immutable store.
    task_control.write_control(
        spec_dir,
        status=reset_status,
        clear_review_reason=clears_review,
        updated_by="web_user",
    )

    # Auto-restart if requested
    if auto_restart:
        try:
            await agent_service.start_task_execution(
                task_id=task_id,
                project_path=project_path,
                spec_id=spec_id,
                auto_continue=True,
            )
            auto_restarted = True
            reset_status = "in_progress"

            # Persist updated status so UI doesn't immediately revert to backlog on refresh
            plan["status"] = reset_status
            plan.pop("reviewReason", None)
            plan_file.write_text(json.dumps(plan, indent=2))
            task_control.write_control(
                spec_dir,
                status=reset_status,
                clear_review_reason=True,
                updated_by="web_server",
            )
        except Exception as e:
            # If auto-restart fails, still return success for recovery
            import logging

            logging.getLogger(__name__).warning(
                f"Auto-restart failed for {task_id}: {e}"
            )
            auto_restart_error = str(e)

    # Emit status change via WebSocket (single final status to avoid UI flicker)
    await emit_task_status(task_id, reset_status)

    # Return wrapped response to match frontend expectations
    return {
        "success": True,
        "data": {
            "task_id": task_id,
            "message": "Task recovered"
            + (" and restarted" if auto_restarted else f" and reset to {reset_status}"),
            "newStatus": reset_status,
            "autoRestarted": auto_restarted,
            "autoRestartError": auto_restart_error,
            "recovered": True,
        },
    }


@router.post("/create-and-run")
async def create_and_run_task(
    project_id: str,
    title: str,
    description: str,
    request: CreateAndRunRequest,
    _access: dict = Depends(require_project_access("member")),
):
    """Create a new task and immediately start execution.

    This is a convenience endpoint that combines task creation
    with spec creation and execution.
    """
    projects = load_projects()

    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    project_path = Path(projects[project_id]["path"])
    agent_service = get_agent_service()

    # Create the spec up front with a deterministic, board-trackable id (NNN-slug)
    # and seed requirements.json — mirroring the create-task path. Previously this
    # used a temporary "pending-<uuid>" id; the spec orchestrator renames any
    # "pending" directory once requirements are gathered
    # (rename_spec_dir_from_requirements), which orphaned the tracked task as
    # "stuck" and produced a doubled-slug skeleton. A non-pending id is never
    # renamed, so the board task id and the spec dir stay in sync. (#232)
    specs_dir = project_path / ".aifactory" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    spec_id = get_next_spec_id(project_path, title)
    spec_dir = specs_dir / spec_id
    spec_dir.mkdir(exist_ok=True)
    requirements: dict = {
        "title": title,
        "description": description,
        "created_at": datetime.now().isoformat(),
    }
    # Persist upstream provenance (#332) so the PFactory→issue→spec chain is
    # traversable. Optional — omitted entirely when no fields are provided.
    if request.provenance is not None:
        prov = request.provenance.model_dump(exclude_none=True, exclude_defaults=True)
        if prov:
            requirements["provenance"] = prov
    (spec_dir / "requirements.json").write_text(json.dumps(requirements, indent=2))

    # Persist execution options so the spec→plan→build auto-continue honors them
    # (#392). The build handoff reads parallel/workers from task_metadata.json
    # via AgentService._read_parallel_opts; without this, create-and-run builds
    # run serial (workers.max=1) even when parallel:true/workers:N was requested,
    # making the #376 wave executor unreachable from the primary web flow.
    task_metadata: dict = {}
    if request.parallel is not None:
        task_metadata["parallel"] = request.parallel
    if request.workers is not None:
        task_metadata["workers"] = request.workers
    # Opt-in: auto-handover the finished build to TFactory for testing (#496).
    if getattr(request, "auto_handover_tfactory", False):
        task_metadata["auto_handover_tfactory"] = True
    if request.mode and request.mode != "full":  # "full" is the default — don't persist
        task_metadata["mode"] = request.mode
    if request.model:
        task_metadata["model"] = request.model
    if request.complexity:
        task_metadata["complexity"] = request.complexity

    # Hybrid skill auto-selection (#394) — propose step. With no manual skills,
    # rank relevant skills from the task description and persist them as
    # suggestedSkills. The planner may refine them into selectedSkills (confirm);
    # otherwise the build falls back to these proposals (see
    # AgentService._write_skill_context). No LLM cost — deterministic matcher.
    if not task_metadata.get("selectedSkills"):
        try:
            from server.services.skills_service import get_skills_service

            suggested = get_skills_service().suggest_selected_skills(
                f"{title}\n{description}", max_results=5
            )
            if suggested:
                task_metadata["suggestedSkills"] = suggested
        except Exception:  # noqa: BLE001 - skill proposal is best-effort
            pass

    if task_metadata:
        (spec_dir / "task_metadata.json").write_text(
            json.dumps(task_metadata, indent=2)
        )

    task_id = f"{project_id}:{spec_id}"

    try:
        await agent_service.start_spec_creation(
            task_id=task_id,
            project_path=project_path,
            title=title,
            description=description,
            complexity=request.complexity,
            auto_continue=request.auto_continue,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start task creation: {str(e)}",
        )

    return {
        "success": True,
        "task_id": task_id,
        "message": "Task creation started. Connect to WebSocket for progress updates.",
    }


@router.post("/from-plan")
async def create_from_trusted_plan(
    project_id: str,
    title: str,
    description: str,
    request: FromPlanRequest,
    _access: dict = Depends(require_project_access("member")),
):
    """Trusted Plan Handoff (#390): verify a signed plan and build directly.

    Verifies the plan's signature + completeness checklist. If trusted-complete,
    installs ``implementation_plan.json``, marks the spec approved, and starts
    the build — bypassing the full spec pipeline (discovery → … → plan). A
    tampered, unsigned, or incomplete plan is rejected with 422.
    """
    projects = load_projects()
    if project_id not in projects:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )

    project_path = Path(projects[project_id]["path"])
    agent_service = get_agent_service()

    specs_dir = project_path / ".aifactory" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    spec_id = get_next_spec_id(project_path, title)
    spec_dir = specs_dir / spec_id
    spec_dir.mkdir(exist_ok=True)

    requirements: dict = {
        "title": title,
        "description": description,
        "created_at": datetime.now().isoformat(),
    }
    if request.provenance is not None:
        prov = request.provenance.model_dump(exclude_none=True, exclude_defaults=True)
        if prov:
            requirements["provenance"] = prov
    # RFC-0001 correlation: stamp the GitHub issue (from the contract's numeric
    # correlation_key) into the FIRST requirements.json write — before the task is
    # ever listed — so the cockpit's AIFactory adapter reads githubIssueNumber from
    # the very first poll. Otherwise an early poll sees None and CFactory mints an
    # orphaned `af-<spec>` duplicate card that never threads. (ingest's
    # _record_approval_provenance preserves this issue_number.)
    corr = (
        request.plan.get("correlation_key") if isinstance(request.plan, dict) else None
    )
    if corr is not None and str(corr).isdigit():
        prov = requirements.get("provenance")
        prov = prov if isinstance(prov, dict) else {}
        prov.setdefault("issue_number", int(corr))
        requirements["provenance"] = prov
    (spec_dir / "requirements.json").write_text(json.dumps(requirements, indent=2))

    # Gate: verify signature + completeness, then install the plan. Nothing is
    # built unless the plan is trusted-complete. Passing project_path lets ingest
    # seed required_commands into the allowlist and apply the v2 execution profile
    # (model/parallel/workers/complexity/skills) to task_metadata.json (RFC-0002).
    result = ingest_trusted_plan(spec_dir, request.plan, project_dir=project_path)
    if not result.ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Plan rejected — not trusted-complete",
                "reasons": result.reasons,
            },
        )

    # RFC-0001 correlation: ingest stamped {approved_by, trusted_plan} provenance;
    # also record the GitHub issue number from the contract's correlation_key so
    # the task list exposes Task.github_issue and the cockpit threads plan→code→test.
    corr = (
        request.plan.get("correlation_key") if isinstance(request.plan, dict) else None
    )
    if corr is not None and str(corr).isdigit():
        req_file = spec_dir / "requirements.json"
        try:
            reqs = json.loads(req_file.read_text())
            prov = reqs.get("provenance")
            prov = prov if isinstance(prov, dict) else {}
            prov.setdefault("issue_number", int(corr))
            reqs["provenance"] = prov
            req_file.write_text(json.dumps(reqs, indent=2))
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    # Honor the contract's execution profile when the HTTP request doesn't
    # override it: a v2 contract carries parallel/workers in its `execution`
    # block, so a caller can hand off a fully-specified plan with an empty body.
    _execution = request.plan.get("execution") or {}
    eff_parallel = (
        request.parallel if request.parallel is not None else _execution.get("parallel")
    )
    eff_workers = (
        request.workers if request.workers is not None else _execution.get("workers")
    )

    task_id = f"{project_id}:{spec_id}"
    try:
        await agent_service.start_task_execution(
            task_id=task_id,
            project_path=project_path,
            spec_id=spec_id,
            auto_continue=request.auto_continue,
            base_branch=request.baseBranch,
            mode=request.mode or "full",
            parallel=eff_parallel,
            workers=eff_workers,
        )
        await emit_task_status(task_id, "in_progress")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to start build from trusted plan: {str(e)}",
        )

    return {
        "success": True,
        "task_id": task_id,
        "approved_by": result.approved_by,
        "approval_timestamp": result.approval_timestamp,
        "message": "Trusted plan verified — build started, spec pipeline skipped.",
    }


@router.post("/{task_id}/apply-correction")
async def apply_task_correction(
    task_id: str,
    request: ApplyCorrectionRequest,
    _access: dict = Depends(require_task_access("member")),
):
    """Apply a correction hand-back (e.g. from TFactory) to an existing spec.

    Writes ``QA_FIX_REQUEST.md`` onto the original spec and runs the QA Fixer.
    Confirm-first: ``confirm=false`` previews; ``confirm=true`` writes + runs.
    Part of the bidirectional AIFactory ↔ TFactory loop (#317).
    """
    *_, spec_dir = _resolve_task(task_id)

    result = await apply_correction(
        spec_dir,
        request.fix_request_md,
        confirm=request.confirm,
        triage=request.triage,
        manifest_hash=request.manifest_hash,
        correlation_key=request.correlation_key,
    )
    return {**result, "task_id": task_id, "source": request.source}


@router.post("/{task_id}/dispatch-to-copilot")
async def dispatch_task_to_copilot(
    task_id: str,
    request: CopilotDispatchRequest,
    _access: dict = Depends(require_task_access("member")),
):
    """Delegate a task to the GitHub Copilot cloud agent (#458).

    Assigns the linked GitHub issue to ``copilot-swe-agent[bot]`` and
    launches a background watcher that polls for the resulting PR (up to
    59 minutes — GitHub's hard session limit).

    Requires ``AIFACTORY_COPILOT_DISPATCH_ENABLED=true``.  If disabled or
    the gh CLI call fails and ``fallback_to_local=true``, the task's status
    is left unchanged so the caller can start the normal pipeline instead.

    On success the task status is set to ``copilot_running`` and the
    ``copilot_dispatch`` block is written into ``task_metadata.json``.
    """
    import asyncio
    import logging

    from ..services.copilot_dispatch_service import (
        CopilotDispatchService,
        is_dispatch_enabled,
        watch_for_copilot_pr,
    )

    logger = logging.getLogger(__name__)

    if not is_dispatch_enabled():
        if request.fallback_to_local:
            logger.info(
                "[copilot-dispatch] disabled — fallback signal for task %s", task_id
            )
            return {
                "task_id": task_id,
                "dispatched": False,
                "reason": "AIFACTORY_COPILOT_DISPATCH_ENABLED is not set",
                "fallback": True,
            }
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Copilot dispatch is not enabled on this instance",
        )

    *_, spec_dir = _resolve_task(task_id)
    task_metadata_path = spec_dir / "task_metadata.json"

    service = CopilotDispatchService()
    try:
        dispatch_meta = await asyncio.to_thread(
            service.dispatch, request.repo_full_name, request.issue_number
        )
    except RuntimeError as exc:
        logger.warning("[copilot-dispatch] dispatch failed task=%s: %s", task_id, exc)
        if request.fallback_to_local:
            return {
                "task_id": task_id,
                "dispatched": False,
                "reason": str(exc),
                "fallback": True,
            }
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    # Persist dispatch metadata.
    _patch_task_metadata(task_metadata_path, {"copilot_dispatch": dispatch_meta})

    # Emit status transition.
    await emit_task_status(task_id, "copilot_running")

    # Launch background PR watcher (fire-and-forget — 59-minute deadline).
    def _on_pr_opened(pr_number: int, pr_url: str | None) -> None:
        import asyncio as _asyncio

        loop = _asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(emit_task_status(task_id, "copilot_pr_opened"))

    asyncio.create_task(
        watch_for_copilot_pr(
            task_id=task_id,
            repo_full_name=request.repo_full_name,
            issue_number=request.issue_number,
            task_metadata_path=task_metadata_path,
            on_pr_opened=_on_pr_opened,
        )
    )

    return {
        "task_id": task_id,
        "dispatched": True,
        "agent_handle": dispatch_meta["agent_handle"],
        "dispatched_at": dispatch_meta["dispatched_at"],
        "issue_number": request.issue_number,
        "repo": request.repo_full_name,
    }


def _patch_task_metadata(path: Path, updates: dict) -> None:
    """Merge ``updates`` into ``task_metadata.json``, creating it if absent."""
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    existing.update(updates)
    path.write_text(json.dumps(existing, indent=2))
