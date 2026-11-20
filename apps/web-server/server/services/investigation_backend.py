"""Lifecycle of isolated, read-only investigation Jobs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import secrets
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from kubernetes_asyncio import client, config
from sqlalchemy import func, select

from server.database.models import InvestigationLog
from server.services import (
    investigation_export,
    investigation_manifest,
    investigation_scrub,
    investigation_tests,
)
from server.services.build_backend import (
    _DEFAULT_DATA_ROOT,
    _DEFAULT_NAMESPACE,
    _ENV_DATA_ROOT,
    _ENV_NAMESPACE,
)
from server.services.investigation_store import InvestigationStore, SlotTaken

_log = logging.getLogger(__name__)
_LIMITS = {"minutes": 30, "tokens": 2_000_000, "turns": 200, "test_calls": 10}
_NOT_FOUND = 404
_CONFLICT = 409
_MAX_REQUEST = 2000
_MAX_BATCH = 200
_MAX_LOGS = 20_000
_REF = re.compile(r"(?<![\w/])([\w./-]+):(\d+)(?!\d)")


def investigations_enabled() -> bool:
    value = os.environ.get("AIFACTORY_INVESTIGATIONS", "off")
    if value == "off":
        return False
    if value == "on":
        return True
    if value == "drain":
        return True
    raise ValueError("Invalid AIFACTORY_INVESTIGATIONS; valid values: off, on, drain")


def _field(value: Any, name: str, default: Any = None) -> Any:
    return (
        value.get(name, default)
        if isinstance(value, dict)
        else getattr(value, name, default)
    )


def _items(value: Any) -> list[Any]:
    items: list[Any] = _field(value, "items", [])
    return items


def _code(exc: Exception) -> int | None:
    return getattr(exc, "status", None)


class InvestigationRefused(RuntimeError):  # noqa: N818 - refusal is public API terminology
    """A requested operation cannot be safely admitted.

    ``detail`` is always fixed text written in this module, so routes may return it.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class InvestigationBackend:
    """One service instance; all state transitions use the store's row lock."""

    def __init__(  # noqa: PLR0913 - injectable cluster clients and clock
        self,
        store: InvestigationStore | None = None,
        *,
        batch: Any = None,
        core: Any = None,
        networking: Any = None,
        clock: Callable[[], datetime] | None = None,
        release_credential: Callable[[str], Any] | None = None,
    ) -> None:
        self.store = store or InvestigationStore()
        self.batch = batch
        self.core = core
        self.networking = networking
        self.clock = clock or (lambda: datetime.now(UTC).replace(tzinfo=None))
        self.release_credential = release_credential or (lambda _id: None)
        self._tokens: dict[str, tuple[str, str]] = {}
        self._dispatching: set[str] = set()
        self._loop: asyncio.Task[None] | None = None
        self._last_purge: datetime | None = None

    async def _clients(self) -> None:
        if (
            self.batch is not None
            and self.core is not None
            and self.networking is not None
        ):
            return
        try:
            config.load_incluster_config()  # type: ignore[no-untyped-call]
        except Exception:  # noqa: BLE001 - cluster config can raise several errors
            await config.load_kube_config()
        api = client.ApiClient()
        self.batch = self.batch or client.BatchV1Api(api)
        self.core = self.core or client.CoreV1Api(api)
        self.networking = self.networking or client.NetworkingV1Api(api)

    @property
    def namespace(self) -> str:
        return os.environ.get(_ENV_NAMESPACE, _DEFAULT_NAMESPACE)

    def _root(self, run_id: str) -> Path:
        return (
            Path(os.environ.get(_ENV_DATA_ROOT, _DEFAULT_DATA_ROOT))
            / "investigations"
            / run_id
        )

    async def _network(self, run: dict[str, Any]) -> None:
        await self._clients()
        agent = investigation_manifest.agent_manifest(run)
        test = investigation_manifest.test_manifest({**run, "test_command": "true"}, 1)
        result = await self.networking.list_namespaced_network_policy(self.namespace)
        policies = [
            self.networking.api_client.sanitize_for_serialization(item)
            if not isinstance(item, dict) and hasattr(self.networking, "api_client")
            else item
            for item in _items(result)
        ]
        reason = investigation_manifest.network_check(
            policies,
            agent["spec"]["template"]["metadata"]["labels"],
            test["spec"]["template"]["metadata"]["labels"],
        )
        if reason:
            raise InvestigationRefused(f"network policy: {reason}")

    async def dispatch(  # noqa: PLR0913, PLR0915, PLR0912 - dispatch coordinates and lifecycle stay together
        self,
        *,
        project_id: str,
        repo_path: str | Path,
        repo: str,
        sha: str,
        request: str,
        claude_token: str,
        test_command: str | None = None,
        limits: dict[str, int] | None = None,
        extra_patterns: str = "",
    ) -> dict[str, str]:
        """Reserve a slot, export source, then create Job and owned Secret."""
        if os.environ.get("AIFACTORY_INVESTIGATIONS", "off") == "drain":
            raise InvestigationRefused("investigations are draining")
        if len(request) > _MAX_REQUEST or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise InvestigationRefused("invalid request or sha")
        if not claude_token:
            raise InvestigationRefused("Claude credential unavailable")
        chosen = {**_LIMITS, **(limits or {})}
        if any(
            not isinstance(chosen[k], int) or not 0 < chosen[k] <= maximum
            for k, maximum in _LIMITS.items()
        ):
            raise InvestigationRefused("invalid limits")
        run_id = str(uuid4())
        run_token = secrets.token_urlsafe(32)
        now = self.clock()
        fields: dict[str, Any] = {
            "id": run_id,
            "created_at": now,
            "project_id": project_id,
            "repo": repo,
            "sha": sha,
            "request": request,
            "test_command": test_command,
            "limits": chosen,
            "state": "queued",
            "lease_expires_at": now + timedelta(seconds=60),
            "token_hash": hashlib.sha256(run_token.encode()).hexdigest(),
        }
        try:
            await self._network(fields)
        except ValueError as exc:
            _log.warning("investigation manifest refused: %s", exc)
            raise InvestigationRefused("manifest refused") from exc
        try:
            await self.store.insert(**fields)
        except SlotTaken as exc:
            raise InvestigationRefused("project investigation slot taken") from exc
        self._tokens[run_id] = (run_token, claude_token)
        self._dispatching.add(run_id)
        try:
            try:
                await asyncio.to_thread(
                    investigation_export.export,
                    repo_path,
                    sha,
                    self._root(run_id) / "src",
                    extra_patterns,
                )
            except Exception as exc:
                if self._root(run_id).exists():
                    await asyncio.to_thread(shutil.rmtree, self._root(run_id))
                await self.store.update(
                    run_id,
                    state="failed",
                    reason="export",
                    pods_gone=True,
                    ended_at=self.clock(),
                )
                self._tokens.pop(run_id, None)
                self.release_credential(run_id)
                raise InvestigationRefused("export failed") from exc
            try:
                async with self.store.lock(run_id) as (session, row):
                    if row.state != "queued":
                        await asyncio.to_thread(
                            shutil.rmtree, self._root(run_id), ignore_errors=True
                        )
                        raise InvestigationRefused("run no longer queued")
                    name = investigation_manifest.agent_manifest(row)["metadata"][
                        "name"
                    ]
                    await self.store.update(run_id, session=session, agent_job=name)
                    row.agent_job = name
                    job = await asyncio.wait_for(
                        self.batch.create_namespaced_job(
                            self.namespace, investigation_manifest.agent_manifest(row)
                        ),
                        timeout=10,
                    )
                    uid = _field(_field(job, "metadata"), "uid")
                    if not uid:
                        job = await self.batch.read_namespaced_job(name, self.namespace)
                        uid = _field(_field(job, "metadata"), "uid")
                    if not uid:
                        raise InvestigationRefused("Job UID unavailable")
                    await self._create_secret(row, str(uid))
                    await self.store.update(
                        run_id,
                        session=session,
                        state="running",
                        lease_expires_at=self.clock() + timedelta(seconds=60),
                    )
            except Exception as exc:
                await self._terminate(run_id, "failed", "dispatch")
                raise InvestigationRefused("dispatch failed") from exc
            return {"id": run_id, "state": "running"}
        finally:
            self._dispatching.discard(run_id)

    async def _create_secret(self, row: Any, uid: str) -> None:
        tokens = self._tokens.get(row.id)
        if not tokens:
            raise InvestigationRefused("run token unavailable after restart")
        manifest = investigation_manifest.secret_manifest(
            {"id": row.id, "run_token": tokens[0], "claude_token": tokens[1]}, uid
        )
        try:
            await asyncio.wait_for(
                self.core.create_namespaced_secret(self.namespace, manifest), timeout=10
            )
        except Exception as exc:
            if _code(exc) != _CONFLICT:
                raise

    async def _claude_token(self, run_id: str) -> str:
        cached = self._tokens.get(run_id)
        if cached:
            return cached[1]
        await self._clients()
        name = investigation_manifest._names({"id": run_id})[0]
        try:
            secret = await self.core.read_namespaced_secret(name, self.namespace)
        except Exception as exc:
            raise InvestigationRefused("scrubbing credential unavailable") from exc
        literal = _field(
            _field(secret, "string_data", _field(secret, "stringData", {})),
            "CLAUDE_CODE_OAUTH_TOKEN",
        )
        encoded = _field(_field(secret, "data", {}), "CLAUDE_CODE_OAUTH_TOKEN")
        token = literal or (base64.b64decode(encoded).decode() if encoded else "")
        if not token:
            raise InvestigationRefused("scrubbing credential unavailable")
        return token

    async def renew_lease(self, run_id: str) -> None:
        async with self.store.lock(run_id) as (session, row):
            if (
                row is None
                or row.pods_gone
                or row.state not in {"queued", "running", "completing"}
            ):
                raise InvestigationRefused("run is not live")
            await self.store.update(
                run_id,
                session=session,
                lease_expires_at=self.clock() + timedelta(seconds=60),
            )

    async def get(self, run_id: str) -> dict[str, Any]:
        row = await self.store.get(run_id)
        if row is None:
            raise InvestigationRefused("run not found")
        return {
            "id": row.id,
            "state": row.state,
            "reason": row.reason,
            "pods_gone": row.pods_gone,
            "tests": row.tests,
            "report": row.report,
            "usage": {
                "tokens": row.tokens_used,
                "turns": row.turns_used,
                "run_tests": row.run_tests_used,
            },
        }

    async def logs(
        self, run_id: str, after: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        if await self.store.get(run_id) is None:
            raise InvestigationRefused("run not found")
        return await self.store.read_logs(run_id, after, limit)

    async def lease_status(self, run_id: str) -> dict[str, Any]:
        row = await self.store.get(run_id)
        return {
            "ok": bool(row and not row.pods_gone and row.state == "running"),
            "state": row.state if row else None,
        }

    async def config(self, run_id: str) -> dict[str, Any]:
        row = await self.store.get(run_id)
        if row is None or row.pods_gone:
            raise InvestigationRefused("run not live")
        return {
            "request": row.request,
            "sha": row.sha,
            "limits": row.limits,
            "has_tests": bool(row.test_command),
        }

    async def append_log(
        self,
        run_id: str,
        lines: list[str],
        *,
        tokens_used: int = 0,
        turns_used: int = 0,
    ) -> None:
        if len(lines) > _MAX_BATCH:
            raise InvestigationRefused("too many log lines")
        async with self.store.lock(run_id) as (session, row):
            if row is None or row.pods_gone:
                raise InvestigationRefused("run not live")
            count = await session.scalar(
                select(func.count())
                .select_from(InvestigationLog)
                .where(InvestigationLog.run_id == run_id)
            )
            clean, pem_open = investigation_scrub.scrub_lines(
                lines,
                claude_token=await self._claude_token(run_id),
                pem_open=row.scrub_open,
            )
            if count >= _MAX_LOGS + 1:
                clean = []
            elif count + len(clean) > _MAX_LOGS:
                clean = [*clean[: max(0, _MAX_LOGS - count)], "log truncated"]
            await self.store.update(
                run_id,
                session=session,
                scrub_open=pem_open,
                tokens_used=max(row.tokens_used, tokens_used),
                turns_used=max(row.turns_used, turns_used),
            )
            if clean:
                session.add_all(
                    InvestigationLog(run_id=run_id, at=self.clock(), line=line[:2000])
                    for line in clean
                )

    async def put_report(self, run_id: str, findings: str) -> None:
        if len(findings) > _MAX_LOGS:
            raise InvestigationRefused("report too long")
        async with self.store.lock(run_id) as (session, row):
            if row is None or row.pods_gone or row.report is not None:
                raise InvestigationRefused("report unavailable")
            clean = investigation_scrub.scrub_text(
                findings, claude_token=await self._claude_token(run_id)
            )
            root = self._root(run_id) / "src"
            refs = [
                f"{path}:{line}"
                for path, line in _REF.findall(clean)
                if (root / path).resolve().is_relative_to(root.resolve())
                and (root / path).is_file()
            ]
            await self.store.update(
                run_id,
                session=session,
                report={
                    "findings": clean,
                    "refs": refs,
                    "tests": row.tests,
                    "sha": row.sha,
                },
            )

    async def _delete_jobs(self, run_id: str, row: Any) -> None:
        await self._clients()
        names = {row.agent_job, *(row.test_jobs or [])}
        listed = await self.batch.list_namespaced_job(
            self.namespace, label_selector=f"factory.io/investigation={run_id}"
        )
        names.update(_field(_field(job, "metadata"), "name") for job in _items(listed))
        for name in names - {None}:
            try:
                await asyncio.wait_for(
                    self.batch.delete_namespaced_job(
                        name, self.namespace, propagation_policy="Foreground"
                    ),
                    timeout=10,
                )
            except Exception as exc:
                if _code(exc) != _NOT_FOUND:
                    raise

    async def _terminate(self, run_id: str, terminal: str, reason: str) -> None:
        async with self.store.lock(run_id) as (session, row):
            if row is None or row.pods_gone:
                return
            if reason == "lease expired" and row.lease_expires_at > self.clock():
                return
            if row.state not in {"stopping", "uncertain"}:
                await self.store.update(
                    run_id,
                    session=session,
                    state="stopping",
                    pending_state=terminal,
                    reason=reason,
                    stopping_since=self.clock(),
                )
            try:
                await self._delete_jobs(run_id, row)
            except Exception:  # noqa: BLE001 - next reconcile tick retries deletion
                _log.exception("investigation Job deletion failed for %s", run_id)

    async def stop(self, run_id: str) -> dict[str, Any]:
        await self._terminate(run_id, "stopped", "stopped by caller")
        for _ in range(10):
            await self._pods_gone(run_id)
            row = await self.store.get(run_id)
            if row is None:
                raise InvestigationRefused("run not found")
            if row.pods_gone:
                break
            await asyncio.sleep(2)
        return (
            {"state": row.state, "pods_gone": row.pods_gone}
            if row
            else {"state": "failed", "pods_gone": False}
        )

    async def _pods_gone(self, run_id: str) -> None:
        async with self.store.lock(run_id) as (session, row):
            if (
                row is None
                or row.pods_gone
                or row.state not in {"stopping", "uncertain"}
            ):
                return
            await self._clients()
            selector = f"factory.io/investigation={run_id}"
            jobs = await self.batch.list_namespaced_job(
                self.namespace, label_selector=selector
            )
            pods = await self.core.list_namespaced_pod(
                self.namespace, label_selector=selector
            )
            if _items(jobs) or _items(pods):
                try:
                    await self._delete_jobs(run_id, row)
                except Exception:  # noqa: BLE001 - keep slot held and retry next tick
                    _log.exception("investigation Job deletion failed for %s", run_id)
                if (
                    row.stopping_since
                    and self.clock() - row.stopping_since >= timedelta(seconds=120)
                ):
                    await self.store.update(run_id, session=session, state="uncertain")
                return
            name, _ = investigation_manifest._names(row)
            try:
                await self.core.delete_namespaced_secret(name, self.namespace)
            except Exception as exc:
                if _code(exc) != _NOT_FOUND:
                    raise
            if self._root(run_id).exists():
                await asyncio.to_thread(shutil.rmtree, self._root(run_id))
            self.release_credential(run_id)
            self._tokens.pop(run_id, None)
            await self.store.update(
                run_id,
                session=session,
                state=row.pending_state or "failed",
                pods_gone=True,
                ended_at=self.clock(),
            )

    async def run_tests(self, run_id: str) -> dict[str, Any]:
        async with self.store.lock(run_id) as (session, row):
            if row is None or row.state != "running" or row.pods_gone:
                raise InvestigationRefused("run not running")
            if not row.test_command:
                return {"ran": False, "reason": "no tests run: none configured"}
            if row.run_tests_used >= row.limits["test_calls"]:
                raise InvestigationRefused("test call limit reached")
            if await self._live_test(row):
                raise InvestigationRefused("test already running")
            n = row.run_tests_used + 1
            manifest = investigation_manifest.test_manifest(row, n)
            name = manifest["metadata"]["name"]
            await self.store.update(
                run_id,
                session=session,
                run_tests_used=n,
                test_jobs=[*(row.test_jobs or []), name],
            )
            await asyncio.wait_for(
                self.batch.create_namespaced_job(self.namespace, manifest), timeout=10
            )
        return await self._collect_test(
            run_id, name, manifest["spec"]["activeDeadlineSeconds"]
        )

    async def _live_test(self, row: Any) -> bool:
        await self._clients()
        listed = await self.batch.list_namespaced_job(
            self.namespace, label_selector=f"factory.io/investigation={row.id}"
        )
        names = set(row.test_jobs or [])
        names.update(
            _field(_field(job, "metadata"), "name")
            for job in _items(listed)
            if "-test-" in (_field(_field(job, "metadata"), "name") or "")
        )
        for name in names:
            try:
                job = await self.batch.read_namespaced_job(name, self.namespace)
            except Exception as exc:
                if _code(exc) == _NOT_FOUND:
                    continue
                raise
            status = _field(job, "status")
            if not _field(status, "succeeded", 0) and not _field(status, "failed", 0):
                return True
        return False

    async def _collect_test(  # noqa: PLR0912 - polling and cleanup need distinct failure paths
        self, run_id: str, name: str, deadline: int
    ) -> dict[str, Any]:
        stop_at = monotonic() + deadline + 15
        completed = False
        while (remaining := stop_at - monotonic()) > 0:
            try:
                job = await asyncio.wait_for(
                    self.batch.read_namespaced_job(name, self.namespace),
                    timeout=remaining,
                )
            except TimeoutError:
                break
            except Exception as exc:
                if _code(exc) == _NOT_FOUND:
                    raise InvestigationRefused("test job deleted") from exc
                raise
            status = _field(job, "status")
            if _field(status, "succeeded", 0) or _field(status, "failed", 0):
                completed = True
                break
            await asyncio.sleep(min(1, max(0, stop_at - monotonic())))
        if not completed:
            try:
                await self.batch.delete_namespaced_job(
                    name, self.namespace, propagation_policy="Foreground"
                )
            except Exception as exc:
                if _code(exc) != _NOT_FOUND:
                    raise
            raise InvestigationRefused("test collection timed out")
        pods = await self.core.list_namespaced_pod(
            self.namespace, label_selector=f"job-name={name}"
        )
        pod = next(iter(_items(pods)), None)
        pod_name = _field(_field(pod, "metadata"), "name") if pod else None
        data = b""
        exit_code = 0 if _field(status, "succeeded", 0) else 1
        if pod_name:
            resp = await self.core.read_namespaced_pod_log(
                pod_name,
                self.namespace,
                container="test",
                limit_bytes=5 * 1024 * 1024 + 1,
                _preload_content=False,
            )
            data = await resp.read()
            for container in _field(_field(pod, "status"), "container_statuses", []):
                terminated = _field(_field(container, "state"), "terminated")
                if terminated is not None:
                    exit_code = _field(terminated, "exit_code", exit_code)
        truncated = len(data) > 5 * 1024 * 1024
        raw = data[: 5 * 1024 * 1024].decode(errors="replace")
        parsed = investigation_tests.parse(raw, truncated)
        clean, _ = investigation_scrub.scrub_lines(
            raw.splitlines(keepends=True),
            claude_token=await self._claude_token(run_id),
            pem_open=False,
        )
        tail = "".join(clean[-200:])
        result = {
            "ran": True,
            "exit_code": exit_code,
            "truncated": truncated,
            "counts": parsed,
            "tail": tail,
        }
        async with self.store.lock(run_id) as (session, row):
            previous = row.tests or []
            await self.store.update(
                run_id,
                session=session,
                tests=[
                    *(previous if isinstance(previous, list) else [previous]),
                    result,
                ],
            )
        try:
            await self.batch.delete_namespaced_job(
                name, self.namespace, propagation_policy="Foreground"
            )
        except Exception as exc:
            if _code(exc) != _NOT_FOUND:
                raise
        return result

    async def reconcile_tick(self) -> None:
        await self._clients()
        now = self.clock()
        if self._last_purge is None or now - self._last_purge >= timedelta(hours=1):
            await self.store.purge_logs(now - timedelta(days=7))
            self._last_purge = now
        for run in await self.store.live_runs(50):
            try:
                await self._reconcile_one(run.id)
            except Exception:  # noqa: BLE001 - one run cannot stop the tick
                _log.exception("investigation reconcile failed for %s", run.id)

    async def _reconcile_one(self, run_id: str) -> None:  # noqa: PLR0911, PLR0912, PLR0915 - state machine
        row = await self.store.get(run_id)
        if row is None or row.pods_gone:
            return
        if run_id in self._dispatching:
            return
        if row.state in {"stopping", "uncertain"}:
            await self._pods_gone(run_id)
            return
        if row.lease_expires_at <= self.clock():
            await self._terminate(run_id, "expired", "lease expired")
            return
        if row.created_at + timedelta(minutes=row.limits["minutes"]) <= self.clock():
            await self._terminate(run_id, "failed", "deadline")
            return
        async with self.store.lock(run_id) as (session, row):
            if row.state not in {"queued", "running", "completing"}:
                return
            if not row.agent_job:
                return
            try:
                job = await self.batch.read_namespaced_job(
                    row.agent_job, self.namespace
                )
            except Exception as exc:
                if _code(exc) != _NOT_FOUND:
                    raise
                job = None
            if job is None:
                if row.state != "queued":
                    await self.store.update(
                        run_id,
                        session=session,
                        state="stopping",
                        pending_state="failed",
                        reason="dispatch",
                        stopping_since=self.clock(),
                    )
                    await self._delete_jobs(run_id, row)
                    return
                if run_id not in self._tokens:
                    await self.store.update(
                        run_id,
                        session=session,
                        state="stopping",
                        pending_state="failed",
                        reason="dispatch",
                        stopping_since=self.clock(),
                    )
                    await self._delete_jobs(run_id, row)
                    return
                try:
                    job = await asyncio.wait_for(
                        self.batch.create_namespaced_job(
                            self.namespace, investigation_manifest.agent_manifest(row)
                        ),
                        timeout=10,
                    )
                except Exception as exc:
                    if _code(exc) != _CONFLICT:
                        raise
                    job = await self.batch.read_namespaced_job(
                        row.agent_job, self.namespace
                    )
            secret_name, _ = investigation_manifest._names(row)
            try:
                await self.core.read_namespaced_secret(secret_name, self.namespace)
            except Exception as exc:
                if _code(exc) != _NOT_FOUND:
                    raise
                uid = _field(_field(job, "metadata"), "uid")
                try:
                    await self._create_secret(row, str(uid))
                except InvestigationRefused:
                    await self.store.update(
                        run_id,
                        session=session,
                        state="stopping",
                        pending_state="failed",
                        reason="dispatch",
                        stopping_since=self.clock(),
                    )
                    await self._delete_jobs(run_id, row)
                    return
            status = _field(job, "status")
            if _field(status, "succeeded", 0):
                await self.store.update(run_id, session=session, state="completing")
                if row.report is None:
                    await self.store.update(
                        run_id,
                        session=session,
                        report={
                            "findings": None,
                            "refs": [],
                            "tests": row.tests,
                            "sha": row.sha,
                        },
                    )
                terminal = "completed"
            elif _field(status, "failed", 0):
                terminal = "failed"
            else:
                await self.store.update(run_id, session=session, state="running")
                return
        await self._terminate(run_id, terminal, "agent job finished")

    def start_loop(self) -> asyncio.Task[None]:
        if self._loop and not self._loop.done():
            return self._loop

        async def run() -> None:
            backoff = 10
            while True:
                try:
                    await self.reconcile_tick()
                    backoff = 10
                except Exception:  # noqa: BLE001 - supervised loop retries any failure
                    _log.exception("investigation reconcile tick failed")
                    backoff = min(backoff * 2, 60)
                await asyncio.sleep(backoff)

        self._loop = asyncio.create_task(run(), name="investigation-reconcile")
        return self._loop
