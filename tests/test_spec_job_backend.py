"""Security boundaries for spec creation in a Kubernetes Job (#1930-#1932)."""

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _path in (_REPO / "apps/web-server", _REPO / "apps/backend"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core import artifact_store, workspace_fetch  # noqa: E402
from server.services import agent_spec_creation as sc  # noqa: E402
from server.services import spec_job_backend as sj  # noqa: E402
from server.services.agent_service import AgentService  # noqa: E402
from server.services.build_backend import (  # noqa: E402
    BuildBackendUnavailableError,
    KubeJobBuildBackend,
)


class FakeBatch:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []
        self.deleted: list[tuple[str, str, str]] = []
        self.api_client = None

    async def create_namespaced_job(self, namespace: str, manifest: dict) -> None:
        self.created.append((namespace, manifest))

    async def read_namespaced_job(self, name: str, namespace: str) -> SimpleNamespace:
        return SimpleNamespace(status=SimpleNamespace(succeeded=None, failed=None))

    async def delete_namespaced_job(
        self, name: str, namespace: str, *, propagation_policy: str
    ) -> None:
        self.deleted.append((namespace, name, propagation_policy))


class FakeArtifactStore:
    objects: dict[str, bytes] = {}
    reads: list[str] = []

    def get_bytes(self, key: str) -> bytes:
        self.reads.append(key)
        return self.objects[key]


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> type[FakeArtifactStore]:
    FakeArtifactStore.objects = {}
    FakeArtifactStore.reads = []
    monkeypatch.setattr(artifact_store, "ArtifactStore", FakeArtifactStore)
    return FakeArtifactStore


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    spec_dir = root / ".aifactory/specs/s1"
    spec_dir.mkdir(parents=True)
    return root, spec_dir


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> AgentService:
    svc = AgentService()
    svc._store_enabled = True
    svc._resolve_claude_token_pooled = lambda _task_id: ("oauth-test", "p", "profile")
    svc._kubejob_backend_enabled = lambda: False
    svc._marker_nonce_env = lambda _task_id: {"AIFACTORY_MARKER_NONCE": "nonce-test"}
    svc._emit_progress = AsyncMock()
    svc._process_output = AsyncMock()
    svc._monitor_process = AsyncMock()

    async def no_settings(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(sc.project_settings_store, "load_project_env", no_settings)
    monkeypatch.setattr(sc.project_settings_store, "mcp_spawn_env", no_settings)

    def discard(coro):
        coro.close()

    monkeypatch.setattr(sc, "spawn", discard)
    monkeypatch.setattr("server.rmux.integration.create_if_enabled", AsyncMock())
    return svc


async def test_flag_on_dispatches_one_job_without_in_pod_spawn_and_keeps_fallback_disabled(
    monkeypatch: pytest.MonkeyPatch,
    project: tuple[Path, Path],
    service: AgentService,
    fake_store: type[FakeArtifactStore],
) -> None:
    root, spec_dir = project
    batch = FakeBatch()
    monkeypatch.setenv("AIFACTORY_SPEC_BACKEND", "kubejob")
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "sensitive-pvc")
    secret_names = (
        "JWT_SECRET",
        "APP_JWT_SECRET",
        "API_TOKEN",
        "APP_API_TOKEN",
        "DATABASE_URL",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    )
    for name in secret_names:
        monkeypatch.setenv(name, f"leak-{name}")

    async def batch_factory():
        return batch

    async def no_logs(self, **_kwargs):
        return None

    def pack(_store, _ref, clone: Path) -> str:
        assert (
            clone / ".aifactory/spec_task.md"
        ).read_text() == "private title\n\nprivate body"
        assert (clone / ".aifactory/specs/s1").is_dir()
        return "s3://bucket/workspace.tar.gz"

    monkeypatch.setattr(artifact_store, "pack_workspace", pack)
    monkeypatch.setattr(KubeJobBuildBackend, "_batch_api", staticmethod(batch_factory))
    monkeypatch.setattr(sj.KubeJobLogStreamer, "stream", no_logs)
    spawn = AsyncMock(side_effect=AssertionError("in-pod spawn"))
    monkeypatch.setattr(sc.asyncio, "create_subprocess_exec", spawn)

    proc = await service.start_spec_creation(
        "p:s1", root, "private title", "private body"
    )
    assert len(batch.created) == 1
    spawn.assert_not_called()
    assert service._monitor_process.call_args.kwargs["cmd"] is None
    assert service._monitor_process.call_args.kwargs["env"] is None

    manifest = batch.created[0][1]
    pod = manifest["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert not set(secret_names) & env.keys()
    assert env["AIFACTORY_MARKER_NONCE"] == "nonce-test"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-test"
    assert env["AIFACTORY_SPEC_FETCH_ID"]
    argv = str(container["command"]) + str(container.get("args", []))
    assert "private title" not in argv
    assert "private body" not in argv
    assert pod["automountServiceAccountToken"] is False
    assert all(
        "persistentVolumeClaim" not in volume for volume in pod.get("volumes", [])
    )
    assert not (root / ".aifactory/worktrees/specs/s1").exists()
    proc.send_signal(signal.SIGTERM)
    assert await proc.wait() == -15


def test_fetch_back_writes_only_allowlisted_files_and_preserves_control_files(
    fake_store: type[FakeArtifactStore], tmp_path: Path
) -> None:
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    protected = (
        "task_metadata.json",
        "requirements.json",
        "review_state.json",
        "task_control.json",
    )
    before = {name: f"control-{name}".encode() for name in protected}
    for name, data in before.items():
        (spec_dir / name).write_bytes(data)
        fake_store.objects[workspace_fetch._spec_file_key("fetch", name)] = (
            b'{"approved": true}'
        )
    expected = {}
    for name in workspace_fetch.SPEC_FETCH_FILES:
        data = b"# spec\n" if name.endswith(".md") else b'{"valid": true}'
        expected[name] = data
        fake_store.objects[workspace_fetch._spec_file_key("fetch", name)] = data

    assert set(workspace_fetch.fetch_spec_files(spec_dir, "fetch")) == set(expected)
    assert {name: (spec_dir / name).read_bytes() for name in expected} == expected
    assert {name: (spec_dir / name).read_bytes() for name in protected} == before
    assert len(fake_store.reads) == len(expected)


def test_fetch_back_skips_oversize_and_invalid_json_without_clobbering(
    fake_store: type[FakeArtifactStore], tmp_path: Path
) -> None:
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "context.json").write_bytes(b'{"old": true}')
    fake_store.objects[workspace_fetch._spec_file_key("fetch", "spec.md")] = b"x" * (
        (8 << 20) + 1
    )
    fake_store.objects[workspace_fetch._spec_file_key("fetch", "context.json")] = (
        b"not json"
    )
    fake_store.objects[workspace_fetch._spec_file_key("fetch", "research.json")] = (
        b'"scalar"'
    )
    assert workspace_fetch.fetch_spec_files(spec_dir, "fetch") == []
    assert (spec_dir / "context.json").read_bytes() == b'{"old": true}'
    assert not (spec_dir / "spec.md").exists()
    assert not (spec_dir / "research.json").exists()


@pytest.mark.parametrize("auto_approve", [True, False])
def test_control_plane_decides_review_state_not_job(
    fake_store: type[FakeArtifactStore], tmp_path: Path, auto_approve: bool
) -> None:
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "review_state.json").write_text('{"approved": true}')
    fake_store.objects[workspace_fetch._spec_file_key("fetch", "spec.md")] = b"# spec"
    fake_store.objects[workspace_fetch._spec_file_key("fetch", "review_state.json")] = (
        b'{"approved": true}'
    )
    assert sj._finish_spec_fetch(spec_dir, "fetch", auto_approve) == 0
    state = json.loads((spec_dir / "review_state.json").read_text())
    assert state["approved"] is auto_approve
    if auto_approve:
        assert state["approved_by"] == "auto"


