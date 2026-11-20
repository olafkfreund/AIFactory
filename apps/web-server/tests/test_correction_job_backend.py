"""QA apply-correction as a Job (#2116)."""

# mypy: ignore-errors
# ruff: noqa: ARG001, ARG002, ARG005, I001, S603, S607
# (test doubles take unused args; git fixtures call the git CLI)

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

_REPO = Path(__file__).resolve().parents[3]
for _path in (_REPO / "apps/web-server", _REPO / "apps/backend"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from server.routes import execution  # noqa: E402
from server.services import agent_job_backend as ajb  # noqa: E402
from server.services import job_s3_scope  # noqa: E402
from server.services import spec_job_backend as sj  # noqa: E402
from server.services.build_backend import (  # noqa: E402
    BuildBackendUnavailableError,
    KubeJobBuildBackend,
)

FLAG = "AIFACTORY_CORRECTION_BACKEND"


class FakeBatch:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []
        self.api_client = None

    async def create_namespaced_job(self, namespace, manifest):
        self.created.append((namespace, manifest))

    async def delete_namespaced_job(self, name, namespace, *, propagation_policy):
        return None


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (FLAG, "AIFACTORY_FORBID_INPOD_AGENTS", "AIFACTORY_LOCAL_DEV"):
        monkeypatch.delenv(name, raising=False)
    sj._LIVE_SPEC_JOBS.clear()


@pytest.mark.parametrize(
    ("value", "expected"), [("", False), ("subprocess", False), ("KubeJob", True)]
)
def test_flag(monkeypatch, value, expected) -> None:
    monkeypatch.setenv(FLAG, value)
    assert ajb.correction_kubejob_enabled() is expected


def test_invalid_flag_refuses(monkeypatch) -> None:
    monkeypatch.setenv(FLAG, "k8s")
    with pytest.raises(BuildBackendUnavailableError):
        ajb.correction_kubejob_enabled()


@pytest.mark.asyncio
async def test_failed_job_releases_unpublished_handback_reservation(
    tmp_path, monkeypatch
):
    spec = tmp_path / "proj/.aifactory/specs/001"
    receipts = spec / "handback_receipts"
    receipts.mkdir(parents=True)
    (spec / "handback_received.json").write_text(json.dumps({"receipt_key": "key"}))
    (receipts / "key").touch()

    class FailedProcess:
        job_name = "failed-fixer"

        async def wait(self):
            return 1

    monkeypatch.setattr(
        "qa.correction.check_fix_cycle_assertions", lambda *_a: {"ok": True}
    )
    await ajb._await_correction(FailedProcess(), spec)
    assert not (receipts / "key").exists()


def _scoped(monkeypatch, *, works: bool) -> None:
    def scope(env, ids):
        if works:
            env.pop("S3_ACCESS_KEY", None)
            env.pop("S3_SECRET_KEY", None)
            env["AWS_ACCESS_KEY_ID"] = "scoped"

    monkeypatch.setattr(job_s3_scope, "scope_job_s3", scope)
    monkeypatch.setattr(
        ajb.build_backend,
        "build_job_env",
        lambda _t, _p=None: {
            "S3_ACCESS_KEY": "a",
            "S3_SECRET_KEY": "s",
            "S3_ENDPOINT": "e",
            "GITHUB_TOKEN": "g",
            "GH_TOKEN": "g",
            "GITHUB_PERSONAL_ACCESS_TOKEN": "g2",
            "KEEP": "1",
        },
    )


def test_env_drops_github_keeps_s3_endpoint_and_scopes(monkeypatch, tmp_path) -> None:
    _scoped(monkeypatch, works=True)
    env = ajb._correction_env("t", tmp_path, ["a", "b"])
    assert env["S3_ENDPOINT"] == "e" and env["KEEP"] == "1"
    assert env["AWS_ACCESS_KEY_ID"] == "scoped"
    assert not {n for n in env if n.startswith(("GITHUB_", "GH_"))}


def test_env_refuses_when_scoping_is_a_noop(monkeypatch, tmp_path) -> None:
    _scoped(monkeypatch, works=False)  # scoping off: the bucket-wide key would stay
    with pytest.raises(BuildBackendUnavailableError):
        ajb._correction_env("t", tmp_path, ["a"])


def test_env_refuses_without_any_scoped_key(monkeypatch, tmp_path) -> None:
    _scoped(monkeypatch, works=False)
    monkeypatch.setattr(ajb.build_backend, "build_job_env", lambda _t, _p=None: {})
    with pytest.raises(BuildBackendUnavailableError):
        ajb._correction_env("t", tmp_path, ["a"])


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    spec = proj / ".aifactory/specs/001-x"
    spec.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    (proj / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(proj), "add", "app.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(proj),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "-m",
            "x",
        ],
        check=True,
    )
    (spec / "QA_FIX_REQUEST.md").write_text("fix")
    return proj


