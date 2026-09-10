"""RFC-0005 Tier A: nixjob gate backend wraps gates in `nix develop path:/work`."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from types import SimpleNamespace  # noqa: E402

from agents.gate_runner import _nix_kube_runner, _nix_wrap, _select_runner  # noqa: E402
from core.nix_env import (  # noqa: E402
    environment_of,
    is_nix_environment,
    materialize_flake_into,
    nix_in_image,
)

_NIX_ENV = {
    "language": "python",
    "system_packages": ["chromium"],
    "verify_commands": ["pytest -q"],
    "provisioning": {"method": "nix", "ref": "flake.nix", "generated": True},
}


def test_nix_wrap_uses_path_ref():
    argv = _nix_wrap(["pytest", "-q"])
    assert argv[:3] == ["nix", "develop", "path:/work#default"], argv
    assert argv[-3:] == ["bash", "-c", "pytest -q"], argv


def test_nixjob_backend_selected(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SANDBOX_GATES", "1")
    monkeypatch.setenv("AIFACTORY_SANDBOX_IMAGE", "ghcr.io/x/nix:latest")
    monkeypatch.setenv("AIFACTORY_SANDBOX_BACKEND", "nixjob")
    runner = _select_runner()
    # nixjob returns the inner closure (not the default/host runner)
    assert callable(runner) and runner.__qualname__.startswith("_nix_kube_runner")


def test_materialize_flake_into(tmp_path):
    assert is_nix_environment(_NIX_ENV)
    assert materialize_flake_into(tmp_path, _NIX_ENV) is True
    flake = (tmp_path / "flake.nix").read_text()
    assert "playwright-test" in flake and "FONTCONFIG_FILE" in flake, flake


def test_materialize_noop_for_non_nix(tmp_path):
    assert (
        materialize_flake_into(tmp_path, {"provisioning": {"method": "image"}}) is False
    )
    assert not (tmp_path / "flake.nix").exists()


def test_environment_of():
    assert environment_of({"environment": _NIX_ENV}) == _NIX_ENV
    assert environment_of({}) is None
    assert environment_of(None) is None


def test_repo_owned_flake_respected(tmp_path):
    (tmp_path / "flake.nix").write_text("# hand-written\n")
    env = dict(_NIX_ENV, provisioning={"method": "nix", "generated": False})
    assert materialize_flake_into(tmp_path, env) is True
    assert (tmp_path / "flake.nix").read_text() == "# hand-written\n"  # not overwritten


# A worktree that really is on the data PVC. `/work` is NOT: on the packed path
# it is a pod-local emptyDir, and a gate there runs locally instead of being
# dispatched to a Job that would see no repo (AIFactory#1491).
_MOUNTABLE = Path("/home/nonroot/.aifactory/workspaces/p/worktrees/tasks/007")


def _capture_sandbox(monkeypatch) -> dict:
    """Swap KubeJobSandbox for a recorder; returns the kwargs it was built with."""
    seen: dict = {}

    class _FakeSandbox:
        def __init__(self, image, **kwargs):
            seen["image"] = image
            seen.update(kwargs)

        def run(self, *_a, **_k):
            return SimpleNamespace(ok=True, exit_code=0, output="ok")

    import core.kube_sandbox as ks

    monkeypatch.setattr(ks, "KubeJobSandbox", _FakeSandbox)
    return seen


def test_gate_drops_warm_store_pvc_when_nix_in_image(monkeypatch):
    """#253: with /nix baked into the image the gate Job must NOT mount the warm
    store. It already mounts the RWO repo PVC; the RWO nix-store PVC strands its
    PV on whichever node first consumed it, and when the two land on different
    nodes (the live cluster: data on the server, nix-store on the agent) no node
    satisfies both and the pod is unschedulable forever.
    """
    seen = _capture_sandbox(monkeypatch)
    monkeypatch.setenv("AIFACTORY_NIX_STORE_PVC", "aifactory-nix-store")
    monkeypatch.setenv("AIFACTORY_PACKED_NIX_IN_IMAGE", "true")
    _nix_kube_runner("ghcr.io/x/nix:latest")(["pytest", "-q"], _MOUNTABLE)
    assert seen["nix_store_pvc"] is None, seen
    assert seen["repo_pvc"] == "aifactory-data", seen  # repo co-mount unchanged


def test_gate_keeps_warm_store_pvc_when_flag_off(monkeypatch):
    """Default OFF stays warm — this fix must not silently drop the cache."""
    seen = _capture_sandbox(monkeypatch)
    monkeypatch.setenv("AIFACTORY_NIX_STORE_PVC", "aifactory-nix-store")
    monkeypatch.delenv("AIFACTORY_PACKED_NIX_IN_IMAGE", raising=False)
    _nix_kube_runner("ghcr.io/x/nix:latest")(["pytest", "-q"], _MOUNTABLE)
    assert seen["nix_store_pvc"] == "aifactory-nix-store", seen


def test_nix_in_image_flag_parsing(monkeypatch):
    monkeypatch.delenv("AIFACTORY_PACKED_NIX_IN_IMAGE", raising=False)
    assert nix_in_image() is False
    for on in ("1", "true", "TRUE", " yes ", "on"):
        monkeypatch.setenv("AIFACTORY_PACKED_NIX_IN_IMAGE", on)
        assert nix_in_image() is True, on
    for off in ("", "0", "false", "no"):
        monkeypatch.setenv("AIFACTORY_PACKED_NIX_IN_IMAGE", off)
        assert nix_in_image() is False, off


def test_repo_is_mountable_only_under_the_data_root():
    """AIFactory#1491: the packed path's /work is a pod-local emptyDir."""
    from core.kube_sandbox import repo_is_mountable

    root = "/home/nonroot/.aifactory"
    assert repo_is_mountable(f"{root}/workspaces/p/worktrees/tasks/007", root)
    # What the build Job actually reports as its cwd — no other pod can see it.
    assert not repo_is_mountable("/work/.aifactory/worktrees/tasks/007", root)
    assert not repo_is_mountable(None, root)


