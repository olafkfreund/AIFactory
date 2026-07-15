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
    _nix_kube_runner("ghcr.io/x/nix:latest")(["pytest", "-q"], Path("/work"))
    assert seen["nix_store_pvc"] is None, seen
    assert seen["repo_pvc"] == "aifactory-data", seen  # repo co-mount unchanged


def test_gate_keeps_warm_store_pvc_when_flag_off(monkeypatch):
    """Default OFF stays warm — this fix must not silently drop the cache."""
    seen = _capture_sandbox(monkeypatch)
    monkeypatch.setenv("AIFACTORY_NIX_STORE_PVC", "aifactory-nix-store")
    monkeypatch.delenv("AIFACTORY_PACKED_NIX_IN_IMAGE", raising=False)
    _nix_kube_runner("ghcr.io/x/nix:latest")(["pytest", "-q"], Path("/work"))
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
