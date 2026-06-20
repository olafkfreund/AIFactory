"""RFC-0016 #671 — control/execution split (run.py as a k8s Job) tests.

Unit-level, no real cluster: the k8s client is a fake (``_FakeBatch``) and the
durable job-state store is the real one backed by a SQLite file DB (the same
pattern as ``test_agent_service_durable_admission.py``). These cover the four
properties the RFC calls out:

* the Job manifest is built correctly (nix-base image, run.py command, worktree
  + warm-store mounts, SA, no-retry);
* the backend selects subprocess vs kubejob by env;
* reconcile-by-poll marks terminal from a job-state row; and
* the reaper marks a vanished Job failed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Backend core (vendored job_dispatch) + web-server server packages on path.
_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "apps" / "web-server", _REPO / "apps" / "backend"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))

from core.job_dispatch import DEFAULT_NIX_IMAGE  # noqa: E402
from server.database.models import Base  # noqa: E402
from server.services import build_backend as bb  # noqa: E402
from server.services.job_state_store import (  # noqa: E402
    JobStateStore,
    SpawnArgs,
)

_DATA_ROOT = "/home/nonroot/.aifactory"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"api {status}")
        self.status = status


class _FakeBatch:
    """Minimal kubernetes_asyncio BatchV1Api stand-in."""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.created: list[tuple[str, dict]] = []
        self.deleted: list[tuple[str, str]] = []
        # job_names that "exist" in the cluster (for read/reap).
        self._existing: set[str] = set(existing or ())
        self.api_client = None

    async def create_namespaced_job(self, namespace: str, manifest: dict) -> None:
        self.created.append((namespace, manifest))
        self._existing.add(manifest["metadata"]["name"])

    async def read_namespaced_job(self, name: str, namespace: str) -> Any:
        if name in self._existing:
            return object()
        raise _ApiError(404)

    async def delete_namespaced_job(
        self, name: str, namespace: str, **_kw: Any
    ) -> None:
        self.deleted.append((namespace, name))
        self._existing.discard(name)


_ENGINES: list = []


@pytest.fixture(autouse=True)
async def _dispose_engines():
    yield
    while _ENGINES:
        await _ENGINES.pop().dispose()


async def _make_store(db_path: Path) -> JobStateStore:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return JobStateStore(session_factory=factory)


def _spawn_args(spec_id: str) -> SpawnArgs:
    return SpawnArgs(project_path="/data/p", spec_id=spec_id)


# --------------------------------------------------------------------------- #
# 1. Manifest correctness
# --------------------------------------------------------------------------- #


def test_manifest_runs_run_py_on_nix_base_with_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Worktree under the data root so it gets co-mounted; warm store + SA set.
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    monkeypatch.setenv("AIFACTORY_NIX_STORE_PVC", "aifactory-nix-store")
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    monkeypatch.delenv("AIFACTORY_SANDBOX_IMAGE", raising=False)

    project_path = Path(_DATA_ROOT) / "workspaces" / "proj-1"
    m = bb.build_run_py_job_manifest(
        task_id="proj-1:042-go-hello",
        project_path=project_path,
        spec_id="042-go-hello",
        correlation_key=482,
    )

    assert m["kind"] == "Job"
    assert m["spec"]["backoffLimit"] == 0  # no silent retries
    assert m["metadata"]["name"].startswith("factory-aifactory-")

    pod = m["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "aifactory-sandbox"
    assert pod["automountServiceAccountToken"] is False

    c = pod["containers"][0]
    assert c["image"] == DEFAULT_NIX_IMAGE  # thin nix-base default
    cmd = c["command"][2]
    # run.py entrypoint against the co-mounted worktree, NOT nix-develop-wrapped.
    assert "python run.py" in cmd
    assert "--spec 042-go-hello" in cmd
    assert "--project-dir /work" in cmd
    assert "nix develop" not in cmd

    mount_paths = {mt["mountPath"] for mt in c["volumeMounts"]}
    assert mount_paths == {"/work", "/nix/store"}
    # worktree subPath is the data-root-relative worktree dir.
    work_mt = next(mt for mt in c["volumeMounts"] if mt["mountPath"] == "/work")
    assert work_mt["subPath"] == (
        "workspaces/proj-1/.aifactory/worktrees/tasks/042-go-hello"
    )

    env_names = {e["name"] for e in c["env"]}
    assert {"JOB_ID", "CORRELATION_KEY", "FACTORY_SERVICE"} <= env_names


def test_manifest_image_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "ghcr.io/acme/custom:tag")
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    m = bb.build_run_py_job_manifest(
        task_id="p:s", project_path=Path(_DATA_ROOT), spec_id="s",
    )
    assert m["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "ghcr.io/acme/custom:tag"
    )


def test_manifest_outside_data_root_has_no_worktree_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A project outside the PVC root (e.g. a laptop) → no worktree co-mount.
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    monkeypatch.delenv("AIFACTORY_NIX_STORE_PVC", raising=False)
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    m = bb.build_run_py_job_manifest(
        task_id="p:s", project_path=Path("/home/dev/myproj"), spec_id="s",
    )
    pod = m["spec"]["template"]["spec"]
    assert "volumes" not in pod  # no work, no store


# --------------------------------------------------------------------------- #
# 2. Backend selection by env
# --------------------------------------------------------------------------- #


def test_backend_defaults_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIFACTORY_BUILD_BACKEND", raising=False)
    assert bb.selected_backend() == bb.BACKEND_SUBPROCESS
    assert bb.kubejob_enabled() is False


def test_backend_kubejob_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "kubejob")
    assert bb.selected_backend() == bb.BACKEND_KUBEJOB
    assert bb.kubejob_enabled() is True


def test_backend_unknown_falls_back_to_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIFACTORY_BUILD_BACKEND", "lambda")
    assert bb.selected_backend() == bb.BACKEND_SUBPROCESS
    assert bb.kubejob_enabled() is False


# --------------------------------------------------------------------------- #
# 3. Dispatch records the k8s-job worker_ref
# --------------------------------------------------------------------------- #


async def test_dispatch_records_worker_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    store = await _make_store(tmp_path / "d.db")
    # Reserve the slot first (admit makes the row running w/ subprocess ref).
    await store.admit("p:s1", _spawn_args("s1"), cap=2, correlation_key="9")

    backend = bb.KubeJobBuildBackend(store)
    fake = _FakeBatch()
    job_name = await backend.dispatch(
        task_id="p:s1",
        project_path=Path(_DATA_ROOT) / "workspaces" / "p",
        spec_id="s1",
        correlation_key="9",
        batch=fake,
    )

    assert fake.created and fake.created[0][1]["metadata"]["name"] == job_name
    state = await store.get_state("p:s1")
    assert state is not None
    assert state["worker_ref"] == {
        "kind": "k8s-job",
        "namespace": "factory",
        "job_name": job_name,
    }


# --------------------------------------------------------------------------- #
# 4. Reconcile-by-poll marks terminal from a job-state row
# --------------------------------------------------------------------------- #


async def test_reconcile_by_poll_returns_terminal(
    tmp_path: Path,
) -> None:
    store = await _make_store(tmp_path / "r.db")
    await store.admit("p:s2", _spawn_args("s2"), cap=2)
    await store.set_worker_ref(
        "p:s2", {"kind": "k8s-job", "namespace": "factory", "job_name": "j2"}
    )
    backend = bb.KubeJobBuildBackend(store)

    # Still running → None.
    assert await backend.reconcile_by_poll("p:s2") is None

    # The Job writes its terminal state into the row (what the pod does).
    await store.mark_terminal("p:s2", "done", result={"pr": "url"})
    assert await backend.reconcile_by_poll("p:s2") == "done"


# --------------------------------------------------------------------------- #
# 5. Reaper marks a vanished Job failed
# --------------------------------------------------------------------------- #


async def test_reaper_fails_vanished_job(tmp_path: Path) -> None:
    store = await _make_store(tmp_path / "x.db")
    await store.admit("p:s3", _spawn_args("s3"), cap=2)
    await store.set_worker_ref(
        "p:s3", {"kind": "k8s-job", "namespace": "factory", "job_name": "gone"}
    )
    backend = bb.KubeJobBuildBackend(store)

    # The cluster has no such Job → reaper fails it.
    fake = _FakeBatch(existing=set())
    reaped = await backend.reap_vanished_jobs(batch=fake)
    assert reaped == ["p:s3"]

    state = await store.get_state("p:s3")
    assert state is not None
    assert state["lifecycle_state"] == "failed"
    assert "disappeared" in (state["error"] or "")


async def test_reaper_leaves_present_job_running(tmp_path: Path) -> None:
    store = await _make_store(tmp_path / "y.db")
    await store.admit("p:s4", _spawn_args("s4"), cap=2)
    await store.set_worker_ref(
        "p:s4", {"kind": "k8s-job", "namespace": "factory", "job_name": "live"}
    )
    backend = bb.KubeJobBuildBackend(store)

    fake = _FakeBatch(existing={"live"})  # Job still exists
    reaped = await backend.reap_vanished_jobs(batch=fake)
    assert reaped == []
    state = await store.get_state("p:s4")
    assert state is not None
    assert state["lifecycle_state"] == "running"


async def test_reaper_fails_present_job_past_deadline(tmp_path: Path) -> None:
    store = await _make_store(tmp_path / "z.db")
    await store.admit("p:s5", _spawn_args("s5"), cap=2)
    await store.set_worker_ref(
        "p:s5", {"kind": "k8s-job", "namespace": "factory", "job_name": "wedged"}
    )
    backend = bb.KubeJobBuildBackend(store)

    fake = _FakeBatch(existing={"wedged"})  # present but wedged
    # now far in the future relative to updated_at → past the deadline.
    reaped = await backend.reap_vanished_jobs(
        batch=fake, now=10**12, deadline_seconds=1
    )
    assert reaped == ["p:s5"]
    state = await store.get_state("p:s5")
    assert state is not None
    assert state["lifecycle_state"] == "failed"
    assert "deadline" in (state["error"] or "")


async def test_delete_job_issues_delete(tmp_path: Path) -> None:
    store = await _make_store(tmp_path / "del.db")
    await store.admit("p:s6", _spawn_args("s6"), cap=2)
    await store.set_worker_ref(
        "p:s6", {"kind": "k8s-job", "namespace": "factory", "job_name": "k6"}
    )
    backend = bb.KubeJobBuildBackend(store)
    fake = _FakeBatch(existing={"k6"})
    assert await backend.delete_job("p:s6", batch=fake) is True
    assert fake.deleted == [("factory", "k6")]
