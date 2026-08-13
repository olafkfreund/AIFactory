"""Kubejob build backend — KubejobMixin for AgentService (#703 spike).

The RFC-0016 kubejob-backend methods, lifted out of the AgentService god-class
into a mixin so the concern lives in its own file. AgentService inherits this
mixin; the methods still run as bound methods on the AgentService instance (they
call sibling methods/attrs like self._emit_log / self._store via the MRO).

This is the #703 spike to measure the mypy cost of the mixin approach.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from factory_common.logsafe import sanitize_log

from server.routes.projects import resolve_project_path
from server.services import review_redrive_service

from .build_log_stream import PlanSync
from .task_log_writer import TaskLogWriter
from .task_phase import TaskPhase

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger(__name__)


class KubejobMixin:
    """Kubejob build-backend methods (RFC-0016). Mixed into AgentService."""

    if TYPE_CHECKING:
        # Attributes/methods provided by the concrete host (AgentService);
        # declared here so mypy can resolve the self.* references in a mixin.
        _kubejob_log_streamers: dict[str, Any]
        _task_current_phases: dict[str, Any]
        _task_log_writers: dict[str, Any]
        _handle_output_line: Callable[..., Any]
        backend_path: Path
        _store_enabled: bool
        _task_profiles: dict[str, Any]
        _drain_queue: Callable[..., Any]
        _emit_log: Callable[..., Any]
        _release_task_credential: Callable[..., Any]
        _resolve_claude_token_pooled: Callable[..., Any]
        _safe_emit_task_status: Callable[..., Any]
        _spawn_task_execution: Callable[..., Any]
        _store: Callable[..., Any]
        _write_skill_context: Callable[..., Any]

    def _kubejob_backend_enabled(self) -> bool:
        """True when builds run as a k8s Job (RFC-0016 #671 control/exec split).

        Env-gated, default OFF (``AIFACTORY_BUILD_BACKEND=subprocess``). The
        kubejob backend requires the durable store (it reconciles by polling
        Postgres + reaps via worker_ref), so when it is requested WITHOUT a
        DATABASE_URL we log loudly and fall back to the in-pod subprocess rather
        than silently stranding builds with no reconcile loop.
        """
        from .build_backend import kubejob_enabled

        if not kubejob_enabled():
            return False
        if not self._store_enabled:
            _log.warning(
                "[AgentService] AIFACTORY_BUILD_BACKEND=kubejob requires the "
                "durable job-state store (DATABASE_URL) for reconcile/reap — "
                "it is unset; falling back to the in-pod subprocess backend."
            )
            return False
        return True

    def _build_backend(self) -> Any:
        """Lazily build + cache the k8s-Job build backend (RFC-0016 #671)."""
        if getattr(self, "_kubejob_build_backend", None) is None:
            from .build_backend import KubeJobBuildBackend

            self._kubejob_build_backend = KubeJobBuildBackend(
                self._store(), on_done=self._on_kubejob_build_done
            )
        return self._kubejob_build_backend

    async def _on_kubejob_build_done(self, job_id: str) -> None:
        """A kubejob build reached ``done`` (#852): finish it like a real build.

        The reaper's ``_done`` only writes the job-state row. Everything the
        in-pod path does on completion is missing here, so without this a green
        packed build (a) leaks its pooled Claude credential, (b) never drains the
        FIFO queue (a queued build waits for an unrelated event), and (c) never
        emits the completion event or fires the TFactory handoff — leaving
        CFactory blind and the independent verifier un-run (Factory#253 "output
        propagation"). ``job_id`` is ``<project_id>:<spec_id>`` (== task_id).
        Best-effort throughout: a build already marked ``done`` must never be
        undone by a bookkeeping failure here.
        """
        self._release_task_credential(job_id)
        try:
            await self._emit_kubejob_terminal_completion(job_id)
        except Exception:  # noqa: BLE001
            _log.exception(
                "[AgentService] kubejob completion emit failed for %s (ignored)",
                job_id,
            )
        await self._drain_queue()

    async def _emit_kubejob_terminal_completion(self, job_id: str) -> None:
        """Emit the RFC-0001 completion event + side-effects for a done kubejob.

        Reuses the in-pod completion path (``run_terminal_completion``) so the
        kubejob and subprocess backends finish identically (RFC-0016 parity):
        it fetches the plan/usage/task_logs the Job pushed to object storage
        (#852 — the control plane's own copies are the pre-dispatch originals),
        emits the completion event, and — when the task opted in
        (``auto_handover_tfactory``) — hands off to TFactory. ``is_completed`` is
        True: a Job that exited 0 with a pushed branch IS a finished autonomous
        build (RFC-0008). The board still surfaces it for human review via the
        durable ``done`` overlay; the PR endgame stays off unless enabled.
        """
        from ..routes.projects import resolve_project_path
        from .completion_orchestration import run_terminal_completion

        project_id, _, spec_id = job_id.partition(":")
        if not spec_id:
            return
        project_path = resolve_project_path(project_id)
        spec_dir = project_path / ".aifactory" / "specs" / spec_id
        try:
            backend_path: Path | None = self.backend_path
        except Exception:  # noqa: BLE001
            backend_path = None
        await run_terminal_completion(
            spec_dir=spec_dir,
            project_path=project_path,
            spec_id=spec_id,
            task_id=job_id,
            backend_path=backend_path,
            is_terminal=True,
            is_completed=True,
            terminal_status="completed",
            logger=_log,
        )

    async def _dispatch_build_job(
        self,
        *,
        task_id: str,
        project_path: Path,
        spec_id: str,
        correlation_key: str | None,
        stop_after_planning: bool = False,
        parallel: bool | None = None,
        workers: int | None = None,
        force: bool = False,
        base_branch: str | None = None,
        mode: str | None = None,
    ) -> None:
        """Dispatch a k8s Job that runs run.py for this build (RFC-0016 #671).

        The durable slot is already reserved (the row is ``running`` with
        ``worker_ref={kind:subprocess}``); the backend overwrites worker_ref
        with the k8s-job reference so the reconcile-by-poll + reaper loops can
        find the Job. The Job flips the row to its terminal state when it
        finishes — we do not block on it. Surfaces the coding status so the
        cockpit shows the build moving even though no in-pod process exists.

        #671 OAuth-env defect: a dispatched Job is a fresh pod that inherits none
        of the control-plane env, so we resolve the build's credential from the
        SAME token pool the in-pod path uses (#670) — concurrent Jobs draw
        DISTINCT tokens — and hand it to the backend, which injects it (plus the
        provider/runtime SDK env) into the Job container env (never argv). Without
        it run.py started but died ``No OAuth token found``. The pooled credential
        is released when the Job reaches a terminal state (reconcile / reap /
        stop), mirroring the subprocess path's _release_task_credential.

        #914: ``parallel``/``workers`` are forwarded to the backend so they reach
        the Job's run.py argv as ``--parallel [--workers N]``. They used to stop
        one frame up — ``_start_build_unit`` accepted them and never passed them
        on — so the #376 wave harness was inert on the kubejob path, which #671
        made the LIVE DEFAULT: intake labels, the portal setting and
        PFactory-planned contracts all resolved ``parallel`` correctly and then
        built serial anyway.

        #916: ``force`` is forwarded for the same reason, and it stopped at the
        same frame. The manifest hardcoded ``--force`` regardless, so the flag
        described the caller's intent on the subprocess path and nothing at all
        on this one.

        #916 (remainder): ``base_branch`` and ``mode`` stopped at the same frame
        too — a quick-mode task ran the full QA pipeline in the Job and a
        base-branch override was silently ignored (run.py auto-detected the
        default). Both now reach the Job argv/env via the manifest builder. The
        skill context (selectedSkills -> skill_context.md) is materialized into
        the authored spec dir HERE, before dispatch, exactly as the subprocess
        path does in ``_spawn_task_execution`` — the backend's worktree
        population copies the whole spec dir into the Job's ``/work``, so the
        file travels with it.
        """
        # #916: selectedSkills were never materialized for kubejob builds — the
        # in-pod path writes skill_context.md before spawning; do the same before
        # the worktree is populated so the Job's spec dir carries it.
        self._write_skill_context(project_path / ".aifactory" / "specs" / spec_id)
        # Pooled credential checkout (#670) — distinct token per concurrent Job.
        token, profile_id, profile_name = self._resolve_claude_token_pooled(task_id)
        if token:
            self._task_profiles[task_id] = {
                "profileId": profile_id,
                "profileName": profile_name,
                "attempt": 1,
            }
            _log.info(
                "[AgentService] kubejob build %s using Claude profile %s (%s)",
                sanitize_log(task_id),
                sanitize_log(profile_name),
                sanitize_log(profile_id),
            )
        else:
            _log.warning(
                "[AgentService] no Claude OAuth token available for kubejob "
                "build %s — run.py will fail with 'No OAuth token found'",
                sanitize_log(task_id),
            )
        try:
            await self._build_backend().dispatch(
                task_id=task_id,
                project_path=project_path,
                spec_id=spec_id,
                correlation_key=correlation_key,
                oauth_token=token,
                stop_after_planning=stop_after_planning,
                parallel=parallel,
                workers=workers,
                force=force,
                base_branch=base_branch,
                mode=mode,
            )
        except Exception:
            # Dispatch failed → the Job will never run, so return the credential
            # now rather than leaking it until a reaper that never fires.
            self._release_task_credential(task_id)
            raise
        # RFC-0017 #680: feed the cockpit log stream + rmux Live Console from the
        # Job pod's logs, exactly as the in-pod subprocess path does — the
        # prerequisite to making kubejob the default. Best-effort: any failure
        # here never affects dispatch or reconcile.
        await self._start_kubejob_log_stream(
            task_id=task_id, project_path=project_path, spec_id=spec_id
        )
        try:
            await self._safe_emit_task_status(task_id, "in_progress")
        except Exception:  # noqa: BLE001 - status emit must not break dispatch
            _log.debug(
                "[AgentService] coding status emit raised after Job dispatch (ignored)",
                exc_info=True,
            )

    async def _start_build_unit(
        self,
        *,
        task_id: str,
        project_path: Path,
        spec_id: str,
        correlation_key: str | None = None,
        auto_continue: bool = True,
        base_branch: str | None = None,
        mode: str | None = "full",
        force: bool = False,
        user_id: str = "",
        stop_after_planning: bool = False,
        parallel: bool | None = None,
        workers: int | None = None,
    ) -> Any:
        """Single backend-selection point for launching a build (RFC-0016 #671).

        kubejob → dispatch a k8s Job (returns None; the Job owns execution +
        reports its own terminal state). Else → the in-pod subprocess (returns
        the Process). EVERY build-launch path must route through here —
        ``start_task_execution`` AND the queue-drain paths — so a flipped backend
        (``AIFACTORY_BUILD_BACKEND=kubejob``) takes effect no matter how the build
        was promoted. The drain paths previously called ``_spawn_task_execution``
        directly, so a queued/drained build silently ran in-pod even with the
        flip on (the flip was a no-op for everything that went through admission).
        """
        if self._kubejob_backend_enabled():
            await self._dispatch_build_job(
                task_id=task_id,
                project_path=project_path,
                spec_id=spec_id,
                correlation_key=correlation_key,
                stop_after_planning=stop_after_planning,
                parallel=parallel,
                workers=workers,
                force=force,
                base_branch=base_branch,
                mode=mode,
            )
            return None
        return await self._spawn_task_execution(
            task_id=task_id,
            project_path=project_path,
            spec_id=spec_id,
            auto_continue=auto_continue,
            base_branch=base_branch,
            mode=mode,
            force=force,
            user_id=user_id,
            stop_after_planning=stop_after_planning,
            parallel=parallel,
            workers=workers,
        )

    async def _start_kubejob_log_stream(
        self, *, task_id: str, project_path: Path, spec_id: str
    ) -> None:
        """Start Job-native log streaming for a dispatched build (#680).

        Creates the passive rmux session (so the Live Console pane FIFO exists
        for viewers, mirroring the in-pod path's ``create_if_enabled``) and
        spawns a background task that follows the build Job's pod logs into the
        cockpit log sink + the rmux feed. The Job ref (namespace/job_name) is
        read from the durable worker_ref the backend just wrote. Wholly
        best-effort — never raises, never blocks dispatch.
        """
        # #1110: the Job's stdout carries the same [PHASE_EVENT] lines the in-pod
        # subprocess emits, and the control plane was throwing all of it at the
        # log pane without reading it. `_spawn_task_execution` is the ONLY place
        # that builds a TaskLogWriter, and the kubejob backend does not go
        # through it — so `task_logs.json` never appeared beside the spec, and
        # `get_execution_progress` (which reads exactly that file) returned None
        # for the whole build. The Job writes its own copy inside its /work
        # emptyDir and pushes it back at the END, which is why every phase
        # arrived in one step at completion.
        #
        # Write to the MAIN spec dir, not the worktree one the in-pod path uses:
        # the Job's worktree is inside the Job, and nothing syncs it here.
        log_writer = self._kubejob_log_writer(project_path, spec_id, task_id)

        ref = await self._kubejob_worker_ref(task_id)
        if ref is None:
            return
        namespace, job_name = ref

        # Passive rmux session so the WS bridge has a pane FIFO to stream from.
        try:
            from ..rmux.integration import create_if_enabled as _rmux_create

            project_id = task_id.split(":", 1)[0] if ":" in task_id else None
            await _rmux_create(spec_id, project_path, "", project_id=project_id)
        except Exception:  # noqa: BLE001 - rmux session is optional; degrade
            _log.debug(
                "[AgentService] rmux create for kubejob build raised (ignored); "
                "spec_id=%s",
                sanitize_log(spec_id),
                exc_info=True,
            )

        from .build_log_stream import KubeJobLogStreamer

        async def _cockpit_sink(line: str) -> None:
            await self._handle_output_line(
                task_id,
                line,
                current_phase=self._task_current_phases.get(
                    task_id, TaskPhase.PLANNING
                ),
                log_writer=log_writer,
                spec_id=spec_id,
            )

        rmux_feed = self._kubejob_rmux_feed()
        streamer = KubeJobLogStreamer(
            log_sink=_cockpit_sink,
            rmux_feed=rmux_feed,
            # #1228: the same gap #1110 closed for phase progress, for the file
            # that carries per-subtask state. The Job advances subtask
            # status/started_at in the plan inside its /work emptyDir; the
            # control plane pulled that copy only at completion, so CFactory's
            # execution DAG showed every node waiting for the whole build.
            # Pull it on the stream's own clock instead — the push and the pull
            # both already exist, they were just each called once.
            plan_sync=self._kubejob_plan_sync(project_path, spec_id),
        )

        async def _run_stream() -> None:
            try:
                await streamer.stream(
                    namespace=namespace, job_name=job_name, spec_id=spec_id
                )
            finally:
                self._kubejob_log_streamers.pop(task_id, None)

        self._cancel_kubejob_log_stream(task_id)
        self._kubejob_log_streamers[task_id] = asyncio.create_task(
            _run_stream(), name=f"kubejob-log-stream-{task_id}"
        )

    def _kubejob_log_writer(
        self, project_path: Path, spec_id: str, task_id: str
    ) -> TaskLogWriter | None:
        """Open ``task_logs.json`` beside the spec for a dispatched Job (#1110).

        Seeds the planning phase immediately, exactly as the in-pod path does at
        spawn, so ``GET /api/tasks/{id}`` reports a phase from the moment the
        Job is dispatched rather than ``executionProgress: null`` until the very
        end. Best-effort: progress reporting must never fail a dispatch.
        """
        try:
            spec_dir = project_path / ".aifactory" / "specs" / spec_id
            spec_dir.mkdir(parents=True, exist_ok=True)
            writer = TaskLogWriter(spec_dir)
            writer.set_phase_status(spec_id, TaskPhase.PLANNING, "active")
        except Exception:  # noqa: BLE001 - reporting must not break the build
            _log.warning(
                "[AgentService] could not open task_logs.json for %s; the build "
                "will run but report no live progress",
                sanitize_log(task_id),
                exc_info=True,
            )
            return None
        self._task_current_phases.setdefault(task_id, TaskPhase.PLANNING)
        self._task_log_writers[task_id] = (writer, writer)
        return writer

    @staticmethod
    def _kubejob_plan_sync(project_path: Path, spec_id: str) -> PlanSync | None:
        """Return the throttled plan pull for this build's log stream (#1228).

        ``None`` off the packed path — ``maybe_fetch_plan`` is a no-op there
        (``/work`` is the co-mounted data PVC, so the Job writes the control
        plane's own copy in place and there is nothing to pull), and returning
        None keeps the streamer from paying for a call that can never do
        anything.
        """
        # Read the env var NAME from workspace_fetch rather than spelling it
        # here, so the packed-path test cannot drift away from the one the pull
        # itself applies.
        from core.workspace_fetch import (  # noqa: PLC0415
            WORKSPACE_URI_ENV,
            maybe_fetch_plan,
        )

        if not os.environ.get(WORKSPACE_URI_ENV, "").strip():
            return None

        spec_dir = project_path / ".aifactory" / "specs" / spec_id
        return lambda: maybe_fetch_plan(spec_dir, spec_id)

    async def _kubejob_worker_ref(self, task_id: str) -> tuple[str, str] | None:
        """Read (namespace, job_name) from the build's durable worker_ref (#680).

        Returns None when the row/ref is missing or not a k8s-job — Job-native
        log streaming is simply skipped (the build is unaffected).
        """
        try:
            state = await self._store().get_state(task_id)
        except Exception:  # noqa: BLE001 - store read failure → skip streaming
            _log.debug(
                "[AgentService] worker_ref read failed for %s (no log stream)",
                sanitize_log(task_id),
                exc_info=True,
            )
            return None
        if not state:
            return None
        ref = state.get("worker_ref") or {}
        if ref.get("kind") != "k8s-job":
            return None
        namespace = ref.get("namespace")
        job_name = ref.get("job_name")
        if not namespace or not job_name:
            return None
        return str(namespace), str(job_name)

    @staticmethod
    def _kubejob_rmux_feed() -> Any:
        """Return the rmux feed callable, or None when rmux is off (#680)."""
        try:
            from ..rmux.integration import feed_if_enabled as _feed
            from ..rmux.integration import is_enabled as _on

            return _feed if _on() else None
        except Exception:  # noqa: BLE001 - rmux integration optional
            return None

    def _cancel_kubejob_log_stream(self, task_id: str) -> None:
        """Cancel + drop a build's Job-native log streamer if present (#680)."""
        streamer = self._kubejob_log_streamers.pop(task_id, None)
        if streamer is not None and not streamer.done():
            streamer.cancel()

    async def _reap_kubejob_console(self, task_id: str) -> None:
        """Reap the passive rmux pane created for a kubejob build (#680).

        ``spec_id`` is the suffix of the composite ``task_id``. No-op + never
        raises when rmux is off or no session exists.
        """
        spec_id = task_id.split(":", 1)[1] if ":" in task_id else task_id
        try:
            from ..rmux.integration import reap_if_enabled as _rmux_reap

            await _rmux_reap(spec_id)
        except Exception:  # noqa: BLE001 - console reap is best-effort
            _log.debug(
                "[AgentService] rmux reap for kubejob build raised (ignored); "
                "spec_id=%s",
                sanitize_log(spec_id),
                exc_info=True,
            )

    async def reconcile_kubejob_builds(self) -> dict[str, str]:
        """Reconcile k8s-Job builds from durable state (RFC-0016 #671).

        The conventions' reconcile-by-poll: for each ``running`` k8s-job row,
        observe whether the Job wrote a terminal lifecycle_state and, if so,
        drain any freed slot. A missed completion event therefore never strands
        a build. Returns ``{job_id: terminal_state}`` for builds that reached a
        terminal state this pass. No-op unless the kubejob backend is enabled.
        """
        out: dict[str, str] = {}
        if not self._kubejob_backend_enabled():
            return out
        try:
            rows = await self._store().get_active_kubejobs()
        except Exception:  # noqa: BLE001 - reconcile must never crash the loop
            _log.exception("[AgentService] kubejob reconcile: store read failed")
            return out
        backend = self._build_backend()
        for row in rows:
            job_id = row["job_id"]
            try:
                terminal = await backend.reconcile_by_poll(job_id)
            except Exception:  # noqa: BLE001
                _log.exception("[AgentService] kubejob reconcile failed for %s", job_id)
                continue
            if terminal is not None:
                out[job_id] = terminal
                # RFC-0017 #680: the Job is terminal → its pod log stream is
                # ending; cancel the streamer + reap the rmux pane so we don't
                # leak the background task or the console FIFO.
                self._cancel_kubejob_log_stream(job_id)
                await self._reap_kubejob_console(job_id)
                # #671 OAuth-env: return the pooled credential checked out at
                # dispatch now that the Job is done (mirrors the subprocess path).
                self._release_task_credential(job_id)
            else:
                # #1249: still running → this IS the tick that used to be the
                # ONLY route into check_review_obligation (monitor_process's
                # subprocess-tied loop), which never runs for a kubejob build.
                # A peer review requested but never started stayed stuck
                # forever with nothing to log, because the failure mode is
                # silence. Every active kubejob row passes through here, so
                # this is the one place that fixes it for both the direct
                # and queue-drained paths.
                await self._redrive_kubejob_review(job_id)
        if out:
            # Builds finished → fill freed slots from the FIFO queue.
            await self._drain_queue()
        return out

    async def _redrive_kubejob_review(self, job_id: str) -> None:
        """Re-drive a stuck peer review for a still-running kubejob build (#1249).

        ``check_review_obligation`` early-outs unless the MAIN spec dir has a
        ``qa_review_cycle.json`` — but the running build writes that file into
        its WORKTREE spec dir, and nothing copies it to main under kubejob (the
        generic worktree-sync loop that used to do that never runs here
        either). ``check_review_obligation`` now syncs that one file itself
        before checking, so calling it here is enough — no new sync loop.
        Best-effort throughout: a redrive failure must never affect reconcile.
        """
        project_id, _, spec_id = job_id.partition(":")
        if not spec_id:
            return
        try:
            project_path = resolve_project_path(project_id)
        except Exception:  # noqa: BLE001 - unknown/unresolvable project
            return
        try:
            await asyncio.to_thread(
                review_redrive_service.check_review_obligation, project_path, spec_id
            )
        except Exception:  # noqa: BLE001 - redrive must never crash reconcile
            _log.debug(
                "[AgentService] kubejob review re-drive check skipped for %s",
                job_id,
                exc_info=True,
            )

    async def kubejob_reconcile_loop(
        self, *, stop: asyncio.Event, interval_seconds: float = 15.0
    ) -> None:
        """Periodic reconcile-by-poll + reaper for k8s-Job builds (#671).

        Started from the app lifespan only when the kubejob backend is enabled.
        Each tick polls Postgres for terminal transitions the Jobs wrote
        (so a missed completion event never strands a build) and reaps vanished
        Jobs. Never raises — a bad tick is logged and the loop continues.
        """
        _log.info(
            "[AgentService] kubejob reconcile loop started (interval=%.0fs)",
            interval_seconds,
        )
        tick = 0
        while not stop.is_set():
            try:
                await self.reconcile_kubejob_builds()
                await self.reap_kubejob_builds()
                # Task-level reaper on a slower cadence (#1001): the spec-dir scan
                # is heavier than the job-state poll and the deadline is generous,
                # so every ~4th tick (~60s at the default interval) is plenty.
                tick += 1
                if tick % 4 == 0:
                    await self.reap_abandoned_tasks()
            except Exception:  # noqa: BLE001 - loop must survive a bad tick
                _log.exception("[AgentService] kubejob reconcile tick failed")
            # Timeout is the normal tick cadence, not an error.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        _log.info("[AgentService] kubejob reconcile loop stopped")

    async def reap_kubejob_builds(
        self, *, deadline_seconds: int | None = None
    ) -> list[str]:
        """Reap stranded k8s-Job builds (RFC-0016 #671 reaper).

        Marks a ``running`` k8s-job row ``failed`` when its Job disappears /
        exceeds the deadline without a terminal write, then drains. Returns the
        reaped job_ids. No-op unless the kubejob backend is enabled.
        """
        if not self._kubejob_backend_enabled():
            return []
        try:
            reaped = await self._build_backend().reap_vanished_jobs(
                deadline_seconds=deadline_seconds
            )
        except Exception:  # noqa: BLE001 - reaper must never crash the loop
            _log.exception("[AgentService] kubejob reaper failed")
            return []
        if reaped:
            # #671 OAuth-env: return each reaped build's pooled credential.
            for job_id in reaped:
                self._release_task_credential(job_id)
            await self._drain_queue()
        return reaped

    # Frontend statuses that claim a live build. A task at one of these with no
    # live build behind it is stranded and lists as "running" forever (#1001).
    _REAPABLE_RUNNING_STATUSES = frozenset({"in_progress"})

    async def reap_abandoned_tasks(self, *, deadline_seconds: int = 600) -> list[str]:
        """Reap a task stuck ``in_progress`` with no live build behind it (#1001).

        A build's Job can die WITHOUT a terminal event (killed pod, node drain,
        control-plane roll, or a manually-cleared job-state row); the task then
        lists as ``in_progress`` forever and the cockpit shows it as running. This
        is the task-level complement to ``reap_kubejob_builds`` (which only touches
        tasks that still have a durable job-state row).

        Mark it ``failed`` — AIFactory's canonical failure path (moves it out of the
        running bucket + emits the status event) — when ALL hold: frontend status is
        ``in_progress``; it is NOT running in THIS pod's subprocess table; it has no
        ``running`` durable job-state row; and its spec dir has been untouched past
        ``deadline_seconds``. The staleness grace makes false-reaping impossible for
        a just-dispatched build (status set before its job-state row lands) and for a
        genuinely-live build (which keeps writing files, so its mtime stays fresh).
        Best-effort; never raises. Returns the reaped task ids."""
        from datetime import UTC, datetime

        from ..routes.projects import load_projects
        from ..routes.task_service import get_spec_dirs, spec_to_task

        reaped: list[str] = []
        now = datetime.now(UTC)
        try:
            projects = load_projects()
        except Exception:  # noqa: BLE001 - reaper must never crash the loop
            return reaped
        for pid, pdata in projects.items():
            project_path = Path(pdata.get("path", ""))
            try:
                spec_dirs = get_spec_dirs(project_path)
            except Exception:  # noqa: BLE001
                continue
            for spec_dir in spec_dirs:
                try:
                    task = spec_to_task(pid, spec_dir)
                except Exception:  # noqa: BLE001
                    continue
                if task.status not in self._REAPABLE_RUNNING_STATUSES:
                    continue
                if self.is_running(task.id):
                    continue  # a live subprocess build in THIS pod
                if await self._has_live_kubejob(task.id):
                    continue  # a live k8s build (durable running row)
                if not self._task_stale(task.updated_at, now, deadline_seconds):
                    continue
                try:
                    await self._update_plan_status(
                        project_path, spec_dir.name, "failed", task.id
                    )
                    reaped.append(task.id)
                    _log.info(
                        "[AgentService] reaped abandoned in_progress task %s "
                        "(no live build, stale > %ds) (#1001)",
                        task.id,
                        deadline_seconds,
                    )
                except Exception:  # noqa: BLE001
                    _log.exception(
                        "[AgentService] reap of abandoned task %s failed", task.id
                    )
        return reaped

    async def _has_live_kubejob(self, task_id: str) -> bool:
        """True when a ``running`` durable job-state row backs this task."""
        if not getattr(self, "_store_enabled", False):
            return False
        try:
            state = await self._store().get_state(task_id)
        except Exception:  # noqa: BLE001
            return False
        return bool(state and state.get("lifecycle_state") == "running")

    @staticmethod
    def _task_stale(updated_at: str, now: Any, deadline_seconds: int) -> bool:
        """True when ``updated_at`` (spec-dir mtime, ISO) is older than the
        deadline. An unparseable timestamp reads NOT stale — never reap on doubt."""
        from datetime import UTC, datetime

        try:
            dt = datetime.fromisoformat(updated_at)
        except (ValueError, TypeError):
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return (now - dt).total_seconds() > deadline_seconds

    async def _stop_kubejob_build(self, task_id: str) -> bool:
        """Stop a build running as a k8s Job (RFC-0016 #671).

        Deletes the Job (best-effort) and marks the durable row failed so the
        slot frees and the reconcile/reaper loops don't keep watching it.
        Returns True when a running k8s-job row was found + stopped.
        """
        try:
            state = await self._store().get_state(task_id)
        except Exception:  # noqa: BLE001
            _log.warning(
                "[AgentService] could not read state to stop kubejob %s",
                sanitize_log(task_id),
                exc_info=True,
            )
            return False
        if state is None or state.get("lifecycle_state") != "running":
            return False
        if (state.get("worker_ref") or {}).get("kind") != "k8s-job":
            return False
        # RFC-0017 #680: stop the Job-native log streamer + reap the console
        # pane before deleting the Job (the pod log stream is about to vanish).
        self._cancel_kubejob_log_stream(task_id)
        await self._reap_kubejob_console(task_id)
        try:
            await self._build_backend().delete_job(task_id)
        except Exception:  # noqa: BLE001 - delete is best-effort; row mark below frees the slot
            _log.warning(
                "[AgentService] could not delete k8s Job for %s (ignored)",
                sanitize_log(task_id),
                exc_info=True,
            )
        try:
            await self._store().mark_terminal(
                task_id, "failed", error="stopped by user"
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "[AgentService] could not free durable slot on kubejob stop %s",
                sanitize_log(task_id),
                exc_info=True,
            )
        # #671 OAuth-env: return the pooled credential checked out at dispatch.
        self._release_task_credential(task_id)
        await self._safe_emit_task_status(task_id, "human_review", "errors")
        await self._drain_queue()
        _log.info("[AgentService] Stopped k8s-Job build %s", sanitize_log(task_id))
        return True
