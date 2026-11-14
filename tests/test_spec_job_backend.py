"""Security boundaries for spec creation in a Kubernetes Job (#1930-#1932)."""

from __future__ import annotations

import asyncio
import json
import re
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
from server.services.process_monitor import monitor_process  # noqa: E402


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
    reads: list[tuple[str, int | None]] = []

    def get_bytes(self, key: str, max_bytes: int | None = None) -> bytes:
        self.reads.append((key, max_bytes))
        return self.objects[key][:max_bytes]


def test_artifact_store_reads_only_requested_bytes() -> None:
    body = SimpleNamespace(read=Mock(side_effect=lambda limit=None: b"abcdef"[:limit]))
    store = artifact_store.ArtifactStore()
    store._s3 = SimpleNamespace(get_object=lambda **_kwargs: {"Body": body})
    assert store.get_bytes("key", max_bytes=3) == b"abc"
    body.read.assert_called_once_with(3)
    body.read.reset_mock()
    assert store.get_bytes("key") == b"abcdef"
    body.read.assert_called_once_with()


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
    assert all(limit == (8 << 20) + 1 for _, limit in fake_store.reads)


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
    assert all(limit == (8 << 20) + 1 for _, limit in fake_store.reads)


@pytest.mark.parametrize("return_code", [1, -15])
@pytest.mark.parametrize("approved", [None, True])
async def test_failed_spec_job_never_starts_build(
    project: tuple[Path, Path],
    service: AgentService,
    return_code: int,
    approved: bool | None,
) -> None:
    root, spec_dir = project
    if approved is not None:
        (spec_dir / "review_state.json").write_text('{"approved": true}')
    service._spec_dirs["p:s1"] = spec_dir
    service.start_task_execution = AsyncMock()
    proc = SimpleNamespace(pid=None, wait=AsyncMock(return_value=return_code))
    await monitor_process(service, "p:s1", proc, root)
    service.start_task_execution.assert_not_called()
    assert service._emit_progress.await_args.args[0].phase.value == "failed"


async def test_spec_workspace_rejects_external_symlink_before_pack(
    monkeypatch: pytest.MonkeyPatch,
    project: tuple[Path, Path],
) -> None:
    root, spec_dir = project
    secret = root.parent / "secret"
    secret.write_text("secret")
    (spec_dir / "outside").symlink_to(secret)
    pack = AsyncMock(side_effect=AssertionError("pack called"))
    monkeypatch.setattr(artifact_store, "pack_workspace", pack)
    with pytest.raises(BuildBackendUnavailableError, match="symlink"):
        await sj.KubeJobSpecBackend().dispatch(
            task_id="p:s1",
            project_path=root,
            spec_id="s1",
            spec_dir=spec_dir,
            task_text="title",
            model=None,
            complexity=None,
            auto_approve=False,
            oauth_token=None,
            mcp_env={},
            marker_nonce="nonce",
        )
    pack.assert_not_called()
    assert not (root / ".aifactory/worktrees/specs/s1").exists()


async def test_repeat_dispatch_uses_distinct_kubernetes_names(
    monkeypatch: pytest.MonkeyPatch, project: tuple[Path, Path]
) -> None:
    root, spec_dir = project
    batch = FakeBatch()
    monkeypatch.setattr(
        artifact_store, "pack_workspace", lambda *_: "s3://bucket/workspace"
    )
    monkeypatch.setattr(sj.KubeJobLogStreamer, "stream", AsyncMock())
    task_id = "project:" + "long-spec-id-" * 3
    processes = []
    for _ in range(2):
        processes.append(
            await sj.KubeJobSpecBackend().dispatch(
                task_id=task_id,
                project_path=root,
                spec_id="s1",
                spec_dir=spec_dir,
                task_text="title",
                model=None,
                complexity=None,
                auto_approve=False,
                oauth_token=None,
                mcp_env={},
                marker_nonce="nonce",
                batch=batch,
            )
        )
    names = [manifest["metadata"]["name"] for _, manifest in batch.created]
    assert len(set(names)) == 2
    assert all(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name) for name in names)
    assert all(re.search(r"-spec-[0-9a-f]{8}$", name) for name in names)
    for proc in processes:
        proc.terminate()
        assert await proc.wait() == -15


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


@pytest.mark.parametrize(
    ("model", "seeded"), [("sonnet", False), ("copilot:gpt-5", True)]
)
def test_spec_job_gets_copilot_login_only_for_copilot_model(
    monkeypatch: pytest.MonkeyPatch, model: str, seeded: bool
) -> None:
    monkeypatch.setenv("AIFACTORY_CLI_CREDS_SECRET", "cli-creds")
    manifest = sj.build_spec_job_manifest(
        task_id="p:s1",
        fetch_id="a" * 32,
        spec_id="s1",
        workspace_uri="s3://bucket/w.tar.gz",
        extra_env={},
        model=model,
    )
    seed = manifest["spec"]["template"]["spec"]["initContainers"][-1]
    assert seed["name"] == "seed-creds"
    script = seed["args"][0]
    assert ("copilot-apps.json" in script) is seeded
    assert "claude-credentials.json" in script


def test_partial_fetch_without_spec_md_drops_earlier_approval(
    fake_store: type[FakeArtifactStore], tmp_path: Path
) -> None:
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "review_state.json").write_text('{"approved": true}')
    fake_store.objects[
        workspace_fetch._spec_file_key("fetch", "implementation_plan.json")
    ] = b'{"swapped": true}'
    assert sj._finish_spec_fetch(spec_dir, "fetch", True) == 1
    assert json.loads((spec_dir / "review_state.json").read_text())["approved"] is False


