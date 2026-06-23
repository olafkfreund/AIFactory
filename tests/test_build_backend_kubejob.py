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
        task_id="p:s",
        project_path=Path(_DATA_ROOT),
        spec_id="s",
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
        task_id="p:s",
        project_path=Path(_DATA_ROOT),
        spec_id="s",
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
    assert bb._resolve_run_py_path() == "/home/projects/MagesticAI/apps/backend/run.py"
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
        task_id="p:s",
        project_path=Path(_DATA_ROOT),
        spec_id="s",
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
        task_id="p:s",
        project_path=Path("/home/dev/myproj"),
        spec_id="s",
    )
    pod = m["spec"]["template"]["spec"]
    assert "volumes" not in pod  # no work, no store


# --------------------------------------------------------------------------- #
# 1b. Build Job env (#671 OAuth-env defect)
# --------------------------------------------------------------------------- #


def test_build_job_env_injects_oauth_token_and_fixed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The load-bearing fix: a fresh Job pod has no credential source, so the
    # pooled OAuth token MUST be injected (else run.py dies 'No OAuth token
    # found'). The fixed non-interactive build env mirrors the in-pod overrides.
    for var in bb._PASSTHROUGH_BUILD_ENV:
        monkeypatch.delenv(var, raising=False)
    env = bb.build_job_env("oauth-tok-123")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-tok-123"
    assert env["CLAUDE_CODE_ENTRYPOINT"] == "cli"
    assert env["CI"] == "true"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_build_job_env_propagates_present_provider_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Provider/runtime SDK env mirrors core/auth.py's agent-keep allowlist, but
    # ONLY when present on the control plane (no empty placeholders), plus
    # GITHUB_TOKEN for run.py's PR endgame.
    for var in bb._PASSTHROUGH_BUILD_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gw.example/v1")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "proxy-tok")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-tok")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-x")
    env = bb.build_job_env("oauth-tok-123")
    assert env["ANTHROPIC_BASE_URL"] == "https://gw.example/v1"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "proxy-tok"
    assert env["GITHUB_TOKEN"] == "gh-tok"
    assert env["ANTHROPIC_MODEL"] == "claude-x"
    # Unset passthrough vars are omitted, not blanked.
    assert "GH_TOKEN" not in env
    assert "NO_PROXY" not in env


def test_build_job_env_propagates_non_claude_provider_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #689: a non-Claude-routed build needs its provider credential + config env
    # in the Job — forwarded only when present, in env (never argv).
    for var in bb._PASSTHROUGH_BUILD_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://ollama.com")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "oc-key")
    env = bb.build_job_env("oauth-tok-123")
    assert env["OPENAI_API_KEY"] == "sk-openai"
    assert env["GEMINI_API_KEY"] == "g-key"
    assert env["OPENAI_COMPATIBLE_BASE_URL"] == "https://ollama.com"
    assert env["OPENAI_COMPATIBLE_API_KEY"] == "oc-key"
    # Provider vars not set on the control plane are omitted (no empty placeholder).
    assert "GOOGLE_API_KEY" not in env
    assert "OLLAMA_API_KEY" not in env
    assert "GEMINI_CLI_TRUST_WORKSPACE" not in env


