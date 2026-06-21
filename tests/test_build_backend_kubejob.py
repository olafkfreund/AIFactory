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


def test_manifest_runs_run_py_on_build_image_with_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Worktree under the data root so it gets co-mounted; warm store + SA set.
    # The build image defaults to the running aifactory image (AIFACTORY_IMAGE,
    # chart-injected) — NOT the thin nix gate substrate (#671 fix).
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    monkeypatch.setenv("AIFACTORY_NIX_STORE_PVC", "aifactory-nix-store")
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    monkeypatch.delenv("AIFACTORY_BUILD_IMAGE", raising=False)
    monkeypatch.setenv("AIFACTORY_IMAGE", "ghcr.io/dataseeek/aifactory:1.2.3")
    # Even if the gate substrate var is set, the build must NOT pick it up.
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", DEFAULT_NIX_IMAGE)
    # Unset so run.py resolves to the image-default backend path deterministically.
    monkeypatch.delenv("APP_BACKEND_PATH", raising=False)

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
    # The build runs on the aifactory runtime image (python-capable), NOT the
    # thin nix gate image — that was the #671 StartError defect.
    assert c["image"] == "ghcr.io/dataseeek/aifactory:1.2.3"
    assert c["image"] != DEFAULT_NIX_IMAGE
    cmd = c["command"][2]
    # The entrypoint is a plain interpreter invocation that only works on a
    # python-capable image: bash -c "python <abs>/run.py …", NOT
    # nix-develop-wrapped.
    assert c["command"][:2] == ["bash", "-c"]
    # run.py is referenced by its ABSOLUTE image path (default image layout
    # here, since APP_BACKEND_PATH is unset) — NOT a bare ``run.py`` that would
    # resolve against the /work worktree (the #671 ``can't open file
    # '/work/run.py'`` defect). The build still operates on /work via
    # --project-dir.
    assert "python /home/projects/MagesticAI/apps/backend/run.py" in cmd
    assert "python run.py " not in cmd  # never a bare run.py against /work
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


def test_manifest_build_image_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    # AIFACTORY_BUILD_IMAGE is the explicit build-only override and beats both
    # the downward-API AIFACTORY_IMAGE and the gate AIFACTORY_SANDBOX_IMAGE.
    monkeypatch.setenv("AIFACTORY_BUILD_IMAGE", "ghcr.io/acme/custom:tag")
    monkeypatch.setenv("AIFACTORY_IMAGE", "ghcr.io/dataseeek/aifactory:1.2.3")
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", DEFAULT_NIX_IMAGE)
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    m = bb.build_run_py_job_manifest(
        task_id="p:s", project_path=Path(_DATA_ROOT), spec_id="s",
    )
    assert m["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "ghcr.io/acme/custom:tag"
    )


