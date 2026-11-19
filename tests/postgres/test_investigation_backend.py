"""Step 5: investigation lifecycle against real Postgres and fake Kubernetes."""

from __future__ import annotations

import asyncio
import os
import sys
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
        self.log_text = "=== 1 passed in 0.1s ==="
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
        self.jobs[name] = {"metadata": {"name": name, "uid": "uid-1"}, "status": status}
        return self.jobs[name]

    async def read_namespaced_job(self, name: str, _namespace: str) -> Any:
        if name not in self.jobs:
            raise KubeError(404)
        return self.jobs[name]

    async def list_namespaced_job(self, _namespace: str, label_selector: str) -> Any:
        run_id = label_selector.split("=", 1)[1]
        return {
            "items": [
                job
                for name, job in self.jobs.items()
                if run_id.replace("-", "")[:8] in name
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
        if label_selector.startswith("job-name="):
            return {"items": self.pods}
        return {"items": self.pods}

    async def read_namespaced_pod_log(self, _name: str, _namespace: str) -> Any:
        return self.log_text

    async def list_namespaced_network_policy(self, _namespace: str) -> Any:
        return {"items": []}


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
    kube.pods.append({"metadata": {"name": "still-here"}})
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
    kube.pods = [
        {
            "metadata": {"name": "test-pod"},
            "status": {
                "container_statuses": [{"state": {"terminated": {"exit_code": 0}}}]
            },
        }
    ]
    kube.log_text = "=== 1 passed in 0.1s ===\nsecret-token"
    run_id = (await backend.dispatch(**_args(test_command="pytest")))["id"]
    for _ in range(10):
        result = await backend.run_tests(run_id)
        assert result["counts"] == {"passed": 1, "failed": 0, "skipped": 0}
        assert "secret-token" not in result["tail"]
    assert (await service.store.get(run_id)).run_tests_used == 10
    with pytest.raises(investigation_backend.InvestigationRefused, match="limit"):
        await backend.run_tests(run_id)


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
    assert row.state == "running"
    assert row.agent_job in kube.jobs
    assert row.agent_job.removesuffix("-agent") in kube.secrets


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
    await backend.store.update(run_id, tests={"counts": {"passed": 2, "failed": 1}})
    await backend.put_report(run_id, "all 50 tests passed")
    report = (await backend.store.get(run_id)).report
    assert report["tests"] == {"counts": {"passed": 2, "failed": 1}}


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
        "metadata": {"name": f"{base}-test-1", "uid": "orphan"},
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