def _capture_pack(monkeypatch) -> dict:
    seen: dict = {}

    def pack(store, ref, src):
        seen["files"] = sorted(
            p.relative_to(src).as_posix()
            for p in Path(src).rglob("*")
            if p.is_file() and ".git/" not in p.as_posix()
        )
        return "s3://x"

    monkeypatch.setattr("core.artifact_store.pack_workspace", pack, raising=False)
    monkeypatch.setattr(
        "core.artifact_store.ArtifactStore", lambda: None, raising=False
    )
    return seen


def test_workspace_has_clone_and_top_level_spec_files(monkeypatch, tmp_path) -> None:
    proj = _project(tmp_path)
    spec = proj / ".aifactory/specs/001-x"
    (spec / "sub").mkdir()
    (spec / "sub/inner.txt").write_text("no")
    (tmp_path / "secret").write_text("s")
    (spec / "link").symlink_to(tmp_path / "secret")
    seen = _capture_pack(monkeypatch)
    assert ajb._prepare_correction_workspace(spec, "job1") == "s3://x"
    assert "app.py" in seen["files"]
    assert ".aifactory/specs/001-x/QA_FIX_REQUEST.md" in seen["files"]
    assert not [f for f in seen["files"] if f.endswith(("inner.txt", "link"))]
    assert not (proj / ".aifactory/worktrees/corrections/job1").exists()


