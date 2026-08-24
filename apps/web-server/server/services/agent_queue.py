"""Task queue draining + durable admission — QueueMixin (#703).

The queue/admission methods (_mark_task_queued, _drain_queue,
_drain_queue_durable), lifted out of the AgentService god-class into a mixin
(see #703 / #704 for the pattern). AgentService inherits this mixin; methods run
as bound methods via the MRO.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from factory_common.logsafe import sanitize_log

from server.specpath import spec_dir_for

from ..websockets.events import emit_task_status
from . import task_control

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger(__name__)


class QueueMixin:
    """Task queue draining + durable admission methods for AgentService."""

    if TYPE_CHECKING:
        # Attributes/methods provided by the concrete host (AgentService) and
        # sibling mixins; declared so mypy resolves the self.* refs (#703 pattern).
        _admission_lock: Any
        running_tasks: dict[str, Any]
        _store_enabled: bool
        _task_queue: Any
        _concurrency_cap: Callable[..., Any]
        _spawn_task_execution: Callable[..., Any]
        _start_build_unit: Callable[..., Any]
        _store: Callable[..., Any]

    async def _mark_task_queued(
        self, task_id: str, project_path: Path, spec_id: str
    ) -> None:
        """Persist + emit the ``queued`` status for a parked build (#668)."""
        spec_dir = spec_dir_for(project_path, spec_id)
        try:
            task_control.write_control(
                spec_dir,
                status="queued",
                clear_review_reason=True,
                updated_by="web_server",
            )
        except OSError as e:
            _log.warning(
                "[AgentService] could not persist queued status for %s: %s",
                sanitize_log(task_id),
                sanitize_log(e),
            )
        try:
            await emit_task_status(task_id, "queued")
        except Exception:  # noqa: BLE001 - status emit must not break admission
            _log.debug(
                "[AgentService] queued status emit raised (ignored)", exc_info=True
            )

    async def _drain_queue(self) -> None:
        """Start queued builds FIFO while there is free capacity (#668).

        Called from the subprocess-exit monitor after a build finishes so a
        freed slot pulls the next queued task. Guarded by the admission lock so
        it can never race with ``start_task_execution`` on the same slot. Each
        spawn happens under the lock, keeping ``running_tasks`` counting exact;
        a failed spawn drops that task and moves on (no busy-wait, no requeue
        loop). Never raises — it runs in the monitor's teardown path.
        """
        if self._store_enabled:
            await self._drain_queue_durable()
            return

        async with self._admission_lock:
            cap = self._concurrency_cap()
            while self._task_queue and (cap <= 0 or len(self.running_tasks) < cap):
                qt = self._task_queue.popleft()
                # Skip a task that was started/stopped out of band while queued.
                if qt.task_id in self.running_tasks:
                    continue
                try:
                    await self._start_build_unit(
                        task_id=qt.task_id,
                        project_path=qt.project_path,
                        spec_id=qt.spec_id,
                        auto_continue=qt.auto_continue,
                        base_branch=qt.base_branch,
                        mode=qt.mode,
                        force=qt.force,
                        user_id=qt.user_id,
                        stop_after_planning=qt.stop_after_planning,
                        parallel=qt.parallel,
                        workers=qt.workers,
                    )
                    _log.info(
                        "[AgentService] Dequeued + started %s (%d running, %d queued)",
                        qt.task_id,
                        len(self.running_tasks),
                        len(self._task_queue),
                    )
                except Exception:  # noqa: BLE001 - one bad spawn must not stall the queue
                    _log.exception(
                        "[AgentService] failed to start queued task %s — dropping",
                        qt.task_id,
                    )

    async def _drain_queue_durable(self) -> None:
        """Promote queued builds into freed slots via the durable store (#668).

        The store's ``drain`` runs the same SELECT ... FOR UPDATE + advisory
        lock as ``admit``, so a finishing build in this replica and a fresh
        admit in another can't both claim the freed slot. Each promoted row
        comes back already flipped to ``running`` in Postgres; we then spawn the
        local subprocess. A spawn failure frees the durable slot (marks the row
        failed) so a slot is never leaked. Never raises (monitor teardown path).
        """
        cap = self._concurrency_cap()
        try:
            promoted = await self._store().drain(cap)
        except Exception:  # noqa: BLE001 - drain must not break the exit monitor
            _log.exception("[AgentService] durable drain failed")
            return

        for task_id, spawn_args in promoted:
            # Skip a row we already run locally (a restart-recovery row, or a
            # task started out of band) — the store flipped it to running but
            # this replica owns the live subprocess.
            if task_id in self.running_tasks:
                continue
            try:
                await self._start_build_unit(
                    task_id=task_id,
                    project_path=Path(spawn_args.project_path),
                    spec_id=spawn_args.spec_id,
                    auto_continue=spawn_args.auto_continue,
                    base_branch=spawn_args.base_branch,
                    mode=spawn_args.mode,
                    force=spawn_args.force,
                    user_id=spawn_args.user_id,
                    stop_after_planning=spawn_args.stop_after_planning,
                    parallel=spawn_args.parallel,
                    workers=spawn_args.workers,
                )
                _log.info(
                    "[AgentService] Dequeued + started %s (durable store)",
                    task_id,
                )
            except Exception:  # noqa: BLE001 - one bad spawn must not stall the queue
                _log.exception(
                    "[AgentService] failed to start queued task %s — "
                    "freeing durable slot",
                    task_id,
                )
                try:
                    await self._store().mark_terminal(
                        task_id, "failed", error="spawn failed on dequeue"
                    )
                except Exception:  # noqa: BLE001
                    _log.exception(
                        "[AgentService] could not free durable slot for %s",
                        task_id,
                    )