async def test_flag_off_spawns_original_subprocess_without_job(
    monkeypatch: pytest.MonkeyPatch, project: tuple[Path, Path], service: AgentService
) -> None:
    root, _ = project
    monkeypatch.delenv("AIFACTORY_SPEC_BACKEND", raising=False)
    proc = SimpleNamespace(stdout=asyncio.StreamReader(), stderr=asyncio.StreamReader())
    spawn = AsyncMock(return_value=proc)
    dispatch = AsyncMock(side_effect=AssertionError("Job backend touched"))
    monkeypatch.setattr(sc.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(sc.KubeJobSpecBackend, "dispatch", dispatch)
    await service.start_spec_creation("p:s1", root, "title", "body")
    cmd = spawn.call_args.args
    assert cmd[:4] == (
        sys.executable,
        str(service.backend_path / "runners/spec_runner.py"),
        "--task",
        "title\n\nbody",
    )
    assert "--no-build" not in cmd
    assert "--auto-approve" in cmd
    assert "--spec-dir" in cmd
    dispatch.assert_not_called()


@pytest.mark.parametrize("flag,task_id", [("other", "p:s1"), ("kubejob", "s1")])
async def test_invalid_backend_or_unprefixed_job_task_refuses_in_pod_fallback(
    monkeypatch: pytest.MonkeyPatch,
    project: tuple[Path, Path],
    service: AgentService,
    flag: str,
    task_id: str,
) -> None:
    monkeypatch.setenv("AIFACTORY_SPEC_BACKEND", flag)
    spawn = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(sc.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(sc.KubeJobSpecBackend, "dispatch", dispatch)
    with pytest.raises(BuildBackendUnavailableError):
        await service.start_spec_creation(task_id, project[0], "title", "body")
    spawn.assert_not_called()
    dispatch.assert_not_called()


async def test_stop_deletes_job_and_wait_returns_minus_fifteen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = FakeBatch()

    async def batch_factory():
        return batch

    async def no_logs(self, **_kwargs):
        return None

    monkeypatch.setattr(sj.KubeJobLogStreamer, "stream", no_logs)
    proc = sj.SpecJobProcess(
        "ns", "spec-job", batch_factory=batch_factory, finish=AsyncMock(return_value=0)
    )
    AgentService._signal_process_tree(proc, signal.SIGTERM)
    assert await proc.wait() == -15
    assert batch.deleted == [("ns", "spec-job", "Background")]


async def test_sweep_treats_live_spec_job_as_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = SimpleNamespace(
        metadata=SimpleNamespace(name="live-spec", creation_timestamp=0)
    )
    batch = FakeBatch()

    async def list_jobs(*_args, **_kwargs):
        return SimpleNamespace(items=[job])

    batch.list_namespaced_job = list_jobs
    backend = object.__new__(KubeJobBuildBackend)
    backend._store = SimpleNamespace(get_active_kubejobs=AsyncMock(return_value=[]))
    monkeypatch.setattr(sj, "_LIVE_SPEC_JOBS", {"live-spec"})
    assert await backend.sweep_orphan_jobs(now=10_000, batch=batch) == []
    assert batch.deleted == []
    sj._LIVE_SPEC_JOBS.clear()
    assert await backend.sweep_orphan_jobs(now=10_000, batch=batch) == ["live-spec"]
