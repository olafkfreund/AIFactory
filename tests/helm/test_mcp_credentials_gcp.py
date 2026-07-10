"""Helm acceptance tests for the GCP MCP credential slot (issue #168).

Verifies:
- ``mcpCredentials.providers.gcp=true`` mounts SA JSON and sets GOOGLE_APPLICATION_CREDENTIALS
- ``mcpCredentials.gcp.secretName`` override uses a dedicated Secret instead of the shared one
- ``mcpCredentials.gcp.endpointOverride`` injects GCP_MCP_ENDPOINT env var
- When endpointOverride is empty, GCP_MCP_ENDPOINT is NOT injected (Python default used)
"""

from __future__ import annotations

import subprocess

import pytest
import yaml


def _render(chart_dir, set_values: list[str] | None = None) -> list[dict]:
    cmd = ["helm", "template", "test-release", str(chart_dir)]
    for kv in set_values or []:
        cmd.extend(["--set", kv])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _find_deployment(docs: list[dict]) -> dict:
    for d in docs:
        if d.get("kind") == "Deployment":
            return d
    raise AssertionError("no Deployment in rendered manifests")


def _container_envs(deployment: dict) -> list[dict]:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    assert containers
    return containers[0].get("env", [])


def _env_value(deployment: dict, name: str) -> str | None:
    for e in _container_envs(deployment):
        if e["name"] == name:
            return e.get("value")
    return None


def _volume_names(deployment: dict) -> set[str]:
    return {
        v["name"] for v in deployment["spec"]["template"]["spec"].get("volumes", [])
    }


def _find_volume(deployment: dict, name: str) -> dict:
    for v in deployment["spec"]["template"]["spec"]["volumes"]:
        if v["name"] == name:
            return v
    raise AssertionError(f"volume {name!r} not found")


def _find_mount(deployment: dict, name: str) -> dict:
    for m in deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]:
        if m["name"] == name:
            return m
    raise AssertionError(f"volumeMount {name!r} not found")


@pytest.fixture
def chart_dir():
    from pathlib import Path

    return Path(__file__).parent.parent.parent / "charts" / "aifactory"


# ---------------------------------------------------------------------------
# File mount + GOOGLE_APPLICATION_CREDENTIALS
# ---------------------------------------------------------------------------


@pytest.mark.helm
def test_gcp_provider_mounts_sa_json(chart_dir):
    """providers.gcp=true mounts the SA JSON at /etc/aifactory/gcp-sa.json."""
    docs = _render(
        chart_dir,
        set_values=[
            "mcpCredentials.enabled=true",
            "mcpCredentials.providers.gcp=true",
            "mcpCredentials.secretName=shared-secret",
        ],
    )
    deployment = _find_deployment(docs)
    mount = _find_mount(deployment, "mcp-gcp-sa")
    assert mount["mountPath"] == "/etc/aifactory/gcp-sa.json"
    assert mount["subPath"] == "gcp-service-account.json"
    assert mount["readOnly"] is True


@pytest.mark.helm
def test_gcp_provider_sets_GOOGLE_APPLICATION_CREDENTIALS(chart_dir):
    docs = _render(
        chart_dir,
        set_values=[
            "mcpCredentials.enabled=true",
            "mcpCredentials.providers.gcp=true",
            "mcpCredentials.secretName=shared-secret",
        ],
    )
    deployment = _find_deployment(docs)
    val = _env_value(deployment, "GOOGLE_APPLICATION_CREDENTIALS")
    assert val == "/etc/aifactory/gcp-sa.json", (
        f"GOOGLE_APPLICATION_CREDENTIALS={val!r}, expected '/etc/aifactory/gcp-sa.json'"
    )


@pytest.mark.helm
def test_gcp_volume_uses_shared_secret_by_default(chart_dir):
    """Without mcpCredentials.gcp.secretName, the shared secretName is used."""
    docs = _render(
        chart_dir,
        set_values=[
            "mcpCredentials.enabled=true",
            "mcpCredentials.providers.gcp=true",
            "mcpCredentials.secretName=shared-secret",
        ],
    )
    deployment = _find_deployment(docs)
    vol = _find_volume(deployment, "mcp-gcp-sa")
    assert vol["secret"]["secretName"] == "shared-secret"
    assert vol["secret"]["defaultMode"] == 256  # 0400 in decimal


