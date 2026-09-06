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

A COMPLETED build must show its work first (#1070): see ``_build_wrote_nothing``.

Behaviour is otherwise unchanged from the inlined version; see
tests/test_terminal_completion_characterization.py.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory_common.logsafe import sanitize_log

from server.background import spawn

from .task_control import write_control


def _build_wrote_nothing(
    spec_dir: Path, spec_id: str, backend_path: Path | None, logger: Any
) -> bool:
    """True only when the build DEMONSTRABLY produced no commit (#1070).

    The evidence gate. A build is allowed to report COMPLETED — which the board
    renders as "built, awaiting a person" and which fires the TFactory handoff
    and the PR endgame — only once it can show a commit. Its own subtask
    statuses do not count: #1070 spent 898k tokens, wrote a plan document and no
    source file, exited 0, advanced to human_review and handed TFactory a branch
    identical to main, because nothing on the path ever asked the cheapest
    question there is. (Same principle as the #851 honesty gate, one stage
    earlier: no green checkbox without the thing it claims.)

    Fails OPEN. Only a MEASURED zero blocks; ``None`` means the question could
    not be answered here, and failing a build we merely could not measure would
    be worse than the bug it prevents. The memory tree is fetched first because
    on the packed path the build's commit ledger is still in object storage at
    this point (#1038) — emit_terminal_completion fetches it too, a few lines
    below, but the answer is needed BEFORE the status is decided.
    """
    try:
        if backend_path and str(backend_path) not in sys.path:
            sys.path.insert(0, str(backend_path))
        # Deferred: both live under backend_path, which is only on sys.path
        # from the line above — so mypy cannot resolve them from here either.
        # (``unused-ignore`` too: the cq-ratchet runs mypy with
        # --ignore-missing-imports, where the ignore above is redundant.)
        from core.workspace_fetch import (  # type: ignore[import-not-found,unused-ignore] # noqa: PLC0415
            maybe_fetch_memory,
        )
        from pfactory.tfactory_client import build_commit_count  # noqa: PLC0415

        maybe_fetch_memory(spec_dir, spec_id)
        return bool(build_commit_count(spec_dir, spec_id) == 0)
    except Exception:  # noqa: BLE001 — unmeasurable is not empty
        logger.debug("build-evidence check unavailable", exc_info=True)
        return False


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
) -> str:
    """Fire-once terminal-completion emission + side-effects (see module docstring).

    Returns the EFFECTIVE terminal status, which is not always the one passed in:
    the evidence gate below downgrades ``completed`` to ``failed`` when the build
    wrote nothing. Callers must record what this decided rather than what they
    asked for, or the plan says "completed" for a build this function already
    ruled failed (#1430).
    """
    # Evidence gate (#1070): a build with nothing to show is a FAILED build, not
    # a review request. Downgrading here covers both build backends at once —
    # the in-pod subprocess path and the kubejob path both finish through this
    # function — and, because it lands before the emit below, the RFC-0001 event,
    # the board status and the TFactory handoff all tell the same true story.
    if is_completed and _build_wrote_nothing(spec_dir, spec_id, backend_path, logger):
        logger.error(
            "[AgentService] %s reported completed with no commit on its branch; "
            "recording it as a failed build instead of a review request, and "
            "skipping the TFactory handoff + PR endgame (#1070)",
            sanitize_log(spec_id),
        )
        is_completed = False
        terminal_status = "failed"
        try:
            # The established needs-attention pair (#287): a failed build is
            # human_review/errors, never human_review/completed. Without this the
            # board keeps reading the agent's own "completed" out of the plan /
            # task_logs the Job pushed back.
            write_control(
                spec_dir,
                status="human_review",
                review_reason="errors",
                updated_by="evidence_gate",
            )
        except Exception:  # noqa: BLE001 — never break the completion path
            logger.debug("evidence-gate control write failed", exc_info=True)

    if is_terminal:
        _completion_marker = spec_dir / ".terminal_completion_emitted"
        if not _completion_marker.exists():
            # The marker is written AFTER a confirmed delivery, never before
            # (#1407). Writing it first made it a tombstone: the first failure
            # -- transient or not -- permanently suppressed every later attempt,
            # because the emit only runs when the marker is absent. Six of seven
            # specs carried the marker while CFactory had received two POSTs in
            # 24 hours, one of them a probe. "We tried once" was being recorded
            # as "done", and nothing retried or complained.
            try:
                from .completion import emit_terminal_completion

                project_id = (
                    task_id.split(":", 1)[0] if ":" in task_id else project_path.name
                )
                terminal_status = terminal_status
                _event = emit_terminal_completion(
                    spec_dir,
                    task_id=task_id,
                    project_id=project_id,
                    spec_id=spec_id,
                    status=terminal_status,
                )
                if _event.get("_delivered", True):
                    try:
                        _completion_marker.write_text(datetime.now(UTC).isoformat())
                    except OSError as e:
                        logger.warning(
                            "terminal-completion marker write failed for %s; the "
                            "event was delivered and will be re-sent next call: %s",
                            sanitize_log(spec_id),
                            sanitize_log(str(e)),
                        )
                else:
                    # No marker: the next terminal call retries instead of
                    # skipping. CFactory dedups by
                    # (service, correlation_key, status), so a re-send is safe.
                    logger.warning(
                        "completion event for %s was not delivered; leaving no "
                        "marker so the next terminal call retries",
                        sanitize_log(spec_id),
                    )
            except Exception:
                logger.warning(
                    "completion emit failed for %s; no marker written, will retry",
                    sanitize_log(spec_id),
                    exc_info=True,
                )

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
                _seffx_marker.write_text(datetime.now(UTC).isoformat())
            except OSError as e:
                logger.debug(
                    "terminal-side-effects marker write failed (may re-run next call): %s",
                    sanitize_log(str(e)),
                )

            # #1456: score the REAL diff against the high-risk path table and
            # raise reviewTier BEFORE the handoff below — TFactory picks its VAL
            # floor off the contract this metadata feeds, so a raise landing
            # afterwards would verify at the self-declared rigor. Advisory
            # unless AIFACTORY_PATH_RISK_FLOOR_ENFORCE is on (it then rewrites
            # reviewTier, which both TFactory and the PR endgame already read).
            # Same helper the endgame calls; idempotent, best-effort.
            try:
                from .pr_endgame import apply_path_risk_floor  # noqa: PLC0415

                _meta_file = spec_dir / "task_metadata.json"
                _meta = json.loads(_meta_file.read_text())
                apply_path_risk_floor(
                    project_path,
                    spec_dir,
                    spec_id,
                    str(_meta.get("base_branch") or _meta.get("baseBranch") or "main"),
                    _meta.get("reviewTier"),
                )
            except Exception:  # noqa: BLE001 — never blocks completion
                logger.debug("path risk floor skipped (best-effort)", exc_info=True)

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
                        "[AgentService] Auto-handed off %s to TFactory for testing",
                        sanitize_log(spec_id),
                    )
                elif handoff.get("reason") not in (
                    None,
                    "not_requested",
                    "not_configured",
                ):
                    logger.warning(
                        "[AgentService] TFactory auto-handoff for %s did not send: %s",
                        sanitize_log(spec_id),
                        sanitize_log(handoff),
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
                            spawn(_re_test())

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
                        _conflict_fixer = None
                        if _reviewer == "aifactory":
                            import subprocess as _sp

                            from .pr_data_service import get_pr_data_service
                            from .pr_endgame import ReviewState
                            from .pr_review_service import get_pr_review_service

                            _pr_box: dict = {}
                            _wt = ctx["worktree"]

                            def _on_pr_opened(prn: int) -> None:
                                _pr_box["pr"] = prn
                                spawn(
                                    get_pr_review_service().start_review(
                                        _proj_id, prn, project_path
                                    )
                                )
                                # Back-fill the PR onto the TFactory verify task so
                                # its triager posts the verdict to this PR — the
                                # handoff was sent before the PR existed (#964).
                                try:
                                    from pfactory.tfactory_client import send_pr_attach

                                    spawn(
                                        send_pr_attach(
                                            spec_dir, spec_id, prn, ctx.get("repo")
                                        )
                                    )
                                except Exception:  # noqa: BLE001 — best-effort
                                    logger.debug(
                                        "tfactory PR-attach failed (best-effort)",
                                        exc_info=True,
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

                            def _conflict_fixer(conflicted_files, wt) -> bool:
                                # #543: resolve git conflict markers in-place via
                                # the QA-fixer. resolve_pr_conflicts owns the
                                # rebase/add/continue + the orchestrator owns the
                                # push + re-review, so this only needs to make the
                                # markers disappear. Verify none remain before
                                # returning True (the fixer is general-purpose, so
                                # never blindly trust it resolved them). Runs in a
                                # worker thread (no loop), so asyncio.run is safe.
                                from pathlib import Path as _P

                                try:
                                    import asyncio as _aio

                                    from qa.correction import (
                                        _run_fixer_bg,
                                        apply_correction,
                                    )

                                    md = (
                                        "## Auto-resolve merge conflicts\n\n"
                                        "Resolve the git conflict markers "
                                        "(<<<<<<<, =======, >>>>>>>) in these "
                                        "files, keeping BOTH sides' intent where "
                                        "possible. The result must contain NO "
                                        "conflict markers:\n"
                                        + "\n".join(f"- {f}" for f in conflicted_files)
                                    )

                                    async def _fixer_to_completion(_spec):
                                        await _run_fixer_bg(_spec)
                                        return {"status": "qa_fixed", "completed": True}

                                    _aio.run(
                                        apply_correction(
                                            spec_dir,
                                            md,
                                            confirm=True,
                                            fixer_fn=_fixer_to_completion,
                                            correlation_key=f"pr-conflict-{spec_id}",
                                        )
                                    )
                                except Exception:  # noqa: BLE001
                                    logger.debug(
                                        "PR endgame conflict_fixer failed",
                                        exc_info=True,
                                    )
                                    return False
                                # Only report resolved if NO markers remain.
                                for rel in conflicted_files:
                                    try:
                                        txt = (_P(wt) / rel).read_text()
                                    except OSError:
                                        return False
                                    if (
                                        "<<<<<<<" in txt
                                        or ">>>>>>>" in txt
                                        or "\n=======\n" in txt
                                    ):
                                        return False
                                return True

                        endgame = await run_pr_endgame(
                            spec_dir=spec_dir,
                            spec_id=spec_id,
                            worktree=ctx["worktree"],
                            branch=ctx["branch"],
                            base=ctx["base"],
                            repo=ctx["repo"],
                            # RFC-0020 3.5: the tenant's declared host. The
                            # endgame refuses off GitHub rather than running
                            # `gh` against a repo that is not there.
                            provider=ctx.get("provider", "github"),
                            auto_merge=is_auto_merge_enabled(project_path),
                            # RFC-0011 tier (#1158) — read by
                            # gather_pr_context from the same
                            # task_metadata.json it reads the base from.
                            review_tier=ctx.get("review_tier"),
                            # #1456 advisory rollout: what the CHANGED PATHS
                            # say the tier should be, noted on the PR for the
                            # human even while it may not withhold the merge.
                            review_tier_floor=ctx.get("review_tier_floor"),
                            reviewer=_reviewer,
                            review_fn=_review_fn,
                            fix_fn=_fix_fn,
                            conflict_fixer=_conflict_fixer,
                            on_pr_opened=_on_pr_opened,
                            re_test=_re_test_sync,
                        )
                        logger.info(
                            f"[AgentService] PR endgame for {sanitize_log(spec_id)} "
                            f"(reviewer={sanitize_log(_reviewer)}): {sanitize_log(endgame)}"
                        )
                    else:
                        logger.info(
                            "[AgentService] PR endgame skipped for %s "
                            "(no worktree branch / repo)",
                            sanitize_log(spec_id),
                        )
            except Exception:
                logger.debug("PR endgame failed (best-effort)", exc_info=True)
    return terminal_status
