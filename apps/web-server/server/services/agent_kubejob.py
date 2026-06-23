"""Kubejob build backend — KubejobMixin for AgentService (#703 spike).

The RFC-0016 kubejob-backend methods, lifted out of the AgentService god-class
into a mixin so the concern lives in its own file. AgentService inherits this
mixin; the methods still run as bound methods on the AgentService instance (they
call sibling methods/attrs like self._emit_log / self._store via the MRO).

This is the #703 spike to measure the mypy cost of the mixin approach.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agent_task_models import TaskLog

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger(__name__)


class KubejobMixin:
    """Kubejob build-backend methods (RFC-0016). Mixed into AgentService."""

    if TYPE_CHECKING:
        # Attributes/methods provided by the concrete host (AgentService);
        # declared here so mypy can resolve the self.* references in a mixin.
        _kubejob_log_streamers: dict[str, Any]
        _store_enabled: bool
        _task_profiles: dict[str, Any]
        _drain_queue: Callable[..., Any]
        _emit_log: Callable[..., Any]
        _release_task_credential: Callable[..., Any]
        _resolve_claude_token_pooled: Callable[..., Any]
        _safe_emit_task_status: Callable[..., Any]
        _spawn_task_execution: Callable[..., Any]
        _store: Callable[..., Any]

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

            self._kubejob_build_backend = KubeJobBuildBackend(self._store())
        return self._kubejob_build_backend

    async def _dispatch_build_job(
        self,
        *,
        task_id: str,
        project_path: Path,
        spec_id: str,
        correlation_key: str | None,
        stop_after_planning: bool = False,
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
        """
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
                task_id,
                profile_name,
                profile_id,
            )
        else:
            _log.warning(
                "[AgentService] no Claude OAuth token available for kubejob "
                "build %s — run.py will fail with 'No OAuth token found'",
                task_id,
            )
        try:
            await self._build_backend().dispatch(
                task_id=task_id,
                project_path=project_path,
                spec_id=spec_id,
                correlation_key=correlation_key,
                oauth_token=token,
                stop_after_planning=stop_after_planning,
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
                spec_id,
                exc_info=True,
            )

        from .build_log_stream import KubeJobLogStreamer

        async def _cockpit_sink(line: str) -> None:
            await self._emit_log(
                TaskLog(task_id=task_id, content=line, source="stdout", level="info")
            )

        rmux_feed = self._kubejob_rmux_feed()
        streamer = KubeJobLogStreamer(log_sink=_cockpit_sink, rmux_feed=rmux_feed)

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
                task_id,
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
                spec_id,
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
        if out:
            # Builds finished → fill freed slots from the FIFO queue.
            await self._drain_queue()
        return out

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
        while not stop.is_set():
            try:
                await self.reconcile_kubejob_builds()
                await self.reap_kubejob_builds()
            except Exception:  # noqa: BLE001 - loop must survive a bad tick
                _log.exception("[AgentService] kubejob reconcile tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass
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
                task_id,
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
                task_id,
                exc_info=True,
            )
        try:
            await self._store().mark_terminal(
                task_id, "failed", error="stopped by user"
            )
        except Exception:  # noqa: BLE001
            _log.warning(
                "[AgentService] could not free durable slot on kubejob stop %s",
                task_id,
                exc_info=True,
            )
        # #671 OAuth-env: return the pooled credential checked out at dispatch.
        self._release_task_credential(task_id)
        await self._safe_emit_task_status(task_id, "human_review", "errors")
        await self._drain_queue()
        _log.info("[AgentService] Stopped k8s-Job build %s", task_id)
        return True