@pytest.mark.parametrize("rel", [".aifactory/spec_task.md", ".aifactory/specs/s1"])
def test_tracked_symlink_in_clone_cannot_redirect_writes_outside_it(
    monkeypatch: pytest.MonkeyPatch,
    project: tuple[Path, Path],
    tmp_path: Path,
    rel: str,
) -> None:
    root, spec_dir = project
    (spec_dir / "requirements.json").write_text("{}")
    victim = tmp_path / "victim"
    victim.mkdir()
    target = victim / "review_state.json" if rel.endswith(".md") else victim
    if rel.endswith(".md"):
        target.write_text("control")
    # Commit the link at the path the clone will have, without touching the
    # live spec dir (which the control plane owns and git ignores here).
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t"]
    blob = (
        subprocess.run(
            [*git, "hash-object", "-w", "--stdin"],
            input=str(target).encode(),
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(
        [*git, "update-index", "--add", "--cacheinfo", f"120000,{blob},{rel}"],
        check=True,
    )
    subprocess.run([*git, "commit", "-q", "-m", "link"], check=True)
    pack = AsyncMock(side_effect=AssertionError("packed"))
    monkeypatch.setattr(artifact_store, "pack_workspace", pack)
    monkeypatch.setattr(artifact_store, "ArtifactStore", lambda: None)
    with pytest.raises(BuildBackendUnavailableError):
        sj._prepare_spec_workspace(root, "s1", spec_dir, "ATTACKER TEXT", "p-s1-spec")
    pack.assert_not_called()
    assert sorted(p.name for p in victim.iterdir()) == (
        ["review_state.json"] if rel.endswith(".md") else []
    )
    if rel.endswith(".md"):
        assert target.read_text() == "control"
    assert not (root / ".aifactory/worktrees/specs/s1").exists()


@pytest.mark.parametrize(("status", "code"), [("succeeded", 0), ("failed", 1)])
async def test_job_outcome_sets_returncode_and_closes_stdout(
    monkeypatch: pytest.MonkeyPatch, status: str, code: int
) -> None:
    class DoneBatch(FakeBatch):
        async def read_namespaced_job(self, name: str, namespace: str):
            return SimpleNamespace(
                status=SimpleNamespace(
                    succeeded=1 if status == "succeeded" else None,
                    failed=1 if status == "failed" else None,
                )
            )

    batch = DoneBatch()

    async def batch_factory():
        return batch

    async def no_logs(self, **_kwargs):
        return None

    monkeypatch.setattr(sj.KubeJobLogStreamer, "stream", no_logs)
    finish = AsyncMock(return_value=0)
    proc = sj.SpecJobProcess(
        "ns", "spec-job", batch_factory=batch_factory, finish=finish
    )
    assert await proc.wait() == code
    assert finish.await_count == (1 if status == "succeeded" else 0)
    assert await proc.stdout.read() == b""
    assert batch.deleted == []


async def test_stop_ends_log_stream_without_waiting_for_drain_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = FakeBatch()

    async def batch_factory():
        return batch

    async def until_inactive(self, **_kwargs):
        await asyncio.Event().wait()  # a log follow that never ends

    monkeypatch.setattr(sj.KubeJobLogStreamer, "stream", until_inactive)
    proc = sj.SpecJobProcess(
        "ns", "spec-job", batch_factory=batch_factory, finish=AsyncMock()
    )
    proc.terminate()
    assert await asyncio.wait_for(proc.wait(), timeout=5) == -15


def test_push_uploads_only_allowlisted_regular_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    puts: list[str] = []

    class Store:
        def put_bytes(self, key: str, *_args, **_kwargs) -> None:
            puts.append(key)

    monkeypatch.setattr(artifact_store, "ArtifactStore", Store)
    (tmp_path / "spec.md").write_text("# spec")
    (tmp_path / "review_state.json").write_text('{"approved": true}')
    (tmp_path / "task_control.json").write_text("{}")
    (tmp_path / "context.json").symlink_to(tmp_path / "task_control.json")
    monkeypatch.delenv(workspace_fetch.SPEC_FETCH_ID_ENV, raising=False)
    assert workspace_fetch.maybe_push_spec_files(tmp_path) == 0
    monkeypatch.setenv(workspace_fetch.SPEC_FETCH_ID_ENV, "fetch")
    assert workspace_fetch.maybe_push_spec_files(tmp_path) == 1
    assert puts == [workspace_fetch._spec_file_key("fetch", "spec.md")]


async def test_refused_dispatch_releases_pooled_credential(
    monkeypatch: pytest.MonkeyPatch, project: tuple[Path, Path], service: AgentService
) -> None:
    monkeypatch.setenv("AIFACTORY_SPEC_BACKEND", "kubejob")
    monkeypatch.setattr(
        sc.KubeJobSpecBackend, "dispatch", AsyncMock(side_effect=RuntimeError("no s3"))
    )
    released: list[str] = []
    service._release_task_credential = released.append
    with pytest.raises(RuntimeError):
        await service.start_spec_creation("p:s1", project[0], "title", "body")
    assert released == ["p:s1"]


def test_fresh_task_without_auto_approve_gets_unapproved_review_state(
    fake_store: type[FakeArtifactStore], tmp_path: Path
) -> None:
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    fake_store.objects[workspace_fetch._spec_file_key("fetch", "spec.md")] = b"# spec"
    assert sj._finish_spec_fetch(spec_dir, "fetch", False) == 0
    assert json.loads((spec_dir / "review_state.json").read_text())["approved"] is False
