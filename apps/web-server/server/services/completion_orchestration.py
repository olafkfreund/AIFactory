"""Terminal-completion orchestration — extracted from agent_service.py (#556).

The fire-once terminal-completion side-effects, lifted out of
``AgentService._update_plan_status`` so the gating is isolated and testable.
``run_terminal_completion`` is called once per status update with decoupled
booleans (so this module needs no TaskPhase import -> no circular import with
agent_service):

  - ``is_terminal``  (COMPLETED or FAILED): write the ``.terminal_completion_emitted``
    fire-once marker and emit the RFC-0001 completion event (status=terminal_status).
  - ``is_completed`` (COMPLETED only): write the ``.terminal_side_effects_done``
    fire-once marker and run the TFactory handoff + PR endgame.

Behaviour is unchanged from the inlined version; see
tests/test_terminal_completion_characterization.py.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


async def run_terminal_completion(
    *,
    spec_dir: Path,
    project_path: Path,
    spec_id: str,
    task_id: str,
    backend_path: Path | None,
    is_terminal: bool,
    is_completed: bool,
    terminal_status: str,
    logger: Any,
) -> None:
    """Fire-once terminal-completion emission + side-effects (see module docstring)."""
    if is_terminal:
        _completion_marker = spec_dir / ".terminal_completion_emitted"
        if not _completion_marker.exists():
            try:
                _completion_marker.write_text(datetime.now(timezone.utc).isoformat())
            except OSError:
                pass
            try:
                from .completion import emit_terminal_completion

                project_id = (
                    task_id.split(":", 1)[0] if ":" in task_id else project_path.name
                )
                terminal_status = terminal_status
                emit_terminal_completion(
                    spec_dir,
                    task_id=task_id,
                    project_id=project_id,
                    spec_id=spec_id,
                    status=terminal_status,
                )
            except Exception:
                logger.debug("completion emit failed (best-effort)", exc_info=True)

    # Terminal completion side-effects: hand off to TFactory + run the PR
    # endgame. Gated on COMPLETED only — NOT on emit_events. emit_events
    # controls WS double-emission (Issue #14) and is False on the
    # _monitor_process terminal path, so gating side-effects on it meant
    # they NEVER fired on a real completion (#71). A fire-once marker
    # makes this idempotent across the multiple COMPLETED call paths
    # (lines ~1972 emit_events=True and ~2269 emit_events=False).
    if is_completed:
        _seffx_marker = spec_dir / ".terminal_side_effects_done"
        if not _seffx_marker.exists():
            try:
                _seffx_marker.write_text(datetime.now(timezone.utc).isoformat())
            except OSError:
                pass

            # Auto-handover the finished build to TFactory when the task
            # opted in (task_metadata `auto_handover_tfactory`, #496) and
            # TFactory is configured. Best-effort: never blocks completion.
            try:
                if str(backend_path) not in sys.path:
                    sys.path.insert(0, str(backend_path))
                from pfactory.tfactory_client import maybe_auto_handoff_tfactory

                handoff = await maybe_auto_handoff_tfactory(spec_dir, spec_id)
                if handoff.get("sent"):
                    logger.info(
                        f"[AgentService] Auto-handed off {spec_id} to TFactory for testing"
                    )
                elif handoff.get("reason") not in (
                    None,
                    "not_requested",
                    "not_configured",
                ):
                    logger.warning(
                        f"[AgentService] TFactory auto-handoff for {spec_id} did not send: {handoff}"
                    )
            except Exception:
                logger.debug(
                    "tfactory auto-handoff failed (best-effort)", exc_info=True
                )

            # PR endgame (#71 Phase 4): on a clean build, optionally open
            # a PR, request a Copilot review, and (only on Copilot's
            # APPROVAL) auto-merge + re-test. Toggled per-project from the
            # Settings UI (auto_pr / auto_merge in .aifactory/.env), env as
            # fallback. Both default OFF; human-stop on changes-requested,
            # no-Copilot-review, or timeout.
            try:
                from .pr_endgame import (
                    gather_pr_context,
                    is_auto_merge_enabled,
                    is_auto_pr_enabled,
                    resolve_pr_reviewer,
                    run_pr_endgame,
                    verdict_from_review_result,
                )

                if is_auto_pr_enabled(project_path):
                    ctx = gather_pr_context(project_path, spec_dir, spec_id)
                    if ctx:

                        async def _re_test() -> None:
                            from pfactory.tfactory_client import (
                                maybe_auto_handoff_tfactory,
                            )

                            await maybe_auto_handoff_tfactory(spec_dir, spec_id)

                        def _re_test_sync() -> None:
                            asyncio.create_task(_re_test())

                        # Reviewer gating (#71 Phase A). "aifactory" uses
                        # AIFactory's own review engine (Claude/Ollama, no
                        # Copilot credits): on PR-open, trigger the engine
                        # and gate the merge on its stored verdict (GitHub
                        # forbids self-approving the PR we opened).
                        _reviewer = resolve_pr_reviewer(project_path)
                        _proj_id = task_id.split(":", 1)[0] if ":" in task_id else ""
                        _review_fn = None
                        _on_pr_opened = None
                        _fix_fn = None
                        if _reviewer == "aifactory":
                            import subprocess as _sp

                            from .pr_data_service import get_pr_data_service
                            from .pr_endgame import ReviewState
                            from .pr_review_service import get_pr_review_service

                            _pr_box: dict = {}
                            _wt = ctx["worktree"]

                            def _on_pr_opened(prn: int) -> None:
                                _pr_box["pr"] = prn
                                asyncio.create_task(
                                    get_pr_review_service().start_review(
                                        _proj_id, prn, project_path
                                    )
                                )

                            def _review_fn() -> ReviewState:
                                prn = _pr_box.get("pr")
                                if not prn:
                                    return ReviewState("pending")
                                res = get_pr_data_service().get_review(
                                    project_path, prn
                                )
                                return verdict_from_review_result(res)

                            def _fix_fn(findings) -> bool:
                                # Phase B: route review findings to the QA-fixer,
                                # push the fix to the PR branch, then re-review.
                                # Runs in a worker thread (no running loop), so
                                # asyncio.run is safe. Best-effort.
                                prn = _pr_box.get("pr")
                                try:
                                    import asyncio as _aio

                                    from qa.correction import (
                                        _run_fixer_bg,
                                        apply_correction,
                                    )

                                    md = (
                                        "## Pre-merge review findings (auto-fix)\n\n"
                                        + "\n".join(
                                            f"- [{(f or {}).get('severity', 'note')}] "
                                            f"{(f or {}).get('title') or (f or {}).get('message') or f}"
                                            for f in (findings or [])
                                        )
                                    )

                                    # Run the QA-fixer TO COMPLETION (not the
                                    # default fire-and-forget background task) so
                                    # the fix actually lands BEFORE we push +
                                    # re-review — otherwise we'd re-review the
                                    # un-fixed code and waste the cycle budget.
                                    async def _fixer_to_completion(_spec):
                                        await _run_fixer_bg(_spec)
                                        return {"status": "qa_fixed", "completed": True}

                                    _aio.run(
                                        apply_correction(
                                            spec_dir,
                                            md,
                                            confirm=True,
                                            fixer_fn=_fixer_to_completion,
                                            correlation_key=f"pr-{prn}",
                                        )
                                    )
                                    _sp.run(
                                        ["gh", "auth", "setup-git"],
                                        capture_output=True,
                                        timeout=30,
                                    )
                                    push = _sp.run(
                                        ["git", "push", "origin", "HEAD"],
                                        cwd=str(_wt),
                                        capture_output=True,
                                        text=True,
                                        timeout=120,
                                    )
                                    if push.returncode != 0:
                                        logger.warning(
                                            "[pr-endgame] fix push failed: %s",
                                            (push.stderr or "")[:200],
                                        )
                                        return False
                                    # Re-review the FIXED code and wait for the
                                    # fresh result before returning, so the loop
                                    # doesn't re-read the stale (pre-fix) verdict
                                    # and burn a cycle. Bounded; best-effort.
                                    if prn:
                                        import time as _t

                                        _ds = get_pr_data_service()
                                        _before = (
                                            (
                                                _ds.get_review(project_path, prn) or {}
                                            ).get("data", {})
                                            or {}
                                        ).get("reviewedAt")
                                        _aio.run(
                                            get_pr_review_service().start_review(
                                                _proj_id, prn, project_path
                                            )
                                        )
                                        for _ in range(40):  # ~6 min cap
                                            _t.sleep(9)
                                            _now = (
                                                (
                                                    _ds.get_review(project_path, prn)
                                                    or {}
                                                ).get("data", {})
                                                or {}
                                            ).get("reviewedAt")
                                            if _now and _now != _before:
                                                break
                                    return True
                                except Exception:  # noqa: BLE001
                                    logger.debug(
                                        "PR endgame fix_fn failed", exc_info=True
                                    )
                                    return False

                        endgame = await run_pr_endgame(
                            spec_dir=spec_dir,
                            spec_id=spec_id,
                            worktree=ctx["worktree"],
                            branch=ctx["branch"],
                            base=ctx["base"],
                            repo=ctx["repo"],
                            auto_merge=is_auto_merge_enabled(project_path),
                            reviewer=_reviewer,
                            review_fn=_review_fn,
                            fix_fn=_fix_fn,
                            on_pr_opened=_on_pr_opened,
                            re_test=_re_test_sync,
                        )
                        logger.info(
                            f"[AgentService] PR endgame for {spec_id} "
                            f"(reviewer={_reviewer}): {endgame}"
                        )
                    else:
                        logger.info(
                            "[AgentService] PR endgame skipped for %s "
                            "(no worktree branch / repo)",
                            spec_id,
                        )
            except Exception:
                logger.debug("PR endgame failed (best-effort)", exc_info=True)
