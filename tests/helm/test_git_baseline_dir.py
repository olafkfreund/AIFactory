"""Git metadata baselines persist with the workspaces PVC (#1746).

Without workspaces the clones and the baselines share the aifactory-data
emptyDir, so they are lost together. With workspaces the clones persist, so
the baselines must too, or a pod restart re-baselines a tampered repo.
"""

from __future__ import annotations

import subprocess

import pytest
import yaml


def _env(chart_dir, *sets: str) -> dict[str, str]:
    cmd = [
        "helm",
        "template",
        "t",
        str(chart_dir),
        "--set",
        "postgres.externalSecretName=pg",
    ]
    for kv in sets:
        cmd += ["--set", kv]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)  # noqa: S603
    dep = next(
        d for d in yaml.safe_load_all(out.stdout) if d and d.get("kind") == "Deployment"
    )
    env = dep["spec"]["template"]["spec"]["containers"][0].get("env", [])
    return {e["name"]: e.get("value") for e in env}


@pytest.mark.helm
def test_baseline_dir_on_workspaces_pvc(helm_available, chart_dir) -> None:
    env = _env(chart_dir, "workspaces.enabled=true", "workspaces.mountPath=/data/ws/")
    assert env["AIFACTORY_GIT_BASELINE_DIR"] == "/data/ws/.git-baselines"


@pytest.mark.helm
def test_baseline_dir_unset_without_workspaces(helm_available, chart_dir) -> None:
    assert "AIFACTORY_GIT_BASELINE_DIR" not in _env(chart_dir)
