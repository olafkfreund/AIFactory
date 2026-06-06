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
from task_logger import LogPhase

from .memory_manager import get_graphiti_context, save_session_memory
from .parallel_runner import PhaseRunResult, SubtaskResult, run_parallel_phase
from .session import run_agent_session
from .utils import sync_plan_to_source
from .wave_log import WaveRecorder

logger = logging.getLogger(__name__)


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
        logger.debug("[parallel] insight extraction skipped for %s: %s", subtask.id, exc)
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
                logger.debug("[parallel] memory context skipped for %s: %s", subtask.id, exc)

            commit_before = await asyncio.to_thread(_head_commit, child_path)

            # --- agent session (the genuinely concurrent, awaited part) ---
            sub_model = getattr(subtask, "model", None) or phase_model
            client = _make_client(child_path, child_spec_dir, sub_model)
            async with client:
                status, _resp, _err = await run_agent_session(
                    client, prompt, child_spec_dir, verbose, phase=LogPhase.CODING
                )

            # --- commit the subtask's work in its own worktree ---
            await asyncio.to_thread(
                wt_mgr.commit_in_worktree,
                child_name,
                f"aifactory: {subtask.id} (parallel wave)",
            )

            session_ok = status != "error"

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
                    error="agent session error",
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

    async def merge_subtask(subtask: Any, result: SubtaskResult) -> bool:
        if not result.worktree_name:
            return False
        try:
            return await asyncio.to_thread(
                wt_mgr.merge_worktree, result.worktree_name, True, False
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[parallel] merge %s failed: %s", subtask.id, exc)
            return False

    async def mark_complete(subtask: Any) -> None:
        # Parent-owned, serialized canonical plan write (concurrency invariant #3).
        async with plan_lock:
            try:
                canonical = ImplementationPlan.load(plan_path)
                for ph in canonical.phases:
                    for st in ph.subtasks:
                        if st.id == subtask.id:
                            st.complete()
                canonical.save(plan_path)
                sync_plan_to_source(spec_dir, source_spec_dir)
            except Exception as exc:  # noqa: BLE001
                logger.error("[parallel] mark_complete %s failed: %s", subtask.id, exc)

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