# ---------------------------------------------------------------------------
# mcpCredentials.gcp.secretName override
# ---------------------------------------------------------------------------


@pytest.mark.helm
def test_helm_template_renders_gcp_mcp_secret_mount(chart_dir):
    """mcpCredentials.gcp.secretName overrides the shared Secret for the GCP volume."""
    docs = _render(
        chart_dir,
        set_values=[
            "mcpCredentials.enabled=true",
            "mcpCredentials.providers.gcp=true",
            "mcpCredentials.secretName=shared-secret",
            "mcpCredentials.gcp.secretName=gcp-creds-secret",
        ],
    )
    deployment = _find_deployment(docs)
    vol = _find_volume(deployment, "mcp-gcp-sa")
    # The GCP-specific secretName must take priority
    assert vol["secret"]["secretName"] == "gcp-creds-secret", (
        f"Expected gcp-creds-secret, got {vol['secret']['secretName']!r}"
    )


@pytest.mark.helm
def test_gcp_secret_name_override_does_not_affect_other_volumes(chart_dir):
    """The GCP secretName override must NOT change the AWS / K8s volume source."""
    docs = _render(
        chart_dir,
        set_values=[
            "mcpCredentials.enabled=true",
            "mcpCredentials.providers.gcp=true",
            "mcpCredentials.providers.aws=true",
            "mcpCredentials.secretName=shared-secret",
            "mcpCredentials.gcp.secretName=gcp-only-secret",
        ],
    )
    deployment = _find_deployment(docs)
    aws_vol = _find_volume(deployment, "mcp-aws-credentials")
    assert aws_vol["secret"]["secretName"] == "shared-secret"


# ---------------------------------------------------------------------------
# mcpCredentials.gcp.endpointOverride
# ---------------------------------------------------------------------------


@pytest.mark.helm
def test_gcp_endpoint_override_injects_GCP_MCP_ENDPOINT(chart_dir):
    """endpointOverride wires GCP_MCP_ENDPOINT env var into the pod."""
    staging_url = (
        "https://cloudaicompanion-staging.googleapis.com/v1/extensions/default/mcp"
    )
    docs = _render(
        chart_dir,
        set_values=[
            "mcpCredentials.enabled=true",
            "mcpCredentials.providers.gcp=true",
            "mcpCredentials.secretName=shared-secret",
            f"mcpCredentials.gcp.endpointOverride={staging_url}",
        ],
    )
    deployment = _find_deployment(docs)
    val = _env_value(deployment, "GCP_MCP_ENDPOINT")
    assert val == staging_url, f"GCP_MCP_ENDPOINT={val!r}, expected {staging_url!r}"


@pytest.mark.helm
def test_gcp_no_endpoint_override_does_not_inject_GCP_MCP_ENDPOINT(chart_dir):
    """When endpointOverride is empty, GCP_MCP_ENDPOINT must NOT appear in env."""
    docs = _render(
        chart_dir,
        set_values=[
            "mcpCredentials.enabled=true",
            "mcpCredentials.providers.gcp=true",
            "mcpCredentials.secretName=shared-secret",
        ],
    )
    deployment = _find_deployment(docs)
    env_names = [e["name"] for e in _container_envs(deployment)]
    assert "GCP_MCP_ENDPOINT" not in env_names, (
        "GCP_MCP_ENDPOINT must not be set when endpointOverride is empty — "
        "the Python module's default GA endpoint is used instead"
    )


# ---------------------------------------------------------------------------
# Disabled guard
# ---------------------------------------------------------------------------


@pytest.mark.helm
def test_gcp_not_mounted_when_providers_gcp_false(chart_dir):
    """providers.gcp=false (default) — no GCP volume or GOOGLE_APPLICATION_CREDENTIALS."""
    docs = _render(
        chart_dir,
        set_values=[
            "mcpCredentials.enabled=true",
            "mcpCredentials.secretName=shared-secret",
            # providers.gcp intentionally omitted (defaults to false)
        ],
    )
    deployment = _find_deployment(docs)
    vols = _volume_names(deployment)
    assert "mcp-gcp-sa" not in vols

    env_names = [e["name"] for e in _container_envs(deployment)]
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in env_names