def test_unmountable_repo_runs_the_nix_shell_locally(monkeypatch, tmp_path):
    """A gate must never be dispatched to a Job that cannot see the code.

    Doing so runs the build tool against an empty directory and reports the
    resulting non-zero as if it had tested the repo (AIFactory#1491).
    """
    import agents.gate_runner as gr

    (tmp_path / "flake.nix").write_text("{}")
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", "/home/nonroot/.aifactory")

    dispatched: list[object] = []
    import core.kube_sandbox as ks

    monkeypatch.setattr(
        ks, "KubeJobSandbox", lambda *a, **k: dispatched.append(a) or None
    )
    seen: dict[str, object] = {}

    def fake_default(command, cwd):
        seen["command"], seen["cwd"] = command, cwd
        return 0, "BUILD SUCCESSFUL"

    monkeypatch.setattr(gr, "_default_runner", fake_default)

    exit_code, output = gr._nix_kube_runner("img")(["gradle", "test"], tmp_path)

    assert not dispatched, "dispatched a Job that could not mount the repo"
    assert (exit_code, output) == (0, "BUILD SUCCESSFUL")
    # It still runs inside the per-task dev shell, rooted at the real flake.
    assert seen["command"][:3] == ["nix", "develop", f"path:{tmp_path}#default"]
    assert seen["cwd"] == tmp_path


def test_mountable_repo_still_dispatches_a_job(monkeypatch, tmp_path):
    """The co-mount path is unchanged — the fallback is not a silent takeover."""
    import agents.gate_runner as gr

    root = tmp_path / "data"
    work = root / "workspaces" / "p" / "worktrees" / "tasks" / "007"
    work.mkdir(parents=True)
    (work / "flake.nix").write_text("{}")
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", str(root))

    class FakeSandbox:
        def __init__(self, *a, **k):
            pass

        def run(self, *a, **k):
            return SimpleNamespace(ok=True, exit_code=0, output="ran in a Job")

    import core.kube_sandbox as ks

    monkeypatch.setattr(ks, "KubeJobSandbox", FakeSandbox)
    monkeypatch.setattr(
        gr,
        "_default_runner",
        lambda *a: (_ for _ in ()).throw(AssertionError("ran locally")),
    )

    assert gr._nix_kube_runner("img")(["gradle", "test"], work) == (0, "ran in a Job")