def test_manifest_ignores_sandbox_image(monkeypatch: pytest.MonkeyPatch) -> None:
    # The build Job must NEVER use AIFACTORY_SANDBOX_IMAGE (the shared thin nix
    # gate substrate) — that is the #671 defect. With only the gate var set and
    # no build/running image, it falls back to DEFAULT_NIX_IMAGE *via the logged
    # fallback*, not by consulting the sandbox var.
    monkeypatch.delenv("AIFACTORY_BUILD_IMAGE", raising=False)
    monkeypatch.delenv("AIFACTORY_IMAGE", raising=False)
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "ghcr.io/acme/should-not-leak:tag")
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    m = bb.build_run_py_job_manifest(
        task_id="p:s", project_path=Path(_DATA_ROOT), spec_id="s",
    )
    image = m["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image != "ghcr.io/acme/should-not-leak:tag"
    assert image == DEFAULT_NIX_IMAGE  # safe fallback, never the gate image


def test_resolve_build_image_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    # Direct unit coverage of the resolver precedence + safe fallback.
    monkeypatch.delenv("AIFACTORY_BUILD_IMAGE", raising=False)
    monkeypatch.delenv("AIFACTORY_IMAGE", raising=False)
    # 3) neither set -> logged fallback to the given default.
    assert bb._resolve_build_image("fallback:img") == "fallback:img"
    # 2) AIFACTORY_IMAGE (downward-API) wins over the fallback.
    monkeypatch.setenv("AIFACTORY_IMAGE", "running:img")
    assert bb._resolve_build_image("fallback:img") == "running:img"
    # 1) AIFACTORY_BUILD_IMAGE (explicit override) wins over everything.
    monkeypatch.setenv("AIFACTORY_BUILD_IMAGE", "build:img")
    assert bb._resolve_build_image("fallback:img") == "build:img"


def test_resolve_run_py_path_is_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    # run.py must be referenced by its ABSOLUTE image path (#671): a bare
    # ``run.py`` resolved against the /work worktree died with ``can't open file
    # '/work/run.py'`` because run.py lives in the image backend dir, not /work.
    # Unset → the image-default backend layout (Dockerfile APP_BACKEND_PATH).
    monkeypatch.delenv("APP_BACKEND_PATH", raising=False)
    assert (
        bb._resolve_run_py_path()
        == "/home/projects/MagesticAI/apps/backend/run.py"
    )
    # APP_BACKEND_PATH (the SAME var the in-pod path resolves) is honored, and a
    # trailing slash never doubles up.
    monkeypatch.setenv("APP_BACKEND_PATH", "/opt/backend/")
    assert bb._resolve_run_py_path() == "/opt/backend/run.py"
    monkeypatch.setenv("APP_BACKEND_PATH", "/opt/backend")
    assert bb._resolve_run_py_path() == "/opt/backend/run.py"


def test_manifest_run_py_path_honours_backend_path_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Job entrypoint uses the resolved absolute run.py path, NOT a bare
    # ``run.py`` against /work, while keeping --project-dir /work.
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    monkeypatch.setenv("AIFACTORY_IMAGE", "ghcr.io/dataseeek/aifactory:1.2.3")
    monkeypatch.setenv("APP_BACKEND_PATH", "/opt/backend")
    m = bb.build_run_py_job_manifest(
        task_id="p:s", project_path=Path(_DATA_ROOT), spec_id="s",
    )
    cmd = m["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert "python /opt/backend/run.py " in cmd
    assert "python run.py " not in cmd  # never bare against /work
    assert "--project-dir /work" in cmd


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
    # This test exercises worker_ref recording, not worktree population; stub the
    # pre-dispatch population (covered by its own tests below) so it doesn't try to
    # run real git / import the backend workspace package.
    monkeypatch.setattr(bb, "populate_build_worktree", lambda *_a, **_k: None)
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


# --------------------------------------------------------------------------- #
# 6. Pre-dispatch worktree population (#671 build-worktree defect)
# --------------------------------------------------------------------------- #
#
# The build Job co-mounts the data-PVC worktree subPath at /work, but that subPath
# was never populated — the Job saw an empty stub and run.py exited
# ``Spec '<id>' not found``. The control plane (sharing the same single-node data
# PVC) must create + populate the worktree (git worktree + the materialized spec,
# reusing the in-pod path) BEFORE the Job is created. These tests use a FAKE
# ``core.workspace`` module so no real git runs.


class _FakeWorkspaceModule:
    """Stand-in for the backend ``core.workspace`` package.

    Records the ``setup_workspace`` call and (faithfully to the real ISOLATED
    path) creates the worktree dir + copies the spec into it on disk, so a test
    can assert the populated path matches the Job's /work subPath.
    """

    class WorkspaceMode:
        ISOLATED = "isolated"
        DIRECT = "direct"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def setup_workspace(
        self,
        project_dir: Path,
        spec_name: str,
        mode: str,
        source_spec_dir: Path | None = None,
    ) -> tuple[Path, Any, Path | None]:
        self.calls.append(
            {
                "project_dir": Path(project_dir),
                "spec_name": spec_name,
                "mode": mode,
                "source_spec_dir": source_spec_dir,
            }
        )
        # Mirror create_worktree's path layout + copy_spec_to_worktree's effect.
        worktree = Path(project_dir) / ".aifactory" / "worktrees" / "tasks" / spec_name
        localized = worktree / ".aifactory" / "specs" / spec_name
        localized.mkdir(parents=True, exist_ok=True)
        if source_spec_dir is not None and Path(source_spec_dir).exists():
            (localized / "spec.md").write_text(
                (Path(source_spec_dir) / "spec.md").read_text()
            )
        return worktree, object(), localized


def _install_fake_workspace(monkeypatch: pytest.MonkeyPatch) -> _FakeWorkspaceModule:
    fake = _FakeWorkspaceModule()
    monkeypatch.setitem(sys.modules, "core.workspace", fake)
    return fake


def _author_spec(project_path: Path, spec_id: str) -> Path:
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.md").write_text("# spec\n")
    return spec_dir


def test_populate_build_worktree_materializes_spec_via_inpod_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # project under the data root → the Job WILL co-mount /work, so the control
    # plane must populate that subPath before dispatch.
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    fake = _install_fake_workspace(monkeypatch)

    project_path = tmp_path / "workspaces" / "proj-7"
    _author_spec(project_path, "077-feat")

    populated = bb.populate_build_worktree(project_path, "077-feat")

    # Reused the in-pod preparation: ISOLATED mode + the authored spec dir.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["mode"] == fake.WorkspaceMode.ISOLATED
    assert call["project_dir"] == project_path
    assert call["spec_name"] == "077-feat"
    assert call["source_spec_dir"] == project_path / ".aifactory" / "specs" / "077-feat"

    # The populated worktree carries the materialized spec (the very thing run.py's
    # find_spec needs at /work/.aifactory/specs/<id>/spec.md).
    assert populated is not None
    spec_in_worktree = (
        Path(populated) / ".aifactory" / "specs" / "077-feat" / "spec.md"
    )
    assert spec_in_worktree.exists()


def test_populated_worktree_matches_job_work_subpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The path the control plane populates MUST equal the subPath the Job mounts
    # at /work — otherwise the Job still sees an empty /work.
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    monkeypatch.setenv("AIFACTORY_IMAGE", "ghcr.io/dataseeek/aifactory:1.2.3")
    _install_fake_workspace(monkeypatch)

    project_path = tmp_path / "workspaces" / "proj-8"
    _author_spec(project_path, "088-feat")

    populated = bb.populate_build_worktree(project_path, "088-feat")
    assert populated is not None

    m = bb.build_run_py_job_manifest(
        task_id="proj-8:088-feat", project_path=project_path, spec_id="088-feat",
    )
    c = m["spec"]["template"]["spec"]["containers"][0]
    work_mt = next(mt for mt in c["volumeMounts"] if mt["mountPath"] == "/work")
    # PVC subPath is data-root-relative; the populated worktree is absolute. The
    # Job mounts <data_root>/<subPath> at /work, which must be the populated path.
    assert str(tmp_path / work_mt["subPath"]) == populated


def test_populate_skips_when_outside_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A project outside the PVC root → the Job has no /work co-mount, so there is
    # nothing (and no shared PVC) to populate; population is a no-op.
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", "/home/nonroot/.aifactory")
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    fake = _install_fake_workspace(monkeypatch)

    project_path = tmp_path / "laptop" / "myproj"
    _author_spec(project_path, "099-feat")

    assert bb.populate_build_worktree(project_path, "099-feat") is None
    assert fake.calls == []  # never touched the in-pod path


def test_populate_raises_when_spec_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No authored spec under the project → fail loudly BEFORE dispatch rather than
    # launch a Job that will hit the very "Spec not found" this fix prevents.
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    _install_fake_workspace(monkeypatch)

    project_path = tmp_path / "workspaces" / "proj-9"
    project_path.mkdir(parents=True, exist_ok=True)  # no spec dir

    with pytest.raises(FileNotFoundError):
        bb.populate_build_worktree(project_path, "100-missing")


async def test_dispatch_populates_worktree_before_job_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end ordering: dispatch() populates the worktree (spec present) BEFORE
    # create_namespaced_job is called. A batch fake records when the Job is created
    # and asserts the spec already exists on disk at that moment.
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    _install_fake_workspace(monkeypatch)

    project_path = tmp_path / "workspaces" / "proj-10"
    _author_spec(project_path, "110-feat")

    spec_in_worktree = (
        project_path / ".aifactory" / "worktrees" / "tasks" / "110-feat"
        / ".aifactory" / "specs" / "110-feat" / "spec.md"
    )

    class _OrderingBatch(_FakeBatch):
        def __init__(self) -> None:
            super().__init__()
            self.spec_present_at_create: bool | None = None

        async def create_namespaced_job(self, namespace: str, manifest: dict) -> None:
            # The worktree MUST already be populated by the time the Job is born.
            self.spec_present_at_create = spec_in_worktree.exists()
            await super().create_namespaced_job(namespace, manifest)

    store = await _make_store(tmp_path / "ord.db")
    await store.admit("proj-10:110-feat", _spawn_args("110-feat"), cap=2)
    backend = bb.KubeJobBuildBackend(store)
    fake = _OrderingBatch()

    await backend.dispatch(
        task_id="proj-10:110-feat",
        project_path=project_path,
        spec_id="110-feat",
        batch=fake,
    )

    assert fake.spec_present_at_create is True
    assert spec_in_worktree.exists()
