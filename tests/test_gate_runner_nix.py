"""RFC-0005 Tier A: nixjob gate backend wraps gates in `nix develop path:/work`."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from agents.gate_runner import _nix_wrap, _select_runner  # noqa: E402
from core.nix_env import (  # noqa: E402
    environment_of,
    is_nix_environment,
    materialize_flake_into,
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
