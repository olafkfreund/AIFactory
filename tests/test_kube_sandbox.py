"""KubeJobBackend (#68): manifest builder + gate_runner kubejob selection."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from core.kube_sandbox import build_job_manifest  # noqa: E402
from core.factory_sandbox import RunResult  # noqa: E402
from agents.gate_runner import _select_runner, _default_runner  # noqa: E402


def test_manifest_is_one_shot_gc_hardened():
    m = build_job_manifest("fsbx-abc", "ghcr.io/x/rust:1.90", ["cargo build", "cargo test"])
    assert m["kind"] == "Job" and m["metadata"]["name"] == "fsbx-abc"
    spec = m["spec"]
    assert spec["backoffLimit"] == 0                    # one shot, no retries
    assert spec["ttlSecondsAfterFinished"] == 120       # auto-GC
    t = spec["template"]["spec"]
    assert t["restartPolicy"] == "Never"
    assert t["automountServiceAccountToken"] is False   # gate needs no k8s API
    assert t["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    c = t["containers"][0]
    assert c["image"] == "ghcr.io/x/rust:1.90"
    assert c["command"] == ["bash", "-c", "cargo build && cargo test"]
    assert c["resources"]["limits"]["memory"] == "2Gi"


def test_select_runner_kubejob_backend(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SANDBOX_GATES", "1")
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "ghcr.io/x/go:1.25")
    monkeypatch.setenv("AIFACTORY_SANDBOX_BACKEND", "kubejob")

    import core.kube_sandbox as ks
    calls = {}

    class FakeKubeSandbox:
        def __init__(self, image, **kw): calls["image"] = image
        def run(self, commands, **kw): calls["commands"] = commands; return RunResult(True, 0, "go1.25", [])

    monkeypatch.setattr(ks, "KubeJobSandbox", FakeKubeSandbox)

    runner = _select_runner()
    assert runner is not _default_runner
    code, out = runner(["go", "version"], Path("/work"))
    assert code == 0 and out == "go1.25"
    assert calls["image"] == "ghcr.io/x/go:1.25"
    assert calls["commands"] == ["go version"]   # argv shlex-joined


def test_select_runner_defaults_to_docker_backend(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SANDBOX_GATES", "1")
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "img")
    monkeypatch.delenv("AIFACTORY_SANDBOX_BACKEND", raising=False)
    # docker backend selected (not the host runner, not kube)
    assert _select_runner() is not _default_runner


def test_kube_backend_error_is_gate_failure(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SANDBOX_GATES", "1")
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "img")
    monkeypatch.setenv("AIFACTORY_SANDBOX_BACKEND", "kubejob")
    import core.kube_sandbox as ks

    class Boom:
        def __init__(self, *a, **k): ...
        def run(self, *a, **k): raise RuntimeError("api down")

    monkeypatch.setattr(ks, "KubeJobSandbox", Boom)
    code, out = _select_runner()(["x"], Path("/w"))
    assert code == 1 and "kube-sandbox error" in out