def test_handoff_workspace_checks_out_task_branch(monkeypatch, tmp_path) -> None:
    proj = _project(tmp_path)
    spec = proj / ".aifactory/specs/001-x"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(
        ["git", "-C", str(proj), "remote", "add", "origin", str(remote)], check=True
    )
    branch = "factory/001-x"
    subprocess.run(["git", "-C", str(proj), "branch", branch], check=True)
    subprocess.run(["git", "-C", str(proj), "push", "origin", branch], check=True)
    (spec / ".task_branch").write_text(branch + "\n")
    (spec / "handback_received.json").write_text('{"tfactory_task_id":"tf-1"}')
    _capture_pack(monkeypatch)

    ajb._prepare_correction_workspace(spec, "job1")
    clone = proj / ".aifactory/worktrees/corrections/job1"
    assert (
        subprocess.run(
            ["git", "-C", str(clone), "symbolic-ref", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == branch
    )


def test_workspace_does_not_follow_a_cloned_symlink(monkeypatch, tmp_path) -> None:
    proj = _project(tmp_path)
    spec = proj / ".aifactory/specs/001-x"
    target = tmp_path / "outside"
    target.write_text("keep")
    # HEAD holds a symlink at the spec filename; the working tree has a regular file.
    (spec / "notes.md").symlink_to(target)
    subprocess.run(
        ["git", "-C", str(proj), "add", "-f", str(spec / "notes.md")], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(proj),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "-m",
            "link",
        ],
        check=True,
    )
    (spec / "notes.md").unlink()
    (spec / "notes.md").write_text("real")
    _capture_pack(monkeypatch)
    ajb._prepare_correction_workspace(spec, "job1")
    assert target.read_text() == "keep"


def test_workspace_refuses_oversize_spec_file_and_symlinked_dir(
    monkeypatch, tmp_path
) -> None:
    proj = _project(tmp_path)
    spec = proj / ".aifactory/specs/001-x"
    _capture_pack(monkeypatch)
    (spec / "big").write_bytes(b"x" * (ajb._CORRECTION_SPEC_FILE_MAX_BYTES + 1))
    with pytest.raises(BuildBackendUnavailableError):
        ajb._prepare_correction_workspace(spec, "job1")
    (spec / "big").unlink()
    (tmp_path / "elsewhere").mkdir()
    corr = proj / ".aifactory/worktrees"
    corr.mkdir(parents=True)
    (corr / "corrections").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(BuildBackendUnavailableError):
        ajb._prepare_correction_workspace(spec, "job2")
    assert not list((tmp_path / "elsewhere").iterdir())


@pytest.fixture
def job(monkeypatch, tmp_path):
    """Dispatch against a fake batch client; returns (batch, spec_dir, fetches)."""
    proj = _project(tmp_path)
    spec = proj / ".aifactory/specs/001-x"
    batch = FakeBatch()
    fetches: list[tuple] = []
    monkeypatch.setattr(ajb, "store_enabled", lambda: True)
    monkeypatch.setattr(ajb, "claude_oauth_token", lambda: "t")
    _scoped(monkeypatch, works=True)
    _capture_pack(monkeypatch)
    monkeypatch.setattr(
        ajb.build_backend, "_resolve_run_py_path", lambda: "/app/backend/run.py"
    )

    async def get_batch():
        return batch

    monkeypatch.setattr(KubeJobBuildBackend, "_batch_api", staticmethod(get_batch))

    async def no_logs(self, **_k):
        return None

    monkeypatch.setattr(sj.KubeJobLogStreamer, "stream", no_logs)

    async def succeeded(*_a, **_k):
        return "succeeded"

    monkeypatch.setattr(KubeJobBuildBackend, "_job_outcome", staticmethod(succeeded))

    def fetch(project, fetch_id, cancel=None):
        fetches.append((project, fetch_id))
        return True

    monkeypatch.setattr("core.workspace_fetch.fetch_correction", fetch)
    return batch, spec, fetches


async def test_dispatch_creates_job_applies_result_and_runs_guard(
    monkeypatch, job
) -> None:
    batch, spec, fetches = job
    guarded: list[Path] = []
    monkeypatch.setattr(
        "qa.correction.check_fix_cycle_assertions",
        lambda d, workspace=None: guarded.append(Path(d)),
    )
    monkeypatch.setenv("AIFACTORY_FORBID_INPOD_AGENTS", "1")  # the Job stays allowed
    res = await ajb.dispatch_correction_fixer(spec)
    assert res["status"] == "qa_fixing" and res["scheduled"] is True
    await asyncio.gather(*ajb._CORRECTION_WAITERS)
    pod = batch.created[0][1]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert batch.created[0][1]["spec"]["activeDeadlineSeconds"] == 1800
    cmd = json.dumps(pod["containers"][0])
    assert "correction_job.py" in cmd and "001-x" in cmd
    env = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
    assert env["AIFACTORY_CORRECTION_FETCH_ID"] == fetches[0][1]
    assert "GITHUB_TOKEN" not in env and "S3_ACCESS_KEY" not in env
    assert fetches[0][0] == spec.parent.parent.parent
    assert (
        json.loads((spec / "handback_job_result.json").read_text())["status"]
        == "applied"
    )
    assert guarded == [spec]


async def test_failed_fetch_is_recorded_as_failed(monkeypatch, job) -> None:
    _batch, spec, _fetches = job
    monkeypatch.setattr("core.workspace_fetch.fetch_correction", lambda *a, **k: False)
    monkeypatch.setattr(
        "qa.correction.check_fix_cycle_assertions", lambda d, workspace=None: None
    )
    await ajb.dispatch_correction_fixer(spec)
    await asyncio.gather(*ajb._CORRECTION_WAITERS)
    assert (
        json.loads((spec / "handback_job_result.json").read_text())["status"]
        == "failed"
    )


async def test_api_error_on_create_is_a_refusal(monkeypatch, job) -> None:
    batch, spec, _f = job

    async def forbidden(namespace, manifest):
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(batch, "create_namespaced_job", forbidden)
    with pytest.raises(BuildBackendUnavailableError, match="RuntimeError"):
        await ajb.dispatch_correction_fixer(spec)


async def test_dispatch_requires_the_store(monkeypatch, job) -> None:
    _batch, spec, _f = job
    monkeypatch.setattr(ajb, "store_enabled", lambda: False)
    with pytest.raises(BuildBackendUnavailableError):
        await ajb.dispatch_correction_fixer(spec)


async def test_dispatch_refuses_without_scoped_credentials(monkeypatch, job) -> None:
    batch, spec, _f = job
    _scoped(monkeypatch, works=False)
    with pytest.raises(BuildBackendUnavailableError):
        await ajb.dispatch_correction_fixer(spec)
    assert not batch.created


# --- route -------------------------------------------------------------------


def _req(confirm: bool = True) -> execution.ApplyCorrectionRequest:
    return execution.ApplyCorrectionRequest(
        fix_request_md="fix", confirm=confirm, triage={"stub": 1}
    )


@pytest.fixture
def route(monkeypatch, tmp_path):
    monkeypatch.setattr(
        execution, "_resolve_task", lambda _t: ("p", "s", tmp_path, tmp_path)
    )
    calls: list[dict] = []

    async def fake_apply(spec_dir, md, **kw):
        calls.append(kw)
        return {"success": False}  # no receipt or audit side effects

    monkeypatch.setattr(execution, "apply_correction", fake_apply)
    return calls


async def test_route_refusal_unchanged_when_flag_unset(monkeypatch, route) -> None:
    monkeypatch.setenv("AIFACTORY_FORBID_INPOD_AGENTS", "1")
    with pytest.raises(HTTPException) as exc:
        await execution.apply_task_correction("p:s", _req(), _access={})
    assert exc.value.status_code == 409 and "disabled" in exc.value.detail
    assert route == []  # nothing written, nothing run


async def test_route_flag_on_uses_job_fixer_under_forbid(monkeypatch, route) -> None:
    monkeypatch.setenv("AIFACTORY_FORBID_INPOD_AGENTS", "1")
    monkeypatch.setenv(FLAG, "kubejob")
    await execution.apply_task_correction("p:s", _req(), _access={})
    assert route[0]["fixer_fn"] is ajb.dispatch_correction_fixer


async def test_route_flag_off_and_no_forbid_keeps_in_pod_fixer(
    monkeypatch, route
) -> None:
    await execution.apply_task_correction("p:s", _req(), _access={})
    assert route[0]["fixer_fn"] is None


async def test_route_carries_task_id_but_ignores_callback_url(route) -> None:
    request = _req()
    request.tfactory_task_id = "tf-1"
    request.tfactory_callback_url = "https://caller.example/steal"
    await execution.apply_task_correction("p:s", request, _access={})
    assert route[0]["tfactory_task_id"] == "tf-1"
    assert "tfactory_callback_url" not in route[0]


async def test_route_preview_is_not_refused(monkeypatch, route) -> None:
    monkeypatch.setenv("AIFACTORY_FORBID_INPOD_AGENTS", "1")
    await execution.apply_task_correction("p:s", _req(confirm=False), _access={})
    assert route[0]["fixer_fn"] is None


async def test_route_invalid_flag_refuses_before_anything_runs(
    monkeypatch, route
) -> None:
    monkeypatch.setenv(FLAG, "k8s")  # the app maps this error to a 503
    with pytest.raises(BuildBackendUnavailableError):
        await execution.apply_task_correction("p:s", _req(), _access={})
    assert route == []


async def test_route_dispatch_failure_propagates_without_fallback(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(FLAG, "kubejob")
    monkeypatch.setattr(
        execution,
        "get_settings",
        lambda: SimpleNamespace(REQUIRE_CORRECTION_TRIAGE=False),
    )
    monkeypatch.setattr(
        execution, "_resolve_task", lambda _t: ("p", "s", tmp_path, tmp_path)
    )

    async def boom(*_a, **_k):
        raise AssertionError("in-pod fixer must not run")

    monkeypatch.setattr("qa.correction._default_fixer", boom)

    async def refuse(_spec):
        raise BuildBackendUnavailableError("requires DATABASE_URL")

    monkeypatch.setattr(execution, "dispatch_correction_fixer", refuse)
    with pytest.raises(BuildBackendUnavailableError, match="DATABASE_URL"):
        await execution.apply_task_correction(
            "p:s",
            execution.ApplyCorrectionRequest(fix_request_md="fix", confirm=True),
            _access={},
        )
    assert (tmp_path / "QA_FIX_REQUEST.md").exists()  # request kept; nothing ran
