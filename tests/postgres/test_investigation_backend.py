"""Step 5: investigation lifecycle against real Postgres and fake Kubernetes."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.postgres.helpers import run_alembic

WEB_SERVER_ROOT = Path(__file__).resolve().parents[2] / "apps" / "web-server"
if str(WEB_SERVER_ROOT) not in sys.path:
    sys.path.append(str(WEB_SERVER_ROOT))

from server.services import (  # noqa: E402
    investigation_backend,
    investigation_export,
    investigation_manifest,
)
from server.services.investigation_store import InvestigationStore, _now  # noqa: E402

pytestmark = [pytest.mark.postgres, pytest.mark.slow]


class KubeError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


class FakeKube:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.secrets: dict[str, dict[str, Any]] = {}
        self.pods: list[dict[str, Any]] = []
        self.events: list[str] = []
        self.manifests: list[dict[str, Any]] = []
        self.auto_test_complete = False
        self.log_bytes = b"=== 1 passed in 0.1s ==="
        self.before_create: asyncio.Event | None = None
        self.resume_create: asyncio.Event | None = None

    async def create_namespaced_job(
        self, _namespace: str, manifest: dict[str, Any]
    ) -> Any:
        name = manifest["metadata"]["name"]
        self.manifests.append(manifest)
        self.events.append(f"create:{name}")
        if self.before_create is not None:
            self.before_create.set()
            assert self.resume_create is not None
            await self.resume_create.wait()
        if name in self.jobs:
            raise KubeError(409)
        status = (
            {"succeeded": 1} if self.auto_test_complete and "-test-" in name else {}
        )
        self.jobs[name] = {
            "metadata": {
                "name": name,
                "uid": "uid-1",
                "labels": manifest["metadata"]["labels"],
            },
            "status": status,
        }
        return self.jobs[name]

    async def read_namespaced_job(self, name: str, _namespace: str) -> Any:
        if name not in self.jobs:
            raise KubeError(404)
        return self.jobs[name]

    async def list_namespaced_job(self, _namespace: str, label_selector: str) -> Any:
        key, value = label_selector.split("=", 1)
        return {
            "items": [
                job
                for job in self.jobs.values()
                if job["metadata"].get("labels", {}).get(key) == value
            ]
        }

    async def delete_namespaced_job(
        self, name: str, _namespace: str, **_kwargs: Any
    ) -> Any:
        self.events.append(f"delete:{name}")
        self.jobs.pop(name, None)

    async def create_namespaced_secret(
        self, _namespace: str, manifest: dict[str, Any]
    ) -> Any:
        name = manifest["metadata"]["name"]
        self.events.append(f"secret:{name}")
        self.secrets[name] = manifest

    async def read_namespaced_secret(self, name: str, _namespace: str) -> Any:
        if name not in self.secrets:
            raise KubeError(404)
        return self.secrets[name]

    async def delete_namespaced_secret(self, name: str, _namespace: str) -> Any:
        self.secrets.pop(name, None)

    async def list_namespaced_pod(self, _namespace: str, label_selector: str) -> Any:
        key, value = label_selector.split("=", 1)
        return {
            "items": [
                pod
                for pod in self.pods
                if pod["metadata"].get("labels", {}).get(key) == value
            ]
        }

    async def read_namespaced_pod_log(
        self, _name: str, _namespace: str, **kwargs: Any
    ) -> Any:
        self.log_kwargs = kwargs
        if kwargs.get("_preload_content", True):
            return self.log_bytes.decode("utf-8")

        async def read() -> bytes:
            return self.log_bytes

        return SimpleNamespace(read=read)

    async def list_namespaced_network_policy(self, _namespace: str) -> Any:
        return {"items": []}


async def test_fake_kube_selects_jobs_and_pods_by_label() -> None:
    kube = FakeKube()
    kube.jobs = {
        "aif-inv-12345678-wrong": {
            "metadata": {
                "name": "wrong",
                "labels": {"factory.io/investigation": "other"},
            }
        },
        "unrelated-name": {
            "metadata": {
                "name": "right",
                "labels": {"factory.io/investigation": "12345678"},
            }
        },
    }
    kube.pods = [
        {
            "metadata": {
                "name": "wrong",
                "labels": {"factory.io/investigation": "other"},
            }
        },
        {
            "metadata": {
                "name": "right",
                "labels": {"factory.io/investigation": "12345678"},
            }
        },
    ]
    selector = "factory.io/investigation=12345678"
    jobs = await kube.list_namespaced_job("factory", selector)
    pods = await kube.list_namespaced_pod("factory", selector)
    assert [job["metadata"]["name"] for job in jobs["items"]] == ["right"]
    assert [pod["metadata"]["name"] for pod in pods["items"]] == ["right"]


@pytest.fixture
async def service(
    test_postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[SimpleNamespace]:
    result = run_alembic(["upgrade", "head"], env={"DATABASE_URL": test_postgres_url})
    assert result.returncode == 0, result.stderr[-1000:]
    engine = create_async_engine(test_postgres_url)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM investigation_logs"))
        await conn.execute(text("DELETE FROM investigations"))
    kube = FakeKube()
    store = InvestigationStore(async_sessionmaker(engine, expire_on_commit=False))
    now = [_now()]
    released: list[str] = []
    backend = investigation_backend.InvestigationBackend(
        store,
        batch=kube,
        core=kube,
        networking=kube,
        clock=lambda: now[0],
        release_credential=released.append,
    )
    monkeypatch.setattr(backend, "_network", AsyncMock())
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "data")
    monkeypatch.setattr(os.path, "ismount", lambda _path: True)
    monkeypatch.setattr(
        investigation_export,
        "export",
        lambda *_args: {"bytes": 0},
    )
    yield SimpleNamespace(
        backend=backend, kube=kube, store=store, now=now, released=released
    )
    await engine.dispose()


def _args(**overrides: Any) -> dict[str, Any]:
    return {
        "project_id": "p1",
        "repo_path": Path(__file__).parent,
        "repo": "o/r",
        "sha": "a" * 40,
        "request": "investigate",
        "claude_token": "secret-token",
        **overrides,
    }


async def test_dispatch_limits_slot_and_job_secret_order(
    service: SimpleNamespace,
) -> None:
    backend, kube = service.backend, service.kube
    for value in (0, -1, 31):
        with pytest.raises(investigation_backend.InvestigationRefused, match="limits"):
            await backend.dispatch(**_args(limits={"minutes": value}))
    result = await backend.dispatch(**_args())
    row = await service.store.get(result["id"])
    assert row.state == "running"
    assert row.agent_job in kube.jobs
    assert kube.events == [
        f"create:{row.agent_job}",
        f"secret:{row.agent_job.removesuffix('-agent')}",
    ]
    with pytest.raises(investigation_backend.InvestigationRefused, match="slot"):
        await backend.dispatch(**_args())


async def test_stop_keeps_slot_until_pods_gone_and_releases_credential(
    service: SimpleNamespace,
) -> None:
    backend, kube = service.backend, service.kube
    run_id = (await backend.dispatch(**_args()))["id"]
    kube.pods.append(
        {
            "metadata": {
                "name": "still-here",
                "labels": {"factory.io/investigation": run_id},
            }
        }
    )
    await backend._terminate(run_id, "stopped", "caller")
    await backend._pods_gone(run_id)
    row = await service.store.get(run_id)
    assert row.state == "stopping" and not row.pods_gone
    service.now[0] += timedelta(seconds=121)
    await backend._pods_gone(run_id)
    assert (await service.store.get(run_id)).state == "uncertain"
    kube.pods.clear()
    await backend._pods_gone(run_id)
    row = await service.store.get(run_id)
    assert row.state == "stopped" and row.pods_gone
    assert service.released == [run_id]


async def test_lease_expiry_and_reconcile_after_restart(
    service: SimpleNamespace,
) -> None:
    backend = service.backend
    run_id = (await backend.dispatch(**_args()))["id"]
    service.now[0] += timedelta(seconds=61)
    await backend.reconcile_tick()
    assert (await service.store.get(run_id)).state == "stopping"
    await backend.reconcile_tick()
    assert (await service.store.get(run_id)).state == "expired"


async def test_scrubbed_logs_and_report_persist(service: SimpleNamespace) -> None:
    backend = service.backend
    run_id = (await backend.dispatch(**_args()))["id"]
    await backend.append_log(run_id, ["secret-token", "-----BEGIN PRIVATE KEY-----"])
    await backend.append_log(
        run_id, ["private material", "-----END PRIVATE KEY-----", "safe"]
    )
    lines = [entry["line"] for entry in (await backend.logs(run_id))["entries"]]
    assert lines == ["[REDACTED]", "safe"]
    root = backend._root(run_id) / "src"
    root.mkdir(parents=True)
    (root / "file.py").write_text("x")
    await backend.put_report(run_id, "file.py:1 secret-token")
    row = await service.store.get(run_id)
    assert row.report["refs"] == ["file.py:1"]
    assert "secret-token" not in row.report["findings"]


async def test_test_admission_and_no_command(service: SimpleNamespace) -> None:
    backend = service.backend
    run_id = (await backend.dispatch(**_args()))["id"]
    assert await backend.run_tests(run_id) == {
        "ran": False,
        "reason": "no tests run: none configured",
    }
    assert (await service.store.get(run_id)).run_tests_used == 0


async def test_barrier_race_stop_waits_for_create(service: SimpleNamespace) -> None:
    backend, kube = service.backend, service.kube
    kube.before_create = asyncio.Event()
    kube.resume_create = asyncio.Event()
    dispatch = asyncio.create_task(backend.dispatch(**_args()))
    await kube.before_create.wait()
    rows = await service.store.live_runs()
    stop = asyncio.create_task(backend._terminate(rows[0].id, "stopped", "caller"))
    await asyncio.sleep(0.05)
    assert not stop.done()
    kube.resume_create.set()
    await dispatch
    await stop
    assert not kube.jobs
    assert not (await service.store.get(rows[0].id)).pods_gone
    await backend._pods_gone(rows[0].id)
    assert (await service.store.get(rows[0].id)).pods_gone


async def test_ten_test_calls_then_refused_and_scrubbed_tail(
    service: SimpleNamespace,
) -> None:
    backend, kube = service.backend, service.kube
    kube.auto_test_complete = True
    run_id = (await backend.dispatch(**_args(test_command="pytest")))["id"]
    base = investigation_manifest._names({"id": run_id})[0]
    kube.pods = [
        {
            "metadata": {"name": "test-pod", "labels": {}},
            "status": {
                "container_statuses": [{"state": {"terminated": {"exit_code": 0}}}]
            },
        }
    ]
    kube.log_bytes = b"=== 1 passed in 0.1s ===\nsecret-token"
    for n in range(1, 11):
        kube.pods[0]["metadata"]["labels"]["job-name"] = f"{base}-test-{n}"
        result = await backend.run_tests(run_id)
        assert result["counts"] == {"passed": 1, "failed": 0, "skipped": 0}
        assert "secret-token" not in result["tail"]
    assert (await service.store.get(run_id)).run_tests_used == 10
    assert len((await service.store.get(run_id)).tests) == 10
    with pytest.raises(investigation_backend.InvestigationRefused, match="limit"):
        await backend.run_tests(run_id)


async def test_test_log_cut_mid_utf8_is_replaced(service: SimpleNamespace) -> None:
    backend, kube = service.backend, service.kube
    kube.auto_test_complete = True
    run_id = (await backend.dispatch(**_args(test_command="pytest")))["id"]
    base = investigation_manifest._names({"id": run_id})[0]
    kube.pods = [
        {
            "metadata": {"name": "test-pod", "labels": {"job-name": f"{base}-test-1"}},
            "status": {},
        }
    ]
    kube.log_bytes = b"=== 1 passed in 0.1s ===\n" + b"\xe2\x82"
    result = await backend.run_tests(run_id)
    assert result["counts"]["passed"] == 1
    assert "�" in result["tail"]


async def test_collect_waits_for_job_deadline(
    service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, kube = service.backend, service.kube
    run_id = (await backend.dispatch(**_args(test_command="pytest")))["id"]
    original_read = kube.read_namespaced_job
    reads = 0
    elapsed = [0.0]

    async def finish_on_920th_read(name: str, namespace: str) -> Any:
        nonlocal reads
        job = await original_read(name, namespace)
        if "-test-" in name:
            reads += 1
            elapsed[0] += 1
            if reads == 920:
                job["status"] = {"succeeded": 1}
        return job

    monkeypatch.setattr(kube, "read_namespaced_job", finish_on_920th_read)
    monkeypatch.setattr(
        investigation_backend, "monotonic", lambda: elapsed[0], raising=False
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    assert (await backend.run_tests(run_id))["ran"] is True
    assert reads == 920


async def test_collect_job_deleted_by_stop_is_refused(
    service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, kube = service.backend, service.kube
    run_id = (await backend.dispatch(**_args(test_command="pytest")))["id"]

    async def delete_job(_seconds: int) -> None:
        for name in list(kube.jobs):
            if "-test-" in name:
                kube.jobs.pop(name)

    monkeypatch.setattr(asyncio, "sleep", delete_job)
    with pytest.raises(investigation_backend.InvestigationRefused, match="deleted"):
        await backend.run_tests(run_id)


async def test_collect_timeout_deletes_job(
    service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, kube = service.backend, service.kube
    run_id = (await backend.dispatch(**_args(test_command="pytest")))["id"]
    elapsed = [0.0]

    async def advance(_seconds: float) -> None:
        elapsed[0] += 1

    monkeypatch.setattr(
        investigation_backend, "monotonic", lambda: elapsed[0], raising=False
    )
    monkeypatch.setattr(asyncio, "sleep", advance)
    with pytest.raises(investigation_backend.InvestigationRefused, match="timed out"):
        await backend.run_tests(run_id)
    assert not any("-test-" in name for name in kube.jobs)


async def test_collect_slow_reads_respect_wall_clock(
    service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, kube = service.backend, service.kube
    run_id = (await backend.dispatch(**_args(test_command="pytest")))["id"]
    elapsed = [0.0]
    reads = 0
    original_read = kube.read_namespaced_job

    async def slow_read(name: str, namespace: str) -> Any:
        nonlocal reads
        job = await original_read(name, namespace)
        if "-test-" in name:
            reads += 1
            elapsed[0] += 20
        return job

    monkeypatch.setattr(kube, "read_namespaced_job", slow_read)
    monkeypatch.setattr(
        investigation_backend, "monotonic", lambda: elapsed[0], raising=False
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    with pytest.raises(investigation_backend.InvestigationRefused, match="timed out"):
        await backend.run_tests(run_id)
    assert reads < 50


async def test_collect_appends_to_legacy_dict_tests(service: SimpleNamespace) -> None:
    backend, kube = service.backend, service.kube
    kube.auto_test_complete = True
    run_id = (await backend.dispatch(**_args(test_command="pytest")))["id"]
    previous = {"counts": {"passed": 2, "failed": 1}}
    await service.store.update(run_id, tests=previous)
    result = await backend.run_tests(run_id)
    tests = (await service.store.get(run_id)).tests
    assert tests == [previous, result]


async def test_collect_ignores_job_deleted_before_final_cleanup(
    service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, kube = service.backend, service.kube
    kube.auto_test_complete = True
    run_id = (await backend.dispatch(**_args(test_command="pytest")))["id"]

    async def already_deleted(_name: str, _namespace: str, **_kwargs: Any) -> None:
        raise KubeError(404)

    monkeypatch.setattr(kube, "delete_namespaced_job", already_deleted)
    assert (await backend.run_tests(run_id))["ran"] is True


async def test_missing_secret_after_restart_fails_closed(
    service: SimpleNamespace,
) -> None:
    backend, kube = service.backend, service.kube
    run_id = (await backend.dispatch(**_args()))["id"]
    kube.secrets.clear()
    backend._tokens.clear()
    await backend.reconcile_tick()
    assert (await service.store.get(run_id)).state == "stopping"


async def test_reconcile_queued_without_recorded_name(service: SimpleNamespace) -> None:
    backend, kube = service.backend, service.kube
    run_id = str(__import__("uuid").uuid4())
    token = run_id
    await service.store.insert(
        id=run_id,
        project_id="p1",
        repo="o/r",
        sha="a" * 40,
        request="investigate",
        limits={"minutes": 30, "tokens": 100, "turns": 10, "test_calls": 1},
        state="queued",
        lease_expires_at=service.now[0] + timedelta(seconds=60),
        token_hash=__import__("hashlib").sha256(token.encode()).hexdigest(),
    )
    backend._tokens[run_id] = (token, "secret-token")
    await backend.reconcile_tick()
    row = await service.store.get(run_id)
    assert row.state == "queued"
    assert row.agent_job is None
    assert not kube.jobs


async def test_export_barrier_reconcile_does_not_preempt_dispatch(
    service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, kube = service.backend, service.kube
    entered, release = threading.Event(), threading.Event()

    def blocked_export(*_args: Any) -> dict[str, int]:
        entered.set()
        assert release.wait(5)
        return {"bytes": 0}

    monkeypatch.setattr(investigation_export, "export", blocked_export)
    dispatch = asyncio.create_task(backend.dispatch(**_args()))
    assert await asyncio.to_thread(entered.wait, 5)
    try:
        service.now[0] += timedelta(seconds=120)
        await backend.reconcile_tick()
        assert not kube.jobs
    finally:
        release.set()
    run_id = (await dispatch)["id"]
    row = await service.store.get(run_id)
    assert row.state == "running"
    assert row.lease_expires_at == service.now[0] + timedelta(seconds=60)
    assert len(kube.jobs) == 1


async def test_stop_during_export_removes_tree(
    service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = service.backend
    entered, release = threading.Event(), threading.Event()

    def blocked_export(
        _repo: Any, _sha: Any, dest: Path, *_args: Any
    ) -> dict[str, int]:
        entered.set()
        assert release.wait(5)
        dest.mkdir(parents=True)
        (dest / "file.py").write_text("x")
        return {"bytes": 1}

    monkeypatch.setattr(investigation_export, "export", blocked_export)
    dispatch = asyncio.create_task(backend.dispatch(**_args()))
    assert await asyncio.to_thread(entered.wait, 5)
    run_id = (await service.store.live_runs())[0].id
    try:
        await backend.stop(run_id)
    finally:
        release.set()
    with pytest.raises(investigation_backend.InvestigationRefused):
        await dispatch
    assert not backend._root(run_id).exists()


async def test_real_network_preflight_and_created_at(
    service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, kube = service.backend, service.kube
    monkeypatch.setattr(
        backend,
        "_network",
        investigation_backend.InvestigationBackend._network.__get__(backend),
    )

    async def policies(_namespace: str) -> Any:
        return {
            "items": [
                {
                    "metadata": {"name": f"factory-investigate-{kind}"},
                    "spec": {
                        "podSelector": {
                            "matchLabels": {
                                "factory.io/run-kind": f"investigate-{kind}"
                            }
                        }
                    },
                }
                for kind in ("agent", "test")
            ]
            + [
                {
                    "metadata": {"name": "factory-investigate-api"},
                    "spec": {"podSelector": {"matchLabels": {"app": "api"}}},
                }
            ]
        }

    monkeypatch.setattr(kube, "list_namespaced_network_policy", policies)
    run_id = (await backend.dispatch(**_args()))["id"]
    assert (await service.store.get(run_id)).created_at == service.now[0]


async def test_terminate_rechecks_renewed_lease(service: SimpleNamespace) -> None:
    backend = service.backend
    run_id = (await backend.dispatch(**_args()))["id"]
    await backend._terminate(run_id, "expired", "lease expired")
    assert (await service.store.get(run_id)).state == "running"


async def test_reconcile_initializes_clients_after_restart(
    service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, kube = service.backend, service.kube
    run_id = (await backend.dispatch(**_args()))["id"]
    kube.jobs[next(iter(kube.jobs))]["status"] = {"succeeded": 1}
    fresh = investigation_backend.InvestigationBackend(
        service.store, clock=backend.clock
    )

    async def clients() -> None:
        fresh.batch = fresh.core = fresh.networking = kube

    monkeypatch.setattr(fresh, "_clients", clients)
    await fresh.reconcile_tick()
    assert (await service.store.get(run_id)).state in {"stopping", "completed"}


async def test_late_job_is_deleted_on_next_tick(service: SimpleNamespace) -> None:
    backend, kube = service.backend, service.kube
    run_id = (await backend.dispatch(**_args()))["id"]
    await backend._terminate(run_id, "stopped", "test")
    name = f"aif-inv-{run_id.replace('-', '')[:8]}-late"
    kube.jobs[name] = {
        "metadata": {
            "name": name,
            "uid": "late",
            "labels": {"factory.io/investigation": run_id},
        },
        "status": {},
    }
    await backend.reconcile_tick()
    assert name not in kube.jobs
    await backend.reconcile_tick()
    assert (await service.store.get(run_id)).pods_gone


async def test_delete_error_preserves_stopping_state(
    service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, kube = service.backend, service.kube
    run_id = (await backend.dispatch(**_args()))["id"]

    async def fail_once(*_args: Any, **_kwargs: Any) -> None:
        raise KubeError(500)

    async def no_op(*_args: Any) -> None:
        return None

    monkeypatch.setattr(kube, "delete_namespaced_job", fail_once)
    monkeypatch.setattr(backend, "_pods_gone", no_op)
    monkeypatch.setattr(investigation_backend.asyncio, "sleep", no_op)
    await backend.stop(run_id)
    assert (await service.store.get(run_id)).state == "stopping"


async def test_test_tail_scrubs_pem_before_window_and_bounds_log(
    service: SimpleNamespace,
) -> None:
    backend, kube = service.backend, service.kube
    kube.auto_test_complete = True
    run_id = (await backend.dispatch(**_args(test_command="pytest")))["id"]
    base = investigation_manifest._names({"id": run_id})[0]
    kube.pods = [
        {
            "metadata": {"name": "test-pod", "labels": {"job-name": f"{base}-test-1"}},
            "status": {},
        }
    ]
    kube.log_bytes = (
        "-----BEGIN PRIVATE KEY-----\n"
        + "body\n" * 201
        + "-----END PRIVATE KEY-----\nclean\n"
    ).encode()
    result = await backend.run_tests(run_id)
    assert "body" not in result["tail"]
    assert result["tail"] == "clean\n"
    assert kube.log_kwargs["limit_bytes"] == 5 * 1024 * 1024 + 1


async def test_export_failure_releases_slot(
    service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = service.backend

    def fail(*_args: Any) -> Any:
        raise investigation_export.ExportError("missing SHA")

    monkeypatch.setattr(investigation_export, "export", fail)
    with pytest.raises(investigation_backend.InvestigationRefused, match="export"):
        await backend.dispatch(**_args())
    row = await backend.store.live_runs()
    assert row == []
    assert await backend.store.get(service.released[0]) is not None


async def test_log_cap_adds_one_truncation_line(service: SimpleNamespace) -> None:
    backend = service.backend
    run_id = (await backend.dispatch(**_args()))["id"]
    await backend.store.append_logs(run_id, ["safe"] * 20_000)
    await backend.append_log(run_id, ["overflow"])
    await backend.append_log(run_id, ["still overflow"])
    from server.database.models import InvestigationLog  # noqa: PLC0415
    from sqlalchemy import func, select  # noqa: PLC0415

    async with backend.store._session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(InvestigationLog)
            .where(InvestigationLog.run_id == run_id)
        )
        last = await session.scalar(
            select(InvestigationLog.line)
            .where(InvestigationLog.run_id == run_id)
            .order_by(InvestigationLog.seq.desc())
            .limit(1)
        )
    assert count == 20_001
    assert last == "log truncated"


async def test_concurrent_test_admission_one_wins(service: SimpleNamespace) -> None:
    backend, kube = service.backend, service.kube
    run_id = (await backend.dispatch(**_args(test_command="pytest")))["id"]
    kube.before_create = asyncio.Event()
    kube.resume_create = asyncio.Event()
    first = asyncio.create_task(backend.run_tests(run_id))
    await kube.before_create.wait()
    second = asyncio.create_task(backend.run_tests(run_id))
    await asyncio.sleep(0.05)
    assert not second.done()
    kube.resume_create.set()
    with pytest.raises(
        investigation_backend.InvestigationRefused, match="already running"
    ):
        await second
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await service.store.get(run_id)).run_tests_used == 1


async def test_report_uses_only_stored_test_counts(service: SimpleNamespace) -> None:
    backend = service.backend
    run_id = (await backend.dispatch(**_args()))["id"]
    await backend.store.update(run_id, tests=[{"counts": {"passed": 2, "failed": 1}}])
    await backend.put_report(run_id, "all 50 tests passed")
    report = (await backend.store.get(run_id)).report
    assert report["tests"] == [{"counts": {"passed": 2, "failed": 1}}]


async def test_retention_tick_removes_old_logs(service: SimpleNamespace) -> None:
    backend = service.backend
    run_id = (await backend.dispatch(**_args()))["id"]
    await backend.store.append_logs(run_id, ["old"])
    await backend.store.update(
        run_id,
        state="completed",
        pods_gone=True,
        ended_at=service.now[0] - timedelta(days=8),
    )
    await backend.reconcile_tick()
    assert (await backend.logs(run_id))["entries"] == []


async def test_test_job_deadline_is_remaining_time_plus_grace(
    service: SimpleNamespace,
) -> None:
    backend, kube = service.backend, service.kube
    kube.auto_test_complete = True
    run_id = (await backend.dispatch(**_args(test_command="pytest")))["id"]
    await backend.store.update(run_id, created_at=_now() - timedelta(minutes=20))
    await backend.run_tests(run_id)
    # The test Job has a 10-minute timeout and its independent 30-second deadline grace.
    manifest = next(m for m in kube.manifests if "-test-" in m["metadata"]["name"])
    assert 628 <= manifest["spec"]["activeDeadlineSeconds"] <= 630


async def test_scrubbing_after_restart_reads_existing_secret(
    service: SimpleNamespace,
) -> None:
    backend = service.backend
    run_id = (await backend.dispatch(**_args()))["id"]
    backend._tokens.clear()
    await backend.append_log(run_id, ["secret-token"])
    await backend.put_report(run_id, "secret-token")
    assert (await backend.logs(run_id))["entries"][0]["line"] == "[REDACTED]"
    assert "secret-token" not in (await backend.store.get(run_id)).report["findings"]


async def test_scrubbing_refuses_when_secret_missing(service: SimpleNamespace) -> None:
    backend, kube = service.backend, service.kube
    run_id = (await backend.dispatch(**_args()))["id"]
    backend._tokens.clear()
    kube.secrets.clear()
    with pytest.raises(investigation_backend.InvestigationRefused, match="scrubbing"):
        await backend.append_log(run_id, ["secret-token"])
    assert (await backend.logs(run_id))["entries"] == []


async def test_orphan_test_job_blocks_new_test_admission(
    service: SimpleNamespace,
) -> None:
    backend, kube = service.backend, service.kube
    run_id = (await backend.dispatch(**_args(test_command="pytest")))["id"]
    base = investigation_manifest._names({"id": run_id})[0]
    kube.jobs[f"{base}-test-1"] = {
        "metadata": {
            "name": f"{base}-test-1",
            "uid": "orphan",
            "labels": {"factory.io/investigation": run_id},
        },
        "status": {},
    }
    with pytest.raises(
        investigation_backend.InvestigationRefused, match="already running"
    ):
        await backend.run_tests(run_id)


@pytest.mark.parametrize(
    "key, maximum",
    [
        ("minutes", 30),
        ("tokens", 2_000_000),
        ("turns", 200),
        ("test_calls", 10),
    ],
)
@pytest.mark.parametrize("bad", [0, -1, 1])
async def test_every_limit_is_bounded(
    service: SimpleNamespace, key: str, maximum: int, bad: int
) -> None:
    value = maximum + 1 if bad == 1 else bad
    with pytest.raises(investigation_backend.InvestigationRefused, match="limits"):
        await service.backend.dispatch(**_args(limits={key: value}))


async def test_deadline_terminates_agent(service: SimpleNamespace) -> None:
    backend = service.backend
    run_id = (await backend.dispatch(**_args()))["id"]
    service.now[0] += timedelta(minutes=31)
    await backend.renew_lease(run_id)
    await backend.reconcile_tick()
    assert (await backend.store.get(run_id)).reason == "deadline"


async def test_log_batch_over_200_is_refused(service: SimpleNamespace) -> None:
    backend = service.backend
    run_id = (await backend.dispatch(**_args()))["id"]
    with pytest.raises(investigation_backend.InvestigationRefused, match="too many"):
        await backend.append_log(run_id, ["x"] * 201)
    assert (await backend.logs(run_id))["entries"] == []


async def test_network_refusal_precedes_slot_reservation(
    service: SimpleNamespace,
) -> None:
    backend = service.backend
    backend._network.side_effect = investigation_backend.InvestigationRefused(
        "network policy: build"
    )
    with pytest.raises(investigation_backend.InvestigationRefused, match="build"):
        await backend.dispatch(**_args())
    assert await backend.store.live_runs() == []


def test_backend_is_independent_of_the_build_loop():
    # Plan step 5: investigations run on their own task, so a stuck deletion here can
    # never delay the build reconcile loop in agent_kubejob.
    source = (
        Path(__file__).resolve().parents[2]
        / "apps/web-server/server/services/investigation_backend.py"
    ).read_text()
    assert "agent_kubejob" not in source
    assert "reconcile_kubejob_builds" not in source
    assert 'name="investigation-reconcile"' in source
