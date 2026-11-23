"""Investigation Jobs contain only the capabilities granted by their role."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "web-server"))

from core.job_dispatch import assert_job_policy
from server.services import investigation_manifest as manifests


@pytest.fixture
def run(monkeypatch):
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data")
    monkeypatch.setattr(os.path, "ismount", lambda _: True)
    return {
        "id": UUID("12345678-1234-1234-1234-123456789abc"),
        "limits": {"minutes": 30},
        "created_at": datetime.now(UTC),
        "test_command": "pytest -q",
        "claude_token": "credential",
        "run_token": "run-credential",
    }


def test_manifests_are_hardened_and_have_exact_env(run, monkeypatch):
    monkeypatch.setenv("FACTORY_GCP_CREDS_SECRET", "gcp-secret")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otel.example")
    for manifest, expected_env in (
        (
            manifests.agent_manifest(run),
            {
                "JOB_ID",
                "AIFACTORY_INVESTIGATION_ID",
                "AIFACTORY_API_URL",
                "HOME",
                "CLAUDE_CONFIG_DIR",
                "PYTHONPATH",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "AIFACTORY_RUN_TOKEN",
            },
        ),
        (
            manifests.test_manifest(run, 1),
            {"HTTPS_PROXY", "HTTP_PROXY", "CI", "TEST_COMMAND"},
        ),
    ):
        assert_job_policy(manifest)
        manifests.assert_investigation_manifest(manifest)
        pod = manifest["spec"]["template"]
        spec = pod["spec"]
        assert {e["name"] for e in spec["containers"][0]["env"]} == expected_env
        assert "app.kubernetes.io/instance" not in pod["metadata"]["labels"]
        assert pod["metadata"]["labels"]["factory.io/investigation"] == str(run["id"])
        assert spec["automountServiceAccountToken"] is False
        assert spec["enableServiceLinks"] is False
        assert spec["terminationGracePeriodSeconds"] == 10
        assert all(
            c["securityContext"]["readOnlyRootFilesystem"]
            for c in [*spec["containers"], *spec.get("initContainers", [])]
        )
        assert "gcp-secret" not in str(manifest)
        assert "OTEL_" not in str(manifest)
        assert (
            spec["volumes"][0]["persistentVolumeClaim"]["claimName"] == "aifactory-data"
        )
        assert (
            spec["containers"][0]["volumeMounts"][0]["subPath"]
            == f"investigations/{run['id']}/src"
        )
    agent = manifests.agent_manifest(run)["spec"]["template"]["spec"]["containers"][0]
    assert agent["workingDir"] != "/src"
    assert "-P" in agent["command"]


def test_guard_rejects_extra_inputs(run):
    agent = manifests.agent_manifest(run)
    for change in (
        lambda m: m["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "DATABASE_URL", "value": "secret"}
        ),
        lambda m: m["spec"]["template"]["spec"]["volumes"].append(
            {"name": "host", "hostPath": {"path": "/"}}
        ),
        lambda m: m["spec"]["template"]["spec"]["containers"][0].update(
            envFrom=[{"secretRef": {"name": "other"}}]
        ),
        lambda m: m["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][
            0
        ].update(mountPropagation="HostToContainer"),
        lambda m: m["spec"]["template"]["spec"]["volumes"][0][
            "persistentVolumeClaim"
        ].update(claimName="other"),
        lambda m: m["spec"]["template"]["spec"].update(
            automountServiceAccountToken=True
        ),
        lambda m: m["spec"]["template"]["spec"].update(enableServiceLinks=True),
        lambda m: m["spec"]["template"]["spec"].pop("enableServiceLinks"),
        lambda m: m["spec"]["template"]["metadata"]["labels"].update(
            {"app.kubernetes.io/instance": "aifactory"}
        ),
    ):
        unsafe = copy.deepcopy(agent)
        change(unsafe)
        with pytest.raises(manifests.ManifestRefused):
            manifests.assert_investigation_manifest(unsafe)


def test_guard_rejects_literal_credential(run):
    agent = manifests.agent_manifest(run)
    entries = agent["spec"]["template"]["spec"]["containers"][0]["env"]
    entry = next(e for e in entries if e["name"] == "CLAUDE_CODE_OAUTH_TOKEN")
    entry.pop("valueFrom")
    entry["value"] = "literal"
    with pytest.raises(manifests.ManifestRefused):
        manifests.assert_investigation_manifest(agent)


def test_secret_owned_by_agent_job(run):
    secret = manifests.secret_manifest(run, "uid-123")
    assert secret["metadata"]["ownerReferences"][0]["uid"] == "uid-123"
    assert secret["metadata"]["ownerReferences"][0]["name"] == "aif-inv-12345678-agent"


def test_shared_storage_is_required(run, monkeypatch):
    monkeypatch.delenv("AIFACTORY_SANDBOX_REPO_PVC")
    with pytest.raises(ValueError, match="shared storage unavailable"):
        manifests.agent_manifest(run)


def _policies():
    root = Path(__file__).resolve().parents[1]
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "aifactory",
            str(root / "charts/aifactory"),
            "--namespace",
            "factory",
            "--set",
            "investigations.enabled=true",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [
        doc
        for doc in yaml.safe_load_all(rendered)
        if doc and doc.get("kind") == "NetworkPolicy"
    ]


def test_network_check_accepts_chart_and_rejects_broad_policies(run):
    agent = manifests.agent_manifest(run)["spec"]["template"]["metadata"]["labels"]
    test = manifests.test_manifest(run, 1)["spec"]["template"]["metadata"]["labels"]
    policies = _policies()
    assert manifests.network_check(policies, agent, test) is None

    broad = {
        "metadata": {"name": "unsafe-build"},
        "spec": {"podSelector": {"matchLabels": {"factory.io/kind": "task"}}},
    }
    assert manifests.network_check([*policies, broad], agent, test) == "unsafe-build"
    # Server ingress is reachability, not isolation: a cluster that does not
    # isolate the server needs no investigate-api policy (deviation 13).
    assert (
        manifests.network_check(
            [
                p
                for p in policies
                if not p["metadata"]["name"].endswith("-investigate-api")
            ],
            agent,
            test,
        )
        is None
    )
    build = next(p for p in policies if p["metadata"]["name"].endswith("-tasks"))
    old = copy.deepcopy(build)
    del old["spec"]["podSelector"]["matchExpressions"]
    assert (
        manifests.network_check(
            [p for p in policies if p is not build] + [old], agent, test
        )
        == old["metadata"]["name"]
    )


@pytest.mark.parametrize(
    ("expression", "selected"),
    [
        (
            {
                "key": "factory.io/run-kind",
                "operator": "In",
                "values": ["investigate-agent"],
            },
            True,
        ),
        (
            {
                "key": "factory.io/run-kind",
                "operator": "NotIn",
                "values": ["investigate-test"],
            },
            True,
        ),
        ({"key": "factory.io/run-kind", "operator": "Exists"}, True),
        ({"key": "factory.io/run-kind", "operator": "DoesNotExist"}, False),
    ],
)
def test_selector_expressions(expression, selected):
    assert (
        manifests._selected(
            {"matchExpressions": [expression]},
            {"factory.io/run-kind": "investigate-agent"},
        )
        is selected
    )


def test_guard_raises_a_typed_refusal(monkeypatch):
    from server.services import investigation_manifest as im

    bad = {"kind": "Job", "metadata": {"name": "x"}, "spec": {}}
    with pytest.raises(im.ManifestRefused):
        im.assert_investigation_manifest(bad)


def test_agent_pythonpath_makes_the_entry_point_importable(run, tmp_path):
    """`-P` keeps the cwd off sys.path; PYTHONPATH must name the backend root (#2190)."""
    import subprocess
    import sys
    from pathlib import Path

    container = manifests.agent_manifest(run)["spec"]["template"]["spec"]["containers"][
        0
    ]
    env = {e["name"]: e.get("value") for e in container["env"]}
    image_root = "/home/projects/MagesticAI/"
    assert env["PYTHONPATH"].startswith(image_root)
    assert container["command"][:3] == ["python", "-P", "-m"]
    local = Path(__file__).resolve().parents[1] / env["PYTHONPATH"].removeprefix(
        image_root
    )
    probe = subprocess.run(
        [
            sys.executable,
            "-P",
            "-c",
            "import importlib.util as u; assert u.find_spec('agents.investigate')",
        ],
        cwd=tmp_path,
        env={"PYTHONPATH": str(local)},
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_copy_source_leaves_the_work_root_alone(run, tmp_path):
    """/work is a root-owned emptyDir: copying must not set its mode or times (#2203)."""
    init = manifests.test_manifest(run, 1)["spec"]["template"]["spec"][
        "initContainers"
    ][0]
    assert init["name"] == "copy-source"
    src, work = tmp_path / "src", tmp_path / "work"
    (src / "pkg").mkdir(parents=True)
    (src / ".env.example").write_text("x")
    (src / "pkg" / "a.py").write_text("y")
    (src / "link").symlink_to("/etc/passwd")
    src.chmod(0o700)
    work.mkdir()
    work.chmod(0o777)
    script = init["command"][2].replace("/src", str(src)).replace("/work", str(work))
    subprocess.run(["sh", "-c", script], check=True)
    assert work.stat().st_mode & 0o7777 == 0o777
    assert (work / ".env.example").read_text() == "x"
    assert (work / "pkg" / "a.py").read_text() == "y"
    assert (work / "link").is_symlink()
