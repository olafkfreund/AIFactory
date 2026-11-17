"""PR review and clarification as Jobs (A4, #1932)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _path in (_REPO / "apps/web-server", _REPO / "apps/backend"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from server.services import agent_job_backend as ajb  # noqa: E402
from server.services import clarification_service  # noqa: E402
from server.services import spec_job_backend as sj  # noqa: E402
from server.services.build_backend import (  # noqa: E402
    BuildBackendUnavailableError,
    KubeJobBuildBackend,
)


class FakeBatch:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.api_client = None

    async def create_namespaced_job(self, namespace: str, manifest: dict) -> None:
        self.created.append((namespace, manifest))

    async def delete_namespaced_job(self, name, namespace, *, propagation_policy):
        self.deleted.append(name)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AIFACTORY_REVIEW_BACKEND", "AIFACTORY_FORBID_INPOD_AGENTS"):
        monkeypatch.delenv(name, raising=False)
    sj._LIVE_SPEC_JOBS.clear()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", False), ("subprocess", False), ("KubeJob", True)],
)
def test_flag(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("AIFACTORY_REVIEW_BACKEND", value)
    assert ajb.review_kubejob_enabled() is expected


def test_invalid_flag_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIFACTORY_REVIEW_BACKEND", "k8s")
    with pytest.raises(BuildBackendUnavailableError):
        ajb.review_kubejob_enabled()


def test_manifest_has_no_service_account_token_and_short_deadline() -> None:
    m = ajb.build_agent_job_manifest(
        job_id="clarify-abcd1234",
        argv=["claude", "--print", "--model", "haiku", "p q"],
        workspace_uri=None,
        extra_env={"X": "1"},
        models=["haiku"],
    )
    spec = m["spec"]
    assert spec["activeDeadlineSeconds"] == 1800
    assert spec["template"]["spec"]["automountServiceAccountToken"] is False


async def test_clarification_job_env_has_no_github_or_s3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ajb, "store_enabled", lambda: True)
    monkeypatch.setattr(
        ajb.build_backend,
        "build_job_env",
        lambda _t, _p=None: {
            "S3_ACCESS_KEY": "a",
            "S3_BUCKET": "b",
            "GITHUB_TOKEN": "g",
            "GITHUB_PERSONAL_ACCESS_TOKEN": "g2",
            "GH_ENTERPRISE_TOKEN": "g3",
            "KEEP": "1",
        },
    )

    async def no_logs(self, **_k):
        return None

    monkeypatch.setattr(sj.KubeJobLogStreamer, "stream", no_logs)

    async def succeeded(*_a, **_k):
        return "succeeded"

    monkeypatch.setattr(KubeJobBuildBackend, "_job_outcome", staticmethod(succeeded))
    batch = FakeBatch()
    proc = await ajb.dispatch_clarification(prompt="hi", oauth_token="t", batch=batch)
    env = {
        e["name"]
        for e in batch.created[0][1]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "KEEP" in env
    assert not {n for n in env if n.startswith(("S3_", "GITHUB_", "GH_"))}
    assert await proc.wait() == 0


async def test_dispatch_requires_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ajb, "store_enabled", lambda: False)
    with pytest.raises(BuildBackendUnavailableError):
        await ajb.dispatch_clarification(prompt="hi", oauth_token=None)


def _proc(monkeypatch, data: bytes, *, code: int = 0, truncated: bool = False):
    async def no_logs(self, **_k):
        return None

    async def outcome(*_a, **_k):
        return "succeeded" if code == 0 else "failed"

    monkeypatch.setattr(sj.KubeJobLogStreamer, "stream", no_logs)
    monkeypatch.setattr(KubeJobBuildBackend, "_job_outcome", staticmethod(outcome))

    async def finish() -> int:
        return 0

    async def batch_factory():
        return SimpleNamespace(
            api_client=None,
            delete_namespaced_job=lambda *a, **k: asyncio.sleep(0),
        )

    proc = sj.SpecJobProcess(
        "ns", "job", batch_factory=batch_factory, finish=finish, poll_interval=0.01
    )
    proc.stdout.feed_data(data)
    proc.truncated = truncated
    return proc


async def test_read_clarification_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _proc(monkeypatch, b'{"skip": true}\n')
    assert await ajb.read_clarification(proc, timeout=2) == '{"skip": true}\n'


async def test_read_clarification_truncated_or_failed_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        await ajb.read_clarification(
            _proc(monkeypatch, b"{", truncated=True), timeout=2
        )
        is None
    )
    assert (
        await ajb.read_clarification(_proc(monkeypatch, b"{}", code=1), timeout=2)
        is None
    )


async def test_read_clarification_oversize_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    big = b"x" * (ajb.CLARIFICATION_MAX_BYTES + 1)
    assert await ajb.read_clarification(_proc(monkeypatch, big), timeout=2) is None


async def test_feed_flags_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _proc(monkeypatch, b"")
    await proc._feed("y" * (sj._LOG_LINE_MAX + 1))
    assert proc.truncated is True


async def test_clarification_service_uses_job_and_never_spawns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AIFACTORY_REVIEW_BACKEND", "kubejob")
    monkeypatch.setenv("AIFACTORY_FORBID_INPOD_AGENTS", "1")  # Jobs stay allowed
    monkeypatch.setattr(clarification_service, "claude_oauth_token", lambda: "t")

    async def dispatch(**_k):
        return object()

    async def read(_job):
        return '{"skip": true, "skipReason": "clear", "questions": []}'

    async def spawn(*_a, **_k):
        raise AssertionError("no in-pod spawn")

    monkeypatch.setattr(clarification_service, "dispatch_clarification", dispatch)
    monkeypatch.setattr(clarification_service, "read_clarification", read)
    monkeypatch.setattr(clarification_service.asyncio, "create_subprocess_exec", spawn)
    out = await clarification_service.generate_clarification_questions(
        "t", "d", tmp_path
    )
    assert out["skip"] is True and out["skipReason"] == "clear"


async def test_clarification_dispatch_failure_skips_without_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AIFACTORY_REVIEW_BACKEND", "kubejob")
    monkeypatch.setattr(clarification_service, "claude_oauth_token", lambda: None)

    async def dispatch(**_k):
        raise BuildBackendUnavailableError("no store")

    async def spawn(*_a, **_k):
        raise AssertionError("no in-pod spawn")

    monkeypatch.setattr(clarification_service, "dispatch_clarification", dispatch)
    monkeypatch.setattr(clarification_service.asyncio, "create_subprocess_exec", spawn)
    out = await clarification_service.generate_clarification_questions(
        "t", "d", tmp_path
    )
    assert out["skip"] is True


@pytest.fixture
def review_service(monkeypatch: pytest.MonkeyPatch):
    from server.services import pr_review_service as prs

    svc = prs.PRReviewService()
    svc._emit_progress = lambda *a, **k: asyncio.sleep(0)
    svc.errors = []

    async def emit_error(_pid, _pr, msg):
        svc.errors.append(msg)

    svc._emit_error = emit_error
    svc._process_output = lambda *a, **k: asyncio.sleep(0)
    monkeypatch.setattr(prs, "spawn", lambda coro: coro.close())
    monkeypatch.setattr(
        prs.project_settings_store, "load_project_env", lambda *a, **k: asyncio.sleep(0)
    )

    async def mcp(*_a, **_k):
        return {}

    monkeypatch.setattr(prs.project_settings_store, "mcp_spawn_env", mcp)
    return prs, svc


async def test_start_review_uses_job_not_subprocess(
    monkeypatch: pytest.MonkeyPatch, review_service, tmp_path: Path
) -> None:
    prs, svc = review_service
    monkeypatch.setenv("AIFACTORY_REVIEW_BACKEND", "kubejob")
    monkeypatch.setenv("AIFACTORY_FORBID_INPOD_AGENTS", "1")
    calls = {}

    async def dispatch(**kwargs):
        calls.update(kwargs)
        return SimpleNamespace(terminate=lambda: None)

    async def spawn(*_a, **_k):
        raise AssertionError("no in-pod spawn")

    monkeypatch.setattr(prs, "dispatch_pr_review", dispatch)
    monkeypatch.setattr(prs, "claude_oauth_token", lambda: "t")
    monkeypatch.setattr(prs.asyncio, "create_subprocess_exec", spawn)
    assert await svc.start_review("p", 7, tmp_path, followup=True) is True
    assert calls["pr_number"] == 7 and calls["followup"] is True


async def test_start_review_dispatch_failure_has_no_fallback(
    monkeypatch: pytest.MonkeyPatch, review_service, tmp_path: Path
) -> None:
    prs, svc = review_service
    monkeypatch.setenv("AIFACTORY_REVIEW_BACKEND", "kubejob")

    async def dispatch(**_k):
        raise BuildBackendUnavailableError("no store")

    async def spawn(*_a, **_k):
        raise AssertionError("no in-pod spawn")

    monkeypatch.setattr(prs, "dispatch_pr_review", dispatch)
    monkeypatch.setattr(prs, "claude_oauth_token", lambda: None)
    monkeypatch.setattr(prs.asyncio, "create_subprocess_exec", spawn)
    assert await svc.start_review("p", 7, tmp_path) is False
    assert svc.errors


async def test_start_review_refused_in_pod_when_forbidden(
    monkeypatch: pytest.MonkeyPatch, review_service, tmp_path: Path
) -> None:
    prs, svc = review_service
    monkeypatch.setenv("AIFACTORY_FORBID_INPOD_AGENTS", "1")
    monkeypatch.delenv("AIFACTORY_LOCAL_DEV", raising=False)

    async def spawn(*_a, **_k):
        raise AssertionError("no in-pod spawn")

    monkeypatch.setattr(prs.asyncio, "create_subprocess_exec", spawn)
    assert await svc.start_review("p", 7, tmp_path) is False
    assert "disabled" in svc.errors[0]


async def test_read_clarification_stops_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _proc(monkeypatch, b"x" * (ajb.CLARIFICATION_MAX_BYTES * 3))
    assert await ajb.read_clarification(proc, timeout=2) is None


def test_followup_workspace_refuses_symlinked_review_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess

    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True)
    (tmp_path / "elsewhere").mkdir()
    (proj / ".aifactory").symlink_to(tmp_path / "elsewhere")
    monkeypatch.setattr(
        "core.artifact_store.pack_workspace", lambda *a, **k: "s3://x", raising=False
    )
    with pytest.raises(BuildBackendUnavailableError):
        ajb._prepare_review_workspace(proj, 7, True, "job1")
    assert not list((tmp_path / "elsewhere").iterdir())
