"""KubeJobBackend (#68): manifest builder + gate_runner kubejob selection."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from agents.gate_runner import _default_runner, _select_runner  # noqa: E402
from core.factory_sandbox import RunResult  # noqa: E402
from core.kube_sandbox import _pvc_subpath, build_job_manifest  # noqa: E402


def test_manifest_is_one_shot_gc_hardened():
    m = build_job_manifest(
        "fsbx-abc", "ghcr.io/x/rust:1.90", ["cargo build", "cargo test"]
    )
    assert m["kind"] == "Job" and m["metadata"]["name"] == "fsbx-abc"
    spec = m["spec"]
    assert spec["backoffLimit"] == 0  # one shot, no retries
    assert spec["ttlSecondsAfterFinished"] == 120  # auto-GC
    t = spec["template"]["spec"]
    assert t["restartPolicy"] == "Never"
    assert t["automountServiceAccountToken"] is False  # gate needs no k8s API
    assert t["imagePullSecrets"] == [{"name": "ghcr-pull"}]
    c = t["containers"][0]
    assert c["image"] == "ghcr.io/x/rust:1.90"
    assert c["command"] == ["bash", "-c", "cargo build && cargo test"]
    assert c["resources"]["limits"]["memory"] == "2Gi"


def test_manifest_pod_hardening_and_task_label():
    # #812 (Factory#274 compensating controls): pinned securityContext on every
    # gate Job pod/container, and the factory.io/kind=task pod label that puts
    # the pod under the chart's per-task NetworkPolicy.
    m = build_job_manifest(
        "fsbx-abc", "img", ["nix --version"], nix_store_pvc="aifactory-nix-store"
    )
    tpl = m["spec"]["template"]
    assert tpl["metadata"]["labels"]["factory.io/kind"] == "task"
    t = tpl["spec"]
    assert t["securityContext"] == {
        "runAsNonRoot": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    hardened = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    assert t["containers"][0]["securityContext"] == hardened
    assert t["initContainers"][0]["securityContext"] == hardened


def test_manifest_no_repo_mount_by_default():
    # Toolchain-only Job (back-compat): no PVC volume, no workingDir.
    m = build_job_manifest("fsbx-abc", "img", ["go version"])
    t = m["spec"]["template"]["spec"]
    assert "volumes" not in t
    assert "volumeMounts" not in t["containers"][0]
    assert "workingDir" not in t["containers"][0]


def test_manifest_co_mounts_worktree_via_pvc_subpath():
    m = build_job_manifest(
        "fsbx-abc",
        "img",
        ["go build ./..."],
        repo_pvc="aifactory-data",
        repo_subpath="workspaces/olafkfreund-hello-go/.aifactory/worktrees/tasks/hello-go",
    )
    t = m["spec"]["template"]["spec"]
    assert t["volumes"] == [
        {
            "name": "repo",
            "persistentVolumeClaim": {"claimName": "aifactory-data", "readOnly": False},
        }
    ]
    c = t["containers"][0]
    assert c["workingDir"] == "/work"
    vm = c["volumeMounts"][0]
    assert vm["name"] == "repo" and vm["mountPath"] == "/work"
    assert vm["subPath"].endswith("worktrees/tasks/hello-go")
    assert vm["readOnly"] is False


def test_manifest_no_nix_store_mount_by_default():
    # RFC-0016 #197: cold behavior unchanged when no warm-store PVC named.
    m = build_job_manifest("fsbx-abc", "img", ["nix --version"])
    t = m["spec"]["template"]["spec"]
    assert "initContainers" not in t
    assert "volumes" not in t


def test_manifest_mounts_warm_nix_store_with_seed_init():
    # RFC-0016 #197: the whole /nix tree is served from the warm-store PVC, and a
    # seed initContainer populates it from the image on first use.
    m = build_job_manifest(
        "fsbx-abc",
        "ghcr.io/olafkfreund/tfactory-runner-nix:latest",
        ["nix develop path:/work#default -c go build ./..."],
        repo_pvc="aifactory-data",
        repo_subpath="ws/proj/.aifactory/worktrees/tasks/t",
        nix_store_pvc="aifactory-nix-store",
    )
    t = m["spec"]["template"]["spec"]
    # store volume present alongside the repo co-mount
    vols = {v["name"]: v for v in t["volumes"]}
    assert (
        vols["nix-store"]["persistentVolumeClaim"]["claimName"] == "aifactory-nix-store"
    )
    assert "repo" in vols
    # gate container mounts the warm store at /nix
    mounts = {vm["name"]: vm for vm in t["containers"][0]["volumeMounts"]}
    assert mounts["nix-store"]["mountPath"] == "/nix"
    # seed init container copies the image's /nix into the empty PVC at /warm
    init = t["initContainers"][0]
    assert init["name"] == "seed-nix-store"
    assert init["image"] == "ghcr.io/olafkfreund/tfactory-runner-nix:latest"
    assert init["volumeMounts"][0]["mountPath"] == "/warm"
    assert "/warm/store" in init["command"][-1]


def test_pvc_subpath_strips_data_root():
    root = "/home/nonroot/.aifactory"
    wt = root + "/workspaces/proj/.aifactory/worktrees/tasks/spec-x"
    assert _pvc_subpath(wt, root) == "workspaces/proj/.aifactory/worktrees/tasks/spec-x"
    assert _pvc_subpath(root, root) == ""  # PVC root itself
    assert _pvc_subpath("/tmp/elsewhere", root) is None  # outside PVC -> no mount
    assert _pvc_subpath(None, root) is None  # unset -> no mount


def test_select_runner_kubejob_backend(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SANDBOX_GATES", "1")
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "ghcr.io/x/go:1.25")
    monkeypatch.setenv("AIFACTORY_SANDBOX_BACKEND", "kubejob")

    import core.kube_sandbox as ks

    calls = {}

    class FakeKubeSandbox:
        def __init__(self, image, **kw):
            calls["image"] = image

        def run(self, commands, **kw):
            calls["commands"] = commands
            return RunResult(True, 0, "go1.25", [])

    monkeypatch.setattr(ks, "KubeJobSandbox", FakeKubeSandbox)

    runner = _select_runner()
    assert runner is not _default_runner
    code, out = runner(["go", "version"], Path("/work"))
    assert code == 0 and out == "go1.25"
    assert calls["image"] == "ghcr.io/x/go:1.25"
    assert calls["commands"] == ["go version"]  # argv shlex-joined


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
        def run(self, *a, **k):
            raise RuntimeError("api down")

    monkeypatch.setattr(ks, "KubeJobSandbox", Boom)
    code, out = _select_runner()(["x"], Path("/w"))
    assert code == 1 and "kube-sandbox error" in out


# --- RFC-0005: real container exit code (not the Job's synthetic 0/1) ---
from types import SimpleNamespace  # noqa: E402

from core.kube_sandbox import _exit_code_from_pod  # noqa: E402


def _pod(exit_code):
    term = SimpleNamespace(exit_code=exit_code) if exit_code is not None else None
    state = SimpleNamespace(terminated=term)
    cs = SimpleNamespace(state=state)
    return SimpleNamespace(status=SimpleNamespace(container_statuses=[cs]))


def test_exit_code_reads_real_container_code():
    # Job flag says succeeded, but the container actually exited 2 → report 2.
    assert _exit_code_from_pod(_pod(2), job_succeeded=True) == (False, 2)
    # Real zero exit → succeeded True.
    assert _exit_code_from_pod(_pod(0), job_succeeded=False) == (True, 0)


def test_exit_code_falls_back_to_job_flag_when_no_terminated_state():
    # No terminated state available → synthetic fallback from the Job flag.
    assert _exit_code_from_pod(_pod(None), job_succeeded=True) == (True, 0)
    assert _exit_code_from_pod(
        SimpleNamespace(status=SimpleNamespace(container_statuses=None)),
        job_succeeded=False,
    ) == (False, 1)