def test_packed_workspace_manifest_unpacks_into_an_emptydir():
    """#1524: the gate image has neither the AIFactory code nor store creds, so
    the unpack runs in an initContainer on the build image."""
    from core.kube_sandbox import build_job_manifest

    spec = build_job_manifest(
        "n",
        "gate-image",
        ["true"],
        workspace_uri="s3://b/w.tar.gz",
        unpack_image="aifactory:sha-x-nix",
        store_env={"S3_ENDPOINT": "http://minio:9000"},
    )["spec"]["template"]["spec"]

    (init,) = spec["initContainers"]
    assert init["image"] == "aifactory:sha-x-nix", "unpack must not use the gate image"
    assert init["env"] == [{"name": "S3_ENDPOINT", "value": "http://minio:9000"}]
    assert "s3://b/w.tar.gz" in init["command"]
    # The path to the code inside the image comes from the image itself; a
    # hardcoded guess was wrong (it is not /app).
    assert "APP_BACKEND_PATH" in " ".join(init["command"])
    # Shared scratch, not a PVC — a PVC subPath is exactly what cannot reach it.
    assert spec["volumes"] == [{"name": "repo", "emptyDir": {}}]
    assert spec["containers"][0]["workingDir"] == "/work"


def test_unmountable_repo_with_a_store_dispatches_a_gate_job(monkeypatch, tmp_path):
    """The code is sent TO the gate image, which has the toolchain closure.

    Running in-process instead makes nix build gcc from source: the build
    image's store carries no Kotlin/Gradle closure and the Job has no egress
    (#1524), so that path cannot reach a green gate.
    """
    import agents.gate_runner as gr
    import core.kube_sandbox as ks

    (tmp_path / "flake.nix").write_text("{}")
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", "/home/nonroot/.aifactory")
    monkeypatch.setenv("S3_ENDPOINT", "http://minio:9000")
    monkeypatch.setenv("AIFACTORY_BUILD_IMAGE", "aifactory:sha-x-nix")
    monkeypatch.setattr(gr, "_packed_workspace_for", lambda _root: "s3://b/w.tar.gz")
    monkeypatch.setattr(
        gr,
        "_default_runner",
        lambda *a: (_ for _ in ()).throw(AssertionError("ran in-process")),
    )

    seen: dict = {}

    class FakeSandbox:
        def __init__(self, image, **kw):
            seen["image"] = image
            seen.update(kw)

        def run(self, *_a, **_k):
            return SimpleNamespace(ok=True, exit_code=0, output="BUILD SUCCESSFUL")

    monkeypatch.setattr(ks, "KubeJobSandbox", FakeSandbox)

    assert gr._nix_kube_runner("gate-image")(["gradle", "test"], tmp_path) == (
        0,
        "BUILD SUCCESSFUL",
    )
    assert seen["workspace_uri"] == "s3://b/w.tar.gz"
    assert seen["unpack_image"] == "aifactory:sha-x-nix"
    assert seen["store_env"]["S3_ENDPOINT"] == "http://minio:9000"
    # No repo_pvc: a PVC co-mount is what this path exists to avoid.
    assert "repo_pvc" not in seen


def test_packing_is_skipped_without_an_object_store(monkeypatch, tmp_path):
    """No store configured → no URI to hand a Job; keep the in-process attempt."""
    import agents.gate_runner as gr

    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    assert gr._packed_workspace_for(tmp_path) is None


def test_packed_workspace_refuses_to_unpack_with_the_gate_image():
    """A fallback that cannot work is worse than a refusal.

    The gate image is defined as the one WITHOUT AIFactory code or store
    credentials, so defaulting the unpack to it would turn a misconfiguration
    into a gate failure blamed on the code under test.
    """
    import pytest
    from core.kube_sandbox import build_job_manifest

    with pytest.raises(ValueError, match="unpack_image"):
        build_job_manifest("n", "gate-image", ["true"], workspace_uri="s3://b/w.tar.gz")


def test_no_build_image_falls_back_instead_of_dispatching(monkeypatch, tmp_path):
    """Without AIFACTORY_BUILD_IMAGE there is no image that can unpack, so the
    packed path is not attempted at all."""
    import agents.gate_runner as gr
    import core.kube_sandbox as ks

    (tmp_path / "flake.nix").write_text("{}")
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", "/home/nonroot/.aifactory")
    monkeypatch.setenv("S3_ENDPOINT", "http://minio:9000")
    monkeypatch.delenv("AIFACTORY_BUILD_IMAGE", raising=False)
    monkeypatch.setattr(
        gr,
        "_packed_workspace_for",
        lambda _r: (_ for _ in ()).throw(AssertionError("packed without an image")),
    )
    monkeypatch.setattr(
        ks,
        "KubeJobSandbox",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dispatched")),
    )
    monkeypatch.setattr(gr, "_default_runner", lambda *a: (0, "ran in-process"))

    assert gr._nix_kube_runner("gate-image")(["gradle", "test"], tmp_path) == (
        0,
        "ran in-process",
    )


