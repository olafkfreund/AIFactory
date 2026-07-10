"""Durable, multi-replica-safe job-state store (RFC-0016 #668).

Backs AIFactory's admission cap + FIFO queue + running set with Postgres so
they survive a web-server restart and stay consistent across control-plane
replicas, conforming to ``apis/job-state.schema.json`` (``service="aifactory"``,
``kind="build"``) and ``apis/concurrency-conventions.md`` in the Factory hub.

The two operations that must not race across replicas — granting a slot
(``admit``) and filling a freed slot (``drain``) — run inside a single
transaction that takes a ``SELECT ... FOR UPDATE`` over this service's active
(``queued``/``running``) rows. On Postgres an additional per-service
transaction advisory lock (``pg_advisory_xact_lock``) serialises admissions so
two replicas inserting *brand-new* ``job_id`` rows can't both observe the same
free slot before either row exists. The result: the live ``running`` count is
read under the lock, so concurrent admits can never exceed ``MAX_CONCURRENT_TASKS``
and the ``job_id`` primary key prevents double-starting the same job.

Fallback: when ``DATABASE_URL`` is unset the caller keeps its in-memory
cap/queue (single-pod dev) and logs that it is not multi-replica safe — this
store is never constructed in that mode (see ``store_enabled``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text

_log = logging.getLogger(__name__)

SERVICE = "aifactory"
KIND = "build"

# Canonical lifecycle states (subset AIFactory uses) from status-taxonomy.json.
ACTIVE_STATES = ("queued", "running")

# Stable 64-bit key for the per-service admission advisory lock (Postgres).
# Arbitrary constant — only its uniqueness within the deployment matters.
_ADVISORY_LOCK_KEY = 0x0A1FAC700668  # "AIFAC...668"


def store_enabled() -> bool:
    """True when a real shared Postgres is configured via ``DATABASE_URL``.

    Per the concurrency conventions the durable store is used only when
    ``DATABASE_URL`` is set. An unset (or whitespace-only) value means
    single-pod dev → in-memory fallback. The default SQLite dev DB does NOT
    enable the store because it is pod-local and not multi-replica safe.
    """
    return bool(os.environ.get("DATABASE_URL", "").strip())


def _now() -> datetime:
    # Naive UTC to match the rest of the schema's DateTime columns (the tables
    # use ``TIMESTAMP WITHOUT TIME ZONE`` + ``func.now()``); asyncpg rejects a
    # tz-aware value bound to a naive column. ISO strings in the ``admission``
    # JSON still carry the UTC instant.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime | None) -> str | None:
    # Emit an explicit-UTC ISO 8601 string for the admission JSON timestamps
    # (the column value stays naive; this is just the JSON representation).
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat()


@dataclass
class SpawnArgs:
    """The args needed to (re)spawn a build — persisted in ``spawn_args``.

    Mirrors the queued-build payload so a row promoted from ``queued`` (on
    drain) or reconstructed after a restart spawns identically to one admitted
    immediately. ``project_path`` is stored as a string for JSON portability.
    """

    project_path: str
    spec_id: str
    auto_continue: bool = True
    base_branch: str | None = None
    mode: str | None = "full"
    force: bool = False
    user_id: str = ""
    stop_after_planning: bool = False
    parallel: bool | None = None
    workers: int | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SpawnArgs":
        fields = {
            "project_path",
            "spec_id",
            "auto_continue",
            "base_branch",
            "mode",
            "force",
            "user_id",
            "stop_after_planning",
            "parallel",
            "workers",
        }
        return cls(**{k: v for k, v in data.items() if k in fields})


class JobStateStore:
    """Postgres-backed admission/queue/running store for AIFactory builds."""

    def __init__(self, session_factory: Any = None) -> None:
        if session_factory is None:
            # Lazy import keeps non-DB code paths (CLI, in-memory tests) clean.
            from ..database.engine import async_session_factory

            session_factory = async_session_factory
        self._session_factory = session_factory

    # -- internal helpers ---------------------------------------------------

    async def _maybe_advisory_lock(self, session: Any) -> None:
        """Take the per-service admission advisory lock on Postgres.

        No-op on SQLite (tests / dev) where there are no advisory locks and a
        single connection already serialises writers. The lock is released
        automatically at transaction end (``pg_advisory_xact_lock``).
        """
        if session.bind.dialect.name == "postgresql":
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": _ADVISORY_LOCK_KEY},
            )

    async def _lock_active_rows(self, session: Any) -> None:
        """``SELECT ... FOR UPDATE`` this service's active rows.

        Locks every ``queued``/``running`` row so a concurrent admit/drain in
        another replica blocks until this transaction commits — the live count
        we read next is therefore authoritative. ``FOR UPDATE`` is skipped on
        SQLite (unsupported), which is fine: SQLite is single-writer.
        """
        from ..database.models import JobState

        stmt = (
            select(JobState.job_id)
            .where(
                JobState.service == SERVICE,
                JobState.lifecycle_state.in_(ACTIVE_STATES),
            )
            .order_by(JobState.created_at)
        )
        if session.bind.dialect.name != "sqlite":
            stmt = stmt.with_for_update()
        await session.execute(stmt)

    async def _count_running(self, session: Any) -> int:
        from ..database.models import JobState

        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(JobState)
                    .where(
                        JobState.service == SERVICE,
                        JobState.lifecycle_state == "running",
                    )
                )
            ).scalar_one()
        )

    async def _queue_size(self, session: Any) -> int:
        from ..database.models import JobState

        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(JobState)
                    .where(
                        JobState.service == SERVICE,
                        JobState.lifecycle_state == "queued",
                    )
                )
            ).scalar_one()
        )

    # -- public API ---------------------------------------------------------

    async def admit(
        self,
        job_id: str,
        spawn_args: SpawnArgs,
        cap: int,
        correlation_key: str | None = None,
    ) -> str:
        """Grant a slot or queue the job — atomic across replicas.

        Returns ``"started"`` (a slot was granted; the row is ``running``) or
        ``"queued"`` (at the cap; the row is ``queued`` FIFO). Raises
        ``ValueError`` if the same ``job_id`` is already active (→ HTTP 409),
        preserving the per-task guard.

        ``cap <= 0`` means unlimited (back-compat). The decision uses the live
        ``running`` count read under the advisory lock + ``FOR UPDATE``.
        """
        from ..database.models import JobState

        async with self._session_factory() as session:
            async with session.begin():
                await self._maybe_advisory_lock(session)
                await self._lock_active_rows(session)

                existing = await session.get(JobState, job_id)
                if existing is not None and existing.lifecycle_state in ACTIVE_STATES:
                    raise ValueError(
                        f"Task {job_id} is already {existing.lifecycle_state}"
                    )

                running = await self._count_running(session)
                grant = cap <= 0 or running < cap
                now = _now()

                if grant:
                    admission = {
                        "enqueued_at": None,
                        "queue_position": None,
                        "started_at": _iso(now),
                    }
                    lifecycle = "running"
                else:
                    admission = {
                        "enqueued_at": _iso(now),
                        "queue_position": await self._queue_size(session),
                        "started_at": None,
                    }
                    lifecycle = "queued"

                if existing is None:
                    session.add(
                        JobState(
                            job_id=job_id,
                            correlation_key=correlation_key,
                            service=SERVICE,
                            kind=KIND,
                            lifecycle_state=lifecycle,
                            phase="coding" if grant else None,
                            attempt=1,
                            admission=admission,
                            worker_ref={"kind": "subprocess"} if grant else None,
                            spawn_args=spawn_args.to_json(),
                        )
                    )
                else:
                    # Re-admit a previously-terminal job_id (e.g. a manual
                    # restart of a finished/failed build): reset it cleanly.
                    existing.correlation_key = (
                        correlation_key or existing.correlation_key
                    )
                    existing.lifecycle_state = lifecycle
                    existing.phase = "coding" if grant else None
                    existing.admission = admission
                    existing.worker_ref = {"kind": "subprocess"} if grant else None
                    existing.spawn_args = spawn_args.to_json()
                    existing.result = None
                    existing.error = None
                    existing.ended_at = None

                return "started" if grant else "queued"

    async def mark_running(self, job_id: str) -> None:
        """Mark a granted slot as actually running (worker_ref=subprocess)."""
        from ..database.models import JobState

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(JobState, job_id)
                if row is None:
                    return
                row.lifecycle_state = "running"
                row.phase = "coding"
                row.worker_ref = {"kind": "subprocess"}
                adm = dict(row.admission or {})
                adm.setdefault("started_at", _iso(_now()))
                row.admission = adm

    async def drain(self, cap: int) -> list[tuple[str, SpawnArgs]]:
        """Promote queued jobs FIFO into freed slots — atomic across replicas.

        Returns ``(job_id, SpawnArgs)`` for each job promoted to ``running``,
        in FIFO order, so the caller can spawn them. Runs under the same
        advisory lock + ``FOR UPDATE`` as ``admit`` so a finishing build in one
        replica and a fresh admit in another can't both claim the same slot.
        """
        from ..database.models import JobState

        promoted: list[tuple[str, SpawnArgs]] = []
        async with self._session_factory() as session:
            async with session.begin():
                await self._maybe_advisory_lock(session)
                await self._lock_active_rows(session)

                running = await self._count_running(session)
                free = (cap - running) if cap > 0 else 1_000_000
                if free <= 0:
                    return []

                queued = (
                    (
                        await session.execute(
                            select(JobState)
                            .where(
                                JobState.service == SERVICE,
                                JobState.lifecycle_state == "queued",
                            )
                            .order_by(JobState.created_at)
                            .limit(free)
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in queued:
                    row.lifecycle_state = "running"
                    row.phase = "coding"
                    row.worker_ref = {"kind": "subprocess"}
                    adm = dict(row.admission or {})
                    adm["started_at"] = _iso(_now())
                    adm["queue_position"] = None
                    row.admission = adm
                    promoted.append(
                        (row.job_id, SpawnArgs.from_json(row.spawn_args or {}))
                    )
        return promoted

    async def mark_terminal(
        self,
        job_id: str,
        lifecycle_state: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        usage: dict[str, Any] | None = None,
        service_status: str | None = None,
    ) -> None:
        """Record a terminal transition (done/failed/stuck/review).

        Frees the slot by moving the row out of ``running``. Idempotent: a
        second terminal write for the same job_id is harmless. Enforces the
        never-overclaim rule — ``failed``/``stuck`` carry an ``error``.
        """
        from ..database.models import JobState

        if lifecycle_state in ("failed", "stuck") and not error:
            error = "build failed (no detail captured)"

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(JobState, job_id)
                if row is None:
                    return
                row.lifecycle_state = lifecycle_state
                if result is not None:
                    row.result = result
                if error is not None:
                    row.error = error
                if usage is not None:
                    row.usage = usage
                if service_status is not None:
                    row.service_status = service_status
                if lifecycle_state in ("done", "failed", "stuck"):
                    row.ended_at = _now()

    async def set_worker_ref(self, job_id: str, worker_ref: dict[str, Any]) -> None:
        """Record the execution-unit reference for a job (RFC-0016 #671).

        Used by the kubejob build backend after it creates the k8s Job, so the
        reconcile-by-poll + reaper loops can find the Job (namespace + job_name)
        for a ``running`` row. No-op when the row is gone.
        """
        from ..database.models import JobState

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(JobState, job_id)
                if row is not None:
                    row.worker_ref = worker_ref

    async def get_state(self, job_id: str) -> dict[str, Any] | None:
        """Read a job-state row's salient fields (reconcile-by-poll, #671).

        Returns ``None`` when the row is absent. Used by the kubejob backend's
        polling reconcile to detect a terminal transition the Job wrote (so a
        missed completion event never strands the control plane).
        """
        from ..database.models import JobState

        async with self._session_factory() as session:
            row = await session.get(JobState, job_id)
            if row is None:
                return None
            return {
                "job_id": row.job_id,
                "lifecycle_state": row.lifecycle_state,
                "worker_ref": dict(row.worker_ref or {}),
                "result": row.result,
                "error": row.error,
                "updated_at": row.updated_at,
            }

    async def get_active_kubejobs(self) -> list[dict[str, Any]]:
        """Active (``running``) rows whose worker is a k8s Job (#671 reaper).

        Returns ``{job_id, job_name, namespace, updated_at}`` for each running
        build executed as a k8s Job, so the reaper can check whether the Job
        still exists / has exceeded its deadline and, if it vanished without a
        terminal write, mark the row failed (never strand a build).
        """
        from ..database.models import JobState

        out: list[dict[str, Any]] = []
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(JobState).where(
                            JobState.service == SERVICE,
                            JobState.lifecycle_state == "running",
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                ref = dict(row.worker_ref or {})
                if ref.get("kind") != "k8s-job":
                    continue
                out.append(
                    {
                        "job_id": row.job_id,
                        "job_name": ref.get("job_name"),
                        "namespace": ref.get("namespace"),
                        "updated_at": row.updated_at,
                    }
                )
        return out

    async def increment_attempt(self, job_id: str) -> None:
        """Bump the RFC-0008 failover/restart attempt counter."""
        from ..database.models import JobState

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(JobState, job_id)
                if row is not None:
                    row.attempt = (row.attempt or 1) + 1

    async def remove_queued(self, job_id: str) -> bool:
        """Remove a parked (queued) job so it never auto-starts. Returns True
        when a queued row was removed (the stop-a-queued-task path)."""
        from ..database.models import JobState

        async with self._session_factory() as session:
            async with session.begin():
                row = await session.get(JobState, job_id)
                if row is None or row.lifecycle_state != "queued":
                    return False
                await session.delete(row)
                return True

    async def is_running(self, job_id: str) -> bool:
        return await self._has_state(job_id, "running")

    async def is_queued(self, job_id: str) -> bool:
        return await self._has_state(job_id, "queued")

    async def _has_state(self, job_id: str, state: str) -> bool:
        from ..database.models import JobState

        async with self._session_factory() as session:
            row = await session.get(JobState, job_id)
            return row is not None and row.lifecycle_state == state

    async def get_running_job_ids(self) -> list[str]:
        return await self._job_ids_in("running")

    async def get_queued_job_ids(self) -> list[str]:
        """Queued job_ids in FIFO (enqueue) order."""
        return await self._job_ids_in("queued")

    async def _job_ids_in(self, state: str) -> list[str]:
        from ..database.models import JobState

        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(JobState.job_id)
                        .where(
                            JobState.service == SERVICE,
                            JobState.lifecycle_state == state,
                        )
                        .order_by(JobState.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return list(rows)

    async def reconstruct(self) -> dict[str, list[tuple[str, SpawnArgs]]]:
        """Rebuild in-flight state after a control-plane restart.

        Returns ``{"running": [...], "queued": [...]}`` of ``(job_id,
        SpawnArgs)`` for this service's active rows, FIFO. A fresh replica calls
        this on boot so the admission cap/queue reflect Postgres rather than an
        empty in-memory dict (concurrency conventions §1 durability rule).
        """
        from ..database.models import JobState

        out: dict[str, list[tuple[str, SpawnArgs]]] = {
            "running": [],
            "queued": [],
        }
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(JobState)
                        .where(
                            JobState.service == SERVICE,
                            JobState.lifecycle_state.in_(ACTIVE_STATES),
                        )
                        .order_by(JobState.created_at)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                out[row.lifecycle_state].append(
                    (row.job_id, SpawnArgs.from_json(row.spawn_args or {}))
                )
        return out