def test_build_job_env_never_includes_anthropic_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OAuth-only policy: ANTHROPIC_API_KEY must NEVER reach the build Job (it
    # would silently bill the direct-API account). It is not in the passthrough
    # allowlist, so even when set on the control plane it is dropped.
    for var in bb._PASSTHROUGH_BUILD_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("DATABASE_URL", "postgres://secret/should-not-leak")
    env = bb.build_job_env("oauth-tok-123")
    assert "ANTHROPIC_API_KEY" not in env
    # Control-plane secrets run.py doesn't use are scoped out too.
    assert "DATABASE_URL" not in env


def test_build_job_env_without_token_omits_oauth_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No token resolved → no CLAUDE_CODE_OAUTH_TOKEN key (rather than an empty
    # one that would mask the 'no token' condition). Fixed env still present.
    for var in bb._PASSTHROUGH_BUILD_ENV:
        monkeypatch.delenv(var, raising=False)
    env = bb.build_job_env(None)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["CI"] == "true"


def test_manifest_carries_oauth_env_in_container_not_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SECURITY (#599 class): the OAuth token + provider env must land in the
    # container `env` list, NEVER in `command`/argv (ps-visible). The manifest
    # builder threads extra_env through the shared job_dispatch builder.
    for var in bb._PASSTHROUGH_BUILD_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    monkeypatch.setenv("AIFACTORY_IMAGE", "ghcr.io/dataseeek/aifactory:1.2.3")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-tok")
    extra_env = bb.build_job_env("oauth-tok-xyz")
    m = bb.build_run_py_job_manifest(
        task_id="p:s",
        project_path=Path(_DATA_ROOT),
        spec_id="s",
        extra_env=extra_env,
    )
    c = m["spec"]["template"]["spec"]["containers"][0]
    env_by_name = {e["name"]: e["value"] for e in c["env"]}
    assert env_by_name["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-tok-xyz"
    assert env_by_name["GITHUB_TOKEN"] == "gh-tok"
    assert env_by_name["CI"] == "true"
    # The token must NOT appear anywhere in the argv (command).
    argv = " ".join(c["command"])
    assert "oauth-tok-xyz" not in argv
    assert "gh-tok" not in argv
    # Every env entry is a {name,value} pair (literal env), none via argv.
    assert all("name" in e and "value" in e for e in c["env"])


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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


async def test_dispatch_injects_oauth_token_into_job_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #671 OAuth-env defect: the token the caller resolved from the pool must
    # land in the created Job's container env (not argv) so run.py finds it.
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    for var in bb._PASSTHROUGH_BUILD_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(bb, "populate_build_worktree", lambda *_a, **_k: None)
    store = await _make_store(tmp_path / "d.db")
    await store.admit("p:s1", _spawn_args("s1"), cap=2, correlation_key="9")

    backend = bb.KubeJobBuildBackend(store)
    fake = _FakeBatch()
    await backend.dispatch(
        task_id="p:s1",
        project_path=Path(_DATA_ROOT) / "workspaces" / "p",
        spec_id="s1",
        correlation_key="9",
        oauth_token="pooled-token-abc",
        batch=fake,
    )

    manifest = fake.created[0][1]
    c = manifest["spec"]["template"]["spec"]["containers"][0]
    env_by_name = {e["name"]: e["value"] for e in c["env"]}
    assert env_by_name["CLAUDE_CODE_OAUTH_TOKEN"] == "pooled-token-abc"
    # Never in argv (security #599 class).
    assert "pooled-token-abc" not in " ".join(c["command"])


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


# (test_populate_build_worktree_materializes_spec_via_inpod_path removed: the
# in-pod setup_workspace path was replaced by the self-contained clone (#671) —
# spec materialization is now covered by test_populate_builds_self_contained_repo.)


def test_populated_worktree_matches_job_work_subpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The path the control plane populates MUST equal the subPath the Job mounts
    # at /work — otherwise the Job still sees an empty /work.
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    monkeypatch.setenv("AIFACTORY_IMAGE", "ghcr.io/dataseeek/aifactory:1.2.3")
    proj = _git_init_project(tmp_path)
    spec_dir = proj / ".aifactory" / "specs" / "088-feat"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text("# spec\n")

    populated = bb.populate_build_worktree(proj, "088-feat")
    assert populated is not None

    m = bb.build_run_py_job_manifest(
        task_id="proj-1:088-feat",
        project_path=proj,
        spec_id="088-feat",
    )
    c = m["spec"]["template"]["spec"]["containers"][0]
    work_mt = next(mt for mt in c["volumeMounts"] if mt["mountPath"] == "/work")
    # PVC subPath is data-root-relative; the populated worktree is absolute. The
    # Job mounts <data_root>/<subPath> at /work, which must be the populated path.
    assert str(tmp_path / work_mt["subPath"]) == populated


def test_populate_skips_when_outside_data_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end ordering: dispatch() populates the worktree (spec present) BEFORE
    # create_namespaced_job is called. A batch fake records when the Job is created
    # and asserts the spec already exists on disk at that moment.
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    proj = _git_init_project(tmp_path)
    spec_dir = proj / ".aifactory" / "specs" / "110-feat"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text("# spec\n")

    spec_in_worktree = (
        proj
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / "110-feat"
        / ".aifactory"
        / "specs"
        / "110-feat"
        / "spec.md"
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
    await store.admit("proj-1:110-feat", _spawn_args("110-feat"), cap=2)
    backend = bb.KubeJobBuildBackend(store)
    fake = _OrderingBatch()

    await backend.dispatch(
        task_id="proj-1:110-feat",
        project_path=proj,
        spec_id="110-feat",
        batch=fake,
    )

    assert fake.spec_present_at_create is True
    assert spec_in_worktree.exists()


# --------------------------------------------------------------------------- #
# 5. File-auth CLI credential seeding (#690)
# --------------------------------------------------------------------------- #


def _seed_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    monkeypatch.setenv("AIFACTORY_IMAGE", "ghcr.io/dataseeek/aifactory:1.2.3")
    monkeypatch.delenv("AIFACTORY_BUILD_IMAGE", raising=False)
    return Path(_DATA_ROOT) / "workspaces" / "proj-1"


def test_seed_creds_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # No AIFACTORY_CLI_CREDS_SECRET → env-auth-only path unchanged: no seed
    # initContainer, no cc-* volumes.
    monkeypatch.delenv("AIFACTORY_CLI_CREDS_SECRET", raising=False)
    project_path = _seed_env(monkeypatch)
    m = bb.build_run_py_job_manifest(
        task_id="proj-1:s", project_path=project_path, spec_id="s"
    )
    pod = m["spec"]["template"]["spec"]
    assert "initContainers" not in pod
    vol_names = {v["name"] for v in pod.get("volumes", [])}
    assert "cli-creds" not in vol_names
    assert "cc-claude" not in vol_names


def test_seed_creds_injected_when_secret_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIFACTORY_CLI_CREDS_SECRET", "factory-cli-creds")
    project_path = _seed_env(monkeypatch)
    m = bb.build_run_py_job_manifest(
        task_id="proj-1:s", project_path=project_path, spec_id="s"
    )
    pod = m["spec"]["template"]["spec"]

    # seed-creds initContainer present, busybox, mounts the secret at /seed.
    init = next(c for c in pod["initContainers"] if c["name"] == "seed-creds")
    assert init["image"].startswith("busybox")
    seed_mount = next(mt for mt in init["volumeMounts"] if mt["name"] == "cli-creds")
    assert seed_mount["mountPath"] == "/seed"
    assert seed_mount.get("readOnly") is True
    # cp's every known provider file into HOME, tolerant of a partial secret.
    script = init["args"][0]
    for key in (
        "claude-credentials.json",
        "codex-auth.json",
        "copilot-apps.json",
        "gemini-oauth_creds.json",
    ):
        assert f"/seed/{key}" in script
    assert "|| true" in script  # never aborts on a missing provider file

    # cli-creds secret volume points at the configured secret.
    cli = next(v for v in pod["volumes"] if v["name"] == "cli-creds")
    assert cli["secret"]["secretName"] == "factory-cli-creds"

    # The build container mounts the seeded home dirs (so the CLIs find creds).
    build_mounts = {mt["mountPath"] for mt in pod["containers"][0]["volumeMounts"]}
    assert {
        "/home/nonroot/.claude",
        "/home/nonroot/.codex",
        "/home/nonroot/.gemini",
        "/home/nonroot/.config",
    } <= build_mounts


# --------------------------------------------------------------------------- #
# 6. Self-contained build worktree (#671 — /work has a real .git, not a
#    dangling linked-worktree pointer). Real-git integration test.
# --------------------------------------------------------------------------- #

import subprocess as _sp  # noqa: E402


def _git_init_project(root: Path) -> Path:
    proj = root / "workspaces" / "proj-1"
    proj.mkdir(parents=True)

    def g(*a: str) -> None:
        _sp.run(
            ["git", "-C", str(proj), *a], check=True, capture_output=True, text=True
        )

    g("init", "-b", "main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "Test")
    (proj / "README.md").write_text("hi\n")
    g("add", "-A")
    g("commit", "-m", "init")
    g("remote", "add", "origin", "https://github.com/acme/proj.git")
    return proj


def test_populate_builds_self_contained_repo(monkeypatch, tmp_path):
    # #671: the dispatched build Job's /work must be a STANDALONE repo (real .git
    # dir + GitHub origin + task branch + spec), not a linked git worktree whose
    # .git points at the unmounted project repo.
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    proj = _git_init_project(tmp_path)
    spec_dir = proj / ".aifactory" / "specs" / "042-go"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text("# spec\n")

    wt = bb.populate_build_worktree(proj, "042-go")
    assert wt is not None
    wtp = Path(wt)

    # Real .git DIRECTORY — not a linked-worktree `.git` file (the #671 defect).
    assert (wtp / ".git").is_dir()
    # /work stays on the BASE branch (#716): run.py creates the aifactory/<spec>
    # worktree+branch itself in-Job. Pre-checking it out would make run.py's
    # `git worktree add -b` fail ("branch already exists").
    head = _sp.run(
        ["git", "-C", str(wtp), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == "main"
    # origin points at the REAL GitHub remote (for the PR-endgame push), not the
    # local clone source (unreachable from the Job pod).
    origin = _sp.run(
        ["git", "-C", str(wtp), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert origin == "https://github.com/acme/proj.git"
    # The spec is materialized into the working tree.
    assert (wtp / ".aifactory" / "specs" / "042-go" / "spec.md").exists()


def test_populate_skips_when_outside_data_pvc(monkeypatch, tmp_path):
    # Outside the data PVC (laptop/dev) → no /work co-mount → nothing to populate.
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    proj = _git_init_project(tmp_path)
    assert bb.populate_build_worktree(proj, "042-go") is None


# --------------------------------------------------------------------------- #
# 7. RFC-0017 Stage E (#190): JobSpec.workspace_uri -> WORKSPACE_URI env
# --------------------------------------------------------------------------- #


def test_jobspec_workspace_uri_emits_env() -> None:
    from core.job_dispatch import JobSpec, build_job_manifest

    m = build_job_manifest(
        JobSpec(
            service="aifactory",
            job_id="p:s",
            commands=["echo hi"],
            workspace_uri="s3://factory-artifacts/aifactory/1/p/workspace.tar.gz",
        )
    )
    env = {
        e["name"]: e.get("value")
        for e in m["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert (
        env["WORKSPACE_URI"] == "s3://factory-artifacts/aifactory/1/p/workspace.tar.gz"
    )


def test_jobspec_no_workspace_uri_omits_env() -> None:
    from core.job_dispatch import JobSpec, build_job_manifest

    m = build_job_manifest(
        JobSpec(service="aifactory", job_id="p:s", commands=["echo hi"])
    )
    names = {e["name"] for e in m["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "WORKSPACE_URI" not in names  # default None → single-node path unchanged


# --------------------------------------------------------------------------- #
# 8. RFC-0017 Stage E (#190) producer: build_backend packs the worktree +
#    sets workspace_uri, gated by AIFACTORY_PACK_WORKSPACE (default OFF).
# --------------------------------------------------------------------------- #


def test_maybe_pack_workspace_off_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Flag unset → producer is inert: no pack, no URI, co-mount path unchanged.
    monkeypatch.delenv("AIFACTORY_PACK_WORKSPACE", raising=False)
    src = tmp_path / "wt"
    src.mkdir()
    assert (
        bb._maybe_pack_workspace(str(src), task_id="p:s", correlation_key="1") is None
    )


def test_maybe_pack_workspace_none_worktree_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even ON, a None worktree (outside the data PVC) has nothing to pack.
    monkeypatch.setenv("AIFACTORY_PACK_WORKSPACE", "1")
    assert bb._maybe_pack_workspace(None, task_id="p:s", correlation_key="1") is None


def test_maybe_pack_workspace_on_packs_and_returns_uri(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ON + a real worktree → pack to the in-memory fake S3 and return the
    # canonical workspace-archive URI (apis/concurrency-conventions.md §2 layout).
    import core.artifact_store as a_s

    # Use the in-memory transport so no boto3 / S3 / MinIO is needed.
    monkeypatch.setattr(a_s.ArtifactStore, "_client", lambda self: a_s._FakeS3())
    monkeypatch.setenv("AIFACTORY_PACK_WORKSPACE", "1")
    monkeypatch.delenv("S3_BUCKET", raising=False)  # → DEFAULT_BUCKET

    src = tmp_path / "wt"
    (src / "pkg").mkdir(parents=True)
    (src / "main.py").write_text("print('hi')\n")

    uri = bb._maybe_pack_workspace(
        str(src), task_id="proj-1:042-go", correlation_key="482"
    )
    assert uri == (
        "s3://factory-artifacts/aifactory/482/proj-1:042-go/workspace/"
        f"{a_s.WORKSPACE_ARCHIVE}"
    )


def test_maybe_pack_workspace_swallows_pack_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A pack/object-store error must NEVER break dispatch: it is logged and the
    # caller falls back to the /work co-mount (returns None).
    import core.artifact_store as a_s

    def _boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("s3 down")

    monkeypatch.setattr(a_s, "pack_workspace", _boom)
    monkeypatch.setenv("AIFACTORY_PACK_WORKSPACE", "1")
    src = tmp_path / "wt"
    src.mkdir()
    assert (
        bb._maybe_pack_workspace(str(src), task_id="p:s", correlation_key="1") is None
    )


def test_manifest_includes_workspace_uri_when_packed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The producer threads the packed URI onto the manifest → WORKSPACE_URI env,
    # so the Job-side consumer (later slice) can unpack it into /work.
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    monkeypatch.setenv("AIFACTORY_IMAGE", "ghcr.io/dataseeek/aifactory:1.2.3")
    uri = (
        "s3://factory-artifacts/aifactory/482/proj-1:042-go/workspace/workspace.tar.gz"
    )
    m = bb.build_run_py_job_manifest(
        task_id="proj-1:042-go",
        project_path=Path(_DATA_ROOT) / "workspaces" / "proj-1",
        spec_id="042-go",
        correlation_key=482,
        workspace_uri=uri,
    )
    env = {
        e["name"]: e.get("value")
        for e in m["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["WORKSPACE_URI"] == uri