def test_gate_timeout_is_configurable():
    """Every gate cold-fetches its closure; the budget must be raisable without
    a release (#1541). Parsed from a mapping, so no module reload is needed —
    reloading swaps module identity and breaks sibling tests' monkeypatching."""
    from agents.gate_runner import _timeout_from_env

    assert _timeout_from_env({}) == 600
    assert _timeout_from_env({"AIFACTORY_GATE_TIMEOUT_SECONDS": "1800"}) == 1800
    # Junk must not crash a build; fall back to the default.
    assert _timeout_from_env({"AIFACTORY_GATE_TIMEOUT_SECONDS": "soon"}) == 600
    assert _timeout_from_env({"AIFACTORY_GATE_TIMEOUT_SECONDS": ""}) == 600


def test_packed_path_uses_the_warm_store_even_with_nix_in_image(monkeypatch, tmp_path):
    """#1541: the runner image bakes NO language closures, so without a
    persistent store every gate re-downloads its whole toolchain and Swift never
    finishes inside the Job deadline.

    #253 dropped the warm store because the pod already mounted the RWO repo
    PVC and two RWO PVs stranded on different nodes. The packed path mounts no
    repo PVC at all — the code arrives in an emptyDir — so the nix store is the
    pod's only PVC and has nothing to strand against.
    """
    import agents.gate_runner as gr
    import core.kube_sandbox as ks

    (tmp_path / "flake.nix").write_text("{}")
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", "/home/nonroot/.aifactory")
    monkeypatch.setenv("S3_ENDPOINT", "http://minio:9000")
    monkeypatch.setenv("AIFACTORY_BUILD_IMAGE", "aifactory:sha-x-nix")
    monkeypatch.setenv("AIFACTORY_NIX_STORE_PVC", "aifactory-nix-store")
    # Nix IS in the image — the old rule dropped the warm store on that alone.
    monkeypatch.setenv("AIFACTORY_PACKED_NIX_IN_IMAGE", "true")
    monkeypatch.setattr(gr, "_packed_workspace_for", lambda _r: "s3://b/w.tar.gz")

    seen: dict = {}

    class FakeSandbox:
        def __init__(self, image, **kw):
            seen.update(kw)

        def run(self, *_a, **_k):
            return SimpleNamespace(ok=True, exit_code=0, output="ok")

    monkeypatch.setattr(ks, "KubeJobSandbox", FakeSandbox)
    gr._nix_kube_runner("gate-image")(["gradle", "test"], tmp_path)

    assert seen["nix_store_pvc"] == "aifactory-nix-store"
    # And it is genuinely the only PVC on the pod.
    assert "repo_pvc" not in seen


def test_co_mount_path_still_drops_the_warm_store(monkeypatch, tmp_path):
    """#253's hazard is real where it applies: with the repo PVC mounted, a
    second RWO PVC can strand the pod unschedulable."""
    import agents.gate_runner as gr
    import core.kube_sandbox as ks

    root = tmp_path / "data"
    work = root / "workspaces" / "p" / "worktrees" / "tasks" / "007"
    work.mkdir(parents=True)
    (work / "flake.nix").write_text("{}")
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", str(root))
    monkeypatch.setenv("AIFACTORY_NIX_STORE_PVC", "aifactory-nix-store")
    monkeypatch.setenv("AIFACTORY_PACKED_NIX_IN_IMAGE", "true")

    seen: dict = {}

    class FakeSandbox:
        def __init__(self, image, **kw):
            seen.update(kw)

        def run(self, *_a, **_k):
            return SimpleNamespace(ok=True, exit_code=0, output="ok")

    monkeypatch.setattr(ks, "KubeJobSandbox", FakeSandbox)
    gr._nix_kube_runner("gate-image")(["gradle", "test"], work)

    assert seen["nix_store_pvc"] is None, "co-mount path must not add a 2nd RWO PVC"
