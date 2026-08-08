"""
Parallel Phase Integration (#376)
=================================

Wires the provider-agnostic wave orchestrator
(:func:`agents.parallel_runner.run_parallel_phase`) to the real AIFactory
primitives — child git worktrees, the SDK/provider clients, agent sessions, and
the canonical implementation plan.

The orchestrator owns the *control flow* (which subtasks run together, in what
order, when to merge); this module owns the *side effects* of one subtask:

    run_subtask   -> create an isolated child worktree, run the agent session
                     there, commit. Concurrent & isolated (no shared writes).
    merge_subtask -> merge the child branch back into the task branch.
                     Called sequentially by the orchestrator.
    mark_complete -> record completion in the canonical plan.
                     Called sequentially by the orchestrator.

Everything here is opt-in (only reached when ``--parallel`` is set and a phase
is ``parallel_safe`` with >=2 pending subtasks) and defensive: any failure marks
that subtask failed/unmerged so the caller falls back to serial execution for it
— the default serial path is never affected.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from core.client import create_client
from implementation_plan.plan import ImplementationPlan
from phase_config import (
    get_phase_model,
    get_phase_thinking_budget,
    get_provider_extra_kwargs,
    infer_provider_from_model,
)
from prompt_generator import (
    format_context_for_prompt,
    generate_subtask_prompt,
    load_subtask_context,
)
from providers.factory import get_provider
from task_logger import LogPhase, TaskLogger

from .completion_gate import completion_refusal
from .memory_manager import get_graphiti_context, save_session_memory
from .parallel_runner import PhaseRunResult, SubtaskResult, run_parallel_phase
from .session import run_session_guarded
from .test_evidence import read_test_evidence, record_subtask_completed
from .token_attribution import PromptSegments, TurnUsage, record_turn
from .utils import (
    record_subtask_completion,
    record_subtask_started,
    sync_plan_to_source,
)
from .wave_log import WaveRecorder

logger = logging.getLogger(__name__)


async def gated_mark_complete(
    subtask: Any,
    *,
    plan_path: Path,
    source_spec_dir: Path | None,
    project_dir: Path,
    evidence: dict[str, Any],
) -> bool:
    """Record a wave subtask's completion — through the gates the serial coder
    passes (#1177). Returns False when a gate refused it.

    Module-level rather than a closure so the wave completion path is directly
    testable: this is the function that decides whether a wave child is allowed
    to be called "completed".

    ``project_dir`` is the task worktree *after* this child's merge-back, so the
    gates judge the tree that ships. ``evidence`` was read from the child's own
    worktree spec dir before the merge deleted it (see ``run_subtask``).
    """
    subtask_dict = subtask.to_dict() if hasattr(subtask, "to_dict") else dict(subtask)
    try:
        refusal = completion_refusal(subtask_dict, project_dir, evidence)
    except Exception as exc:  # noqa: BLE001 - a gate that errored has not passed
        # Fails closed, exactly as the serial path does (an exception there
        # leaves the plan unwritten): a control that could not measure must not
        # read as clean. The subtask is redone serially, where the same gate runs.
        logger.error("[parallel] completion gate ERRORED for %s: %s", subtask.id, exc)
        return False
    if refusal:
        logger.error("[parallel] completion REFUSED for %s: %s", subtask.id, refusal)
        return False
    # Close this subtask's evidence window, exactly as the serial funnel does
    # (#1187) — and in the same ledger, since ``plan_path`` is
    # ``spec_dir/implementation_plan.json`` and ``spec_dir`` is what both the
    # serial gate and this engine's own fallback read. Writing to the child's
    # spec dir instead would be lost: the merge deletes that worktree.
    record_subtask_completed(plan_path.parent, subtask.id)
    if not record_subtask_completion(subtask.id, plan_path, source_spec_dir):
        logger.error(
            "[parallel] mark_complete %s: completion not recorded "
            "(plan not found at %s or source %s)",
            subtask.id,
            plan_path,
            source_spec_dir,
        )
    return True


# Cache for the lazily-loaded web-server completion module (#45 P1). The backend
# runs as a separate subprocess and must NOT import the web-server package (see
# agents/inbox.py); so the live per-worker sub-event reuses the SAME emitter the
# terminal event uses by loading that single stdlib-only module BY FILE PATH —
# its only web-server imports are relative and guarded inside functions, so the
# module loads standalone. Sentinel ``False`` = a load attempt already failed
# (never retry); ``None`` = not yet attempted.
_completion_mod: Any = None


def _load_completion_emitter() -> Any:
    """Best-effort handle to the web-server completion emitter (#45 P1).

    Returns the loaded module, or ``None`` if it cannot be located/loaded (in
    which case live sub-event emission silently no-ops — additive + best-effort,
    exactly the terminal event's contract). Never raises.
    """
    global _completion_mod
    if _completion_mod is not None:
        return _completion_mod or None
    try:
        import importlib.util

        # This module file lives at apps/backend/agents/parallel_integration.py;
        # the emitter is its sibling app apps/web-server/server/services/.
        comp_path = (
            Path(__file__).resolve().parents[2]
            / "web-server"
            / "server"
            / "services"
            / "completion.py"
        )
        if not comp_path.exists():
            _completion_mod = False
            return None
        spec = importlib.util.spec_from_file_location(
            "aifactory_completion_emitter", comp_path
        )
        if spec is None or spec.loader is None:
            _completion_mod = False
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _completion_mod = mod
        return mod
    except Exception:  # noqa: BLE001 — emitter is optional; never break a build
        _completion_mod = False
        return None


def emit_worker_live_event(
    *,
    spec_dir: Path,
    project_dir: Path,
    worker_id: str,
    subtask_id: str | None,
    provider: str | None,
    model: str | None,
    agent_phase: str | None,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    duration_ms: int,
) -> None:
    """Emit ONE live per-worker sub-event as this worker finishes (#45 P1).

    Reuses the terminal event's emitter + transport via the lazily-loaded
    completion module. Fully best-effort: any failure (module missing, transport
    down, slow webhook) is swallowed so it can NEVER fail or slow a build —
    identical contract to the existing terminal completion event. The terminal
    event at task end is unchanged (still fires, now with v1.3 rollups).

    ``spec_id`` / ``task_id`` / ``project_id`` are reconstructed the same way the
    web-server's terminal emit derives them (``spec_id = spec_dir.name``,
    ``project_id = project_dir.name``, ``task_id = project_id:spec_id``) so the
    live sub-event shares the terminal event's correlation key.
    """
    try:
        mod = _load_completion_emitter()
        if mod is None:
            return
        spec_id = spec_dir.name
        project_id = project_dir.name
        worker_rec = {
            "worker_id": worker_id,
            "subtask_id": subtask_id,
            "provider": provider,
            "model": model,
            "agent_phase": agent_phase,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "total_tokens": int(input_tokens) + int(output_tokens),
            "cost_usd": float(cost_usd or 0.0),
            "duration_ms": int(duration_ms),
        }
        mod.emit_worker_completion(
            spec_dir=spec_dir,
            task_id=f"{project_id}:{spec_id}",
            spec_id=spec_id,
            project_id=project_id,
            worker=worker_rec,
        )
    except Exception:  # noqa: BLE001 — a live sub-event must never break a build
        logger.debug("[parallel] worker live event skipped", exc_info=True)


def emit_worker_progress_heartbeat(
    *,
    spec_dir: Path,
    project_dir: Path,
    worker_id: str,
    subtask_id: str | None,
    provider: str | None,
    model: str | None,
    agent_phase: str | None,
    total_tokens: int,
    cost_usd: float,
    elapsed_ms: int,
) -> None:
    """Emit ONE throttled per-worker PROGRESS heartbeat for a RUNNING worker
    (#45 Tier 2).

    Reuses the SAME completion emitter + transport as the terminal/worker_done
    events via the lazily-loaded completion module (loaded by file path across
    the subprocess/web-server import boundary). Fully best-effort and bounded:
    the emitter throttles to at most one heartbeat per worker_id per ~10s window,
    and any failure (module missing, transport down/slow) is swallowed here too —
    so a heartbeat can NEVER fail, slow, or block the build. The one-shot
    ``worker_done`` event still fires unchanged when the worker finishes.

    ``spec_id`` / ``task_id`` / ``project_id`` are reconstructed exactly as the
    terminal/worker emits derive them, so the heartbeat shares the same
    correlation key.
    """
    try:
        mod = _load_completion_emitter()
        if mod is None or not hasattr(mod, "emit_worker_progress"):
            return
        spec_id = spec_dir.name
        project_id = project_dir.name
        worker_rec = {
            "worker_id": worker_id,
            "subtask_id": subtask_id,
            "provider": provider,
            "model": model,
            "agent_phase": agent_phase,
            "total_tokens": int(total_tokens),
            "cost_usd": float(cost_usd or 0.0),
            "elapsed_ms": int(elapsed_ms),
        }
        mod.emit_worker_progress(
            spec_dir=spec_dir,
            task_id=f"{project_id}:{spec_id}",
            spec_id=spec_id,
            project_id=project_id,
            worker=worker_rec,
        )
    except Exception:  # noqa: BLE001 — a progress heartbeat must never break a build
        logger.debug("[parallel] worker progress heartbeat skipped", exc_info=True)


def _task_branch(project_dir: Path) -> str:
    """Return the current branch of the task worktree (merge target)."""
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip() or "HEAD"


def _head_commit(path: Path) -> str | None:
    """Return the HEAD commit hash at a worktree path (None if unavailable)."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip() or None if result.returncode == 0 else None


async def _capture_memory(
    *,
    subtask: Any,
    child_spec_dir: Path,
    child_path: Path,
    commit_before: str | None,
    success: bool,
    recovery_manager: Any,
    memory_lock: asyncio.Lock,
    canonical_spec_dir: Path,
    canonical_project_dir: Path,
) -> None:
    """Extract session insights (concurrent) and persist them (serialized).

    Insight extraction is an LLM/read operation and runs concurrently across the
    wave; only ``save_session_memory`` (the embedded-graph write) is serialized
    under ``memory_lock`` so concurrent subtasks never write the DB at once.
    Best-effort: a memory failure never fails the subtask.
    """
    try:
        from analysis.insight_extractor import extract_session_insights

        commit_after = await asyncio.to_thread(_head_commit, child_path)
        insights = await extract_session_insights(
            child_spec_dir,
            child_path,
            subtask.id,
            0,
            commit_before,
            commit_after,
            success,
            recovery_manager,
        )
    except Exception as exc:  # noqa: BLE001 - memory is best-effort
        logger.debug(
            "[parallel] insight extraction skipped for %s: %s", subtask.id, exc
        )
        insights = None

    try:
        async with memory_lock:
            await save_session_memory(
                canonical_spec_dir,
                canonical_project_dir,
                subtask.id,
                0,
                success,
                [subtask.id] if success else [],
                discoveries=insights,
            )
    except Exception as exc:  # noqa: BLE001 - memory is best-effort
        logger.debug("[parallel] memory save skipped for %s: %s", subtask.id, exc)


def _child_spec_name(spec_name: str, subtask_id: str) -> str:
    """Build a filesystem/branch-safe child worktree name for a subtask."""
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", subtask_id).strip("-")
    return f"{spec_name}__w__{safe_id}"


def reset_subtask_status_pending(plan_path: Path, subtask_id: str) -> bool:
    """Set a subtask's status to PENDING in the canonical plan file.

    Used when a parallel merge-back fails: the coder already marked the subtask
    completed in its child worktree, but its files never landed on the task
    branch. Resetting to PENDING makes the serial loop re-implement it (so the
    files land) instead of skipping it as 'completed' and losing the work.

    Returns True if the subtask was found and reset.
    """
    from implementation_plan.enums import SubtaskStatus

    try:
        canonical = ImplementationPlan.load(plan_path)
    except Exception:  # noqa: BLE001 - missing/corrupt plan: nothing to reset
        return False
    found = False
    for ph in canonical.phases:
        for st in ph.subtasks:
            if st.id == subtask_id:
                st.status = SubtaskStatus.PENDING
                found = True
    if found:
        canonical.save(plan_path)
    return found


async def run_parallel_coding_phase(
    *,
    plan: ImplementationPlan,
    phase: Any,
    project_dir: Path,
    spec_dir: Path,
    model: str,
    workers: int,
    verbose: bool = False,
    source_spec_dir: Path | None = None,
    remote_control_session: str | None = None,
    recovery_manager: Any = None,
) -> PhaseRunResult:
    """Run one ``parallel_safe`` phase's subtasks concurrently in waves.

    Builds the real side-effecting hooks and delegates sequencing to
    :func:`agents.parallel_runner.run_parallel_phase`. Returns its
    :class:`PhaseRunResult`; on stall/partial failure the caller resumes the
    remaining subtasks serially.

    Graphiti memory (#376): each subtask reads memory context concurrently
    (pre-injected into its prompt) and extracts insights concurrently, but the
    actual knowledge-graph WRITE is funnelled through ``memory_lock`` so the
    embedded LadybugDB never sees concurrent writers.
    """
    from core.worktree import WorktreeManager  # local import: heavy git module

    plan_path = spec_dir / "implementation_plan.json"
    base_branch = _task_branch(project_dir)
    phase_model = get_phase_model(spec_dir, "coding", model)
    thinking_budget = get_phase_thinking_budget(spec_dir, "coding")
    provider_name = infer_provider_from_model(phase_model)

    # One manager for the phase; `git worktree add` is serialized via this lock
    # so concurrent subtasks don't race the repo during the (fast) setup step.
    wt_mgr = WorktreeManager(project_dir, base_branch=base_branch)
    create_lock = asyncio.Lock()
    plan_lock = asyncio.Lock()
    memory_lock = asyncio.Lock()

    # #851 evidence per wave child, read while its worktree still exists: the
    # PostToolUse hook records test runs into the CHILD spec dir, and a
    # successful merge deletes that worktree before mark_complete runs. Written
    # once per subtask id from its own coroutine, read in the sequential
    # post-wave step, so there is no shared-key race.
    child_evidence: dict[str, dict[str, Any]] = {}

    def _make_client(child_path: Path, child_spec_dir: Path, sub_model: str):
        # Per-subtask model override (#376 right-sizing): a subtask may declare
        # its own model (e.g. "haiku" for scaffolding); fall back to the phase
        # model. The provider is re-inferred so an override can also switch
        # providers (e.g. a Gemini subtask inside a Claude build).
        sub_provider = infer_provider_from_model(sub_model)
        if sub_provider == "claude":
            return create_client(
                child_path,
                child_spec_dir,
                sub_model,
                agent_type="coder",
                max_thinking_tokens=thinking_budget,
                remote_control_session=remote_control_session,
            )
        provider_kwargs = {
            "model": sub_model,
            "working_dir": child_path,
            **get_provider_extra_kwargs(sub_provider, sub_model),
        }
        return get_provider(sub_provider, phase="coding", **provider_kwargs)

    async def run_subtask(subtask: Any, *, index: int) -> SubtaskResult:
        child_name = _child_spec_name(spec_dir.name, subtask.id)
        try:
            # --- isolated worktree creation (serialized, fast) ---
            async with create_lock:
                info = await asyncio.to_thread(wt_mgr.create_worktree, child_name)
            child_path = info.path
            child_spec_dir = child_path / ".aifactory" / "specs" / spec_dir.name
            if not child_spec_dir.exists():
                child_spec_dir = spec_dir  # fallback: shared spec (read-only use)

            # Plan-driven allowlist: grant the plan's declared verification
            # commands into THIS child worktree's security profile (its cwd ==
            # the file the Bash hook reads). Without it, a from-scratch parallel
            # coder is blocked running uv/pytest/ruff/mypy. Idempotent; sanitised.
            try:
                from project.analyzer import seed_profile_with_plan_commands

                _child_plan = child_spec_dir / "implementation_plan.json"
                if _child_plan.exists():
                    await asyncio.to_thread(
                        seed_profile_with_plan_commands, child_path, _child_plan
                    )
            except Exception:
                pass  # never let allowlist seeding break a parallel subtask

            # --- scoped prompt for just this subtask ---
            subtask_dict = subtask.to_dict()
            phase_dict = phase.to_dict() if hasattr(phase, "to_dict") else {}
            prompt = generate_subtask_prompt(
                spec_dir=child_spec_dir,
                project_dir=child_path,
                subtask=subtask_dict,
                phase=phase_dict,
                attempt_count=0,
                recovery_hints=None,
            )
            context = load_subtask_context(child_spec_dir, child_path, subtask_dict)
            if context.get("patterns") or context.get("files_to_modify"):
                prompt += "\n\n" + format_context_for_prompt(context)

            # Parallel isolation directive — this subtask runs CONCURRENTLY with
            # siblings in its own worktree and is merged back. Editing shared
            # files (pyproject.toml, app/main.py, package-level __init__.py, lock
            # files) causes merge-back conflicts that force a slow serial redo.
            # Keep each subtask to its own declared files so waves merge cleanly.
            _own = (subtask_dict.get("files_to_create") or []) + (
                subtask_dict.get("files_to_modify") or []
            )
            prompt += (
                "\n\n## PARALLEL ISOLATION (important)\n"
                "You are implementing ONE subtask concurrently with sibling subtasks, "
                "each in an isolated worktree that is merged back. To avoid merge "
                "conflicts that slow the whole build down:\n"
                "- ONLY create/edit the files for THIS subtask"
                + (f": {', '.join(_own)}.\n" if _own else ".\n")
                + "- Do NOT edit shared files (pyproject.toml / requirements, "
                "app/main.py or other app entrypoints, package __init__.py exports, "
                "or lock files). Dependency additions and app wiring are handled by "
                "the scaffold and the final integration subtask — not here.\n"
                "- If your module needs a dependency, assume it is already declared; "
                "do not add it to pyproject.toml yourself."
            )

            # --- memory READ: pre-inject knowledge-graph context (concurrent) ---
            try:
                mem_context = await get_graphiti_context(
                    child_spec_dir, child_path, subtask_dict
                )
                if mem_context:
                    from security import wrap_untrusted

                    prompt += "\n\n" + wrap_untrusted(
                        mem_context, source="knowledge-graph memory of past sessions"
                    )
            except Exception as exc:  # noqa: BLE001 - memory is best-effort
                logger.debug(
                    "[parallel] memory context skipped for %s: %s", subtask.id, exc
                )

            commit_before = await asyncio.to_thread(_head_commit, child_path)

            # --- agent session (the genuinely concurrent, awaited part) ---
            sub_model = getattr(subtask, "model", None) or phase_model
            client = _make_client(child_path, child_spec_dir, sub_model)
            _t_start = time.monotonic()
            # Watchdog on the client enter (#816): a stalled MCP connect becomes a
            # session_stall error (session_ok False) instead of a silent hang.
            status, _resp, _err = await run_session_guarded(
                client, prompt, child_spec_dir, verbose, phase=LogPhase.CODING
            )
            _t_end = time.monotonic()

            session_ok = status != "error"

            # Capture this child's own test-run evidence NOW: the merge deletes
            # its worktree, and with it the evidence the #851 gate reads (#1177).
            # Scoped by subtask id (#1187) because ``child_spec_dir`` is often
            # the SHARED spec dir — ``.aifactory`` is gitignored, so a fresh
            # child worktree usually has none and the fallback above kicks in.
            child_evidence[subtask.id] = read_test_evidence(child_spec_dir, subtask.id)

            # --- per-worker token attribution (#45 P1, additive) ---
            # Each parallel subtask IS a worker. Fold its real SDK usage into the
            # canonical token_usage.json keyed by the subtask id, tagged with the
            # subtask's provider/model — so a heterogeneous parallel run is
            # observable per worker / per provider. Best-effort: bookkeeping must
            # never fail a build (mirrors the serial coder path).
            try:
                _usage_payload = (_err or {}).get("usage") if _err else None
                if _usage_payload:
                    _turn_usage = TurnUsage.from_sdk_usage(
                        {
                            "input_tokens": _usage_payload.get("input_tokens", 0),
                            "output_tokens": _usage_payload.get("output_tokens", 0),
                            "cache_read_input_tokens": _usage_payload.get(
                                "cache_read_tokens", 0
                            ),
                            "cache_creation_input_tokens": _usage_payload.get(
                                "cache_creation_tokens", 0
                            ),
                        },
                        cost_usd=_usage_payload.get("cost_usd", 0.0),
                    )
                    _segments = PromptSegments(
                        user_prompt=prompt,
                        tool_output_chars=_usage_payload.get("tool_output_chars", 0),
                    )
                    _sub_provider = (
                        infer_provider_from_model(sub_model) if sub_model else None
                    )
                    _duration_ms = int((_t_end - _t_start) * 1000)
                    record_turn(
                        spec_dir,
                        _segments,
                        _turn_usage,
                        model=sub_model,
                        worker_id=subtask.id,
                        subtask_id=subtask.id,
                        provider=_sub_provider,
                        phase=LogPhase.CODING.value,
                        duration_ms=_duration_ms,
                    )
                    # Live per-worker sub-event (#45 P1): this worker has just
                    # finished its wave session and its record is persisted —
                    # emit the live drill-down event now, reusing the terminal
                    # event's best-effort emitter/transport. Best-effort: a
                    # failed/slow emit never fails or slows the build.
                    emit_worker_live_event(
                        spec_dir=spec_dir,
                        project_dir=project_dir,
                        worker_id=subtask.id,
                        subtask_id=subtask.id,
                        provider=_sub_provider,
                        model=sub_model,
                        agent_phase=LogPhase.CODING.value,
                        input_tokens=_turn_usage.total_input_tokens,
                        output_tokens=_turn_usage.output_tokens,
                        cost_usd=_turn_usage.cost_usd,
                        duration_ms=_duration_ms,
                    )
            except Exception as _exc:  # noqa: BLE001 - bookkeeping must not crash a build
                logger.debug(
                    "[parallel] token attribution skipped for %s: %s",
                    subtask.id,
                    _exc,
                )

            # Mirror session errors to the canonical spec_dir task log.
            # Child-worktree logs (child_spec_dir) are never synced back, so
            # the portal sees zero entries when a non-Claude provider fails.
            # TaskLogger.log() is synchronous — safe to call from concurrent
            # asyncio tasks (no interleaving at await points).
            if not session_ok:
                _err_msg = (_err or {}).get("message") or "provider session failed"
                TaskLogger(spec_dir, emit_markers=False).log_error(
                    f"[parallel] subtask {subtask.id}: {_err_msg}",
                    LogPhase.CODING,
                )

            # --- commit the subtask's work in its own worktree ---
            await asyncio.to_thread(
                wt_mgr.commit_in_worktree,
                child_name,
                f"aifactory: {subtask.id} (parallel wave)",
            )

            # --- memory WRITE: extract insights (concurrent) then save (serialized) ---
            await _capture_memory(
                subtask=subtask,
                child_spec_dir=child_spec_dir,
                child_path=child_path,
                commit_before=commit_before,
                success=session_ok,
                recovery_manager=recovery_manager,
                memory_lock=memory_lock,
                canonical_spec_dir=spec_dir,
                canonical_project_dir=project_dir,
            )

            if not session_ok:
                return SubtaskResult(
                    subtask_id=subtask.id,
                    success=False,
                    worktree_name=child_name,
                    error=(_err or {}).get("message") or "agent session error",
                )
            return SubtaskResult(
                subtask_id=subtask.id, success=True, worktree_name=child_name
            )
        except Exception as exc:  # noqa: BLE001 - never crash the build; fall back to serial
            logger.error("[parallel] run_subtask %s failed: %s", subtask.id, exc)
            return SubtaskResult(
                subtask_id=subtask.id,
                success=False,
                worktree_name=child_name,
                error=str(exc),
            )

    async def _reset_to_pending(subtask_id: str) -> None:
        """Reset a merge-failed subtask to PENDING in the canonical plan.

        A merge-back conflict orphans the subtask's work: the coder marked it
        completed inside its child worktree, but its files never landed on the
        task branch. If we leave it 'completed', the serial fallback skips it and
        the files are lost (the build then fails at integration). Reset it to
        PENDING so the serial loop re-implements it on the merged state —
        correctness over speed for the conflicting subtask. (#376 follow-up.)
        """
        async with plan_lock:
            try:
                if reset_subtask_status_pending(plan_path, subtask_id):
                    sync_plan_to_source(spec_dir, source_spec_dir)
                    logger.info(
                        "[parallel] reset %s to pending (merge failed → serial redo)",
                        subtask_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[parallel] reset %s to pending failed: %s", subtask_id, exc
                )

    async def merge_subtask(subtask: Any, result: SubtaskResult) -> bool:
        if not result.worktree_name:
            await _reset_to_pending(subtask.id)
            return False
        try:
            ok = await asyncio.to_thread(
                wt_mgr.merge_worktree, result.worktree_name, True, False
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[parallel] merge %s failed: %s", subtask.id, exc)
            ok = False
        if not ok:
            # Merge conflict / failure → the subtask's work is on its child
            # branch but not the task branch. Reset so serial redoes it.
            await _reset_to_pending(subtask.id)
        return ok

    async def mark_complete(subtask: Any) -> bool:
        # Parent-owned, serialized canonical plan write (concurrency invariant #3).
        # record_subtask_completion falls back to the canonical source plan when
        # the worktree spec dir has no implementation_plan.json — otherwise the
        # update is silently lost and the successful build is reported failed.
        #
        # The gate runs INSIDE the same serialized step, on the post-merge tree,
        # so it reads a tree no sibling is mutating (invariants #2 and #3).
        try:
            async with plan_lock:
                ok = await gated_mark_complete(
                    subtask,
                    plan_path=plan_path,
                    source_spec_dir=source_spec_dir,
                    project_dir=project_dir,
                    evidence=child_evidence.get(subtask.id)
                    or read_test_evidence(spec_dir, subtask.id),
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("[parallel] mark_complete %s failed: %s", subtask.id, exc)
            return True  # bookkeeping error, not a gate refusal — see #1106
        if not ok:
            # Refused: the work is merged but unproven. Reset to PENDING so the
            # serial loop redoes it — there the coder receives the refusal text
            # from update_subtask_status and can fix it or honestly fail it.
            TaskLogger(spec_dir, emit_markers=False).log_error(
                f"[parallel] subtask {subtask.id}: completion refused by the "
                "honesty gate; queued for a serial redo",
                LogPhase.CODING,
            )
            await _reset_to_pending(subtask.id)
        return ok

    # Persist wave lifecycle to parallel_report.json + build-progress.txt so the
    # wave path is verifiable after the fact, not just on the live console (#393).
    phase_name = getattr(phase, "name", "") or getattr(phase, "id", "")
    recorder = WaveRecorder(
        spec_dir,
        workers_max=workers,
        phase_name=str(phase_name),
        source_spec_dir=source_spec_dir,
    )

    def _on_wave(wave_num: int, wave: list[Any]) -> None:
        ids = [s.id for s in wave]
        print(
            f"\n[parallel] Wave {wave_num}: running {len(wave)} subtask(s) "
            f"— {', '.join(ids)}"
        )
        recorder.record_wave(wave_num, ids)
        # #1195: the wave path's start stamp. This hook is the parent-owned,
        # single-threaded moment a wave begins (it runs before the gather and
        # after the previous wave's completions landed), which is exactly what
        # ``started_at`` means and the only place it can be written without
        # racing the concurrent children.
        record_subtask_started(ids, plan_path, source_spec_dir)

    result = await run_parallel_phase(
        phase.subtasks,
        workers=workers,
        run_subtask=run_subtask,
        merge_subtask=merge_subtask,
        mark_complete=mark_complete,
        on_wave=_on_wave,
    )
    recorder.finish(result)
    return result
