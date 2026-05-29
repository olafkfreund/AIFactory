"""Helm chart acceptance tests for the litellm sub-chart toggle.

Epic #35 #38 PR-3. Mirrors the shape of test_audit_anchor_toggle.py +
test_otel_toggle.py.

When ``litellm.enabled=false`` (default):
  • No LiteLLM sub-chart templates rendered.
  • No LITELLM_* env vars on the web container.
  • No Grafana ConfigMap rendered (even if monitoring.grafanaDashboards
    .enabled=true — the toggle is gated by litellm.enabled).
  • Chart byte-for-byte same as pre-#38 deployments.

When ``litellm.enabled=true`` + masterKeySecretName set:
  • Upstream litellm-helm sub-chart renders (Service + Deployment).
  • LITELLM_GATEWAY_URL set to the in-cluster Service URL.
  • LITELLM_MASTER_KEY mounted from the operator-provided Secret.
  • LITELLM_AUDIT_ENABLED + LITELLM_AUDIT_FAILURE_MODE injected.

When ``litellm.enabled=true`` + no masterKeySecretName:
  • Schema rejects with a clear error.

When ``litellm.gatewayUrl`` is set:
  • Overrides the computed in-cluster URL (external-LiteLLM mode).

When ``monitoring.grafanaDashboards.enabled=true`` + litellm.enabled=true:
  • Grafana ConfigMap rendered with the dashboard JSON inside.
"""

from __future__ import annotations

import json
import subprocess

import pytest
import yaml


def _render(chart_dir, set_values: list[str] | None = None) -> list[dict]:
    cmd = ["helm", "template", "test-release", str(chart_dir)]
    for kv in set_values or []:
        cmd.extend(["--set", kv])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _render_expect_error(
    chart_dir, set_values: list[str] | None = None,
) -> str:
    cmd = ["helm", "template", "test-release", str(chart_dir)]
    for kv in set_values or []:
        cmd.extend(["--set", kv])
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert out.returncode != 0, (
        f"expected helm template to fail; got rc=0 stdout={out.stdout[:200]}"
    )
    return out.stderr


def _find_deployment(docs: list[dict], name_contains: str = "aifactory") -> dict:
    """Find the AIFactory web pod Deployment (NOT the LiteLLM one)."""
    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        name = d.get("metadata", {}).get("name", "")
        if name_contains in name and "litellm" not in name:
            return d
    raise AssertionError(
        f"no Deployment matching '{name_contains}' (and not litellm) "
        f"in rendered chart",
    )


def _find_litellm_service(docs: list[dict]) -> dict | None:
    """The sub-chart's own Service (named `{Release}-litellm` via alias)."""
    for d in docs:
        if d.get("kind") != "Service":
            continue
        name = d.get("metadata", {}).get("name", "")
        if name == "test-release-litellm":
            return d
    return None


def _find_grafana_configmap(docs: list[dict]) -> dict | None:
    for d in docs:
        if d.get("kind") != "ConfigMap":
            continue
        labels = d.get("metadata", {}).get("labels", {})
        if labels.get("grafana_dashboard") == "1":
            return d
    return None


def _envs(deployment: dict) -> dict[str, dict]:
    """Index a Deployment's container env list by name."""
    return {
        e["name"]: e
        for e in deployment["spec"]["template"]["spec"]["containers"][0].get(
            "env", [],
        )
    }


@pytest.mark.helm
class TestLiteLLMSubchartOff:
    """Default state — chart byte-for-byte same as pre-#38."""

    def test_no_litellm_env_vars(self, helm_available, chart_dir) -> None:
        docs = _render(chart_dir, ["postgres.externalSecretName=test-pg"])
        dep = _find_deployment(docs)
        env_names = list(_envs(dep).keys())
        for forbidden in (
            "LITELLM_GATEWAY_URL",
            "LITELLM_MASTER_KEY",
            "LITELLM_AUDIT_ENABLED",
            "LITELLM_AUDIT_FAILURE_MODE",
            "LITELLM_AUDIT_FULL_TEXT",
            "LITELLM_AUDIT_EXTRA_PATTERNS",
        ):
            assert forbidden not in env_names, (
                f"{forbidden} present on web pod when litellm.enabled=false"
            )

    def test_no_litellm_service_when_disabled(
        self, helm_available, chart_dir,
    ) -> None:
        docs = _render(chart_dir, ["postgres.externalSecretName=test-pg"])
        assert _find_litellm_service(docs) is None, (
            "LiteLLM sub-chart Service should not render with default values"
        )

    def test_no_grafana_configmap_when_disabled(
        self, helm_available, chart_dir,
    ) -> None:
        # Even if monitoring.grafanaDashboards.enabled=true on its own,
        # the ConfigMap is gated by litellm.enabled (no point shipping
        # the dashboard for a feature that isn't deployed).
        docs = _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "monitoring.grafanaDashboards.enabled=true",
            ],
        )
        assert _find_grafana_configmap(docs) is None


@pytest.mark.helm
class TestLiteLLMSubchartOnInCluster:
    """litellm.enabled=true + masterKeySecretName → sub-chart deploys
    + env vars wire to in-cluster Service URL."""

    def _docs_on(self, chart_dir) -> list[dict]:
        return _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "litellm.enabled=true",
                "litellm.masterKeySecretName=ll-key",
            ],
        )

    def test_litellm_service_rendered(
        self, helm_available, chart_dir,
    ) -> None:
        svc = _find_litellm_service(self._docs_on(chart_dir))
        assert svc is not None, (
            "LiteLLM sub-chart Service not rendered when enabled=true"
        )
        # Upstream chart defaults port 4000.
        ports = svc["spec"]["ports"]
        assert any(p.get("port") == 4000 for p in ports), (
            f"expected port 4000 on LiteLLM Service; got {ports}"
        )

    def test_web_pod_gateway_url_points_at_service(
        self, helm_available, chart_dir,
    ) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = _envs(dep)
        assert "LITELLM_GATEWAY_URL" in env
        # Sub-chart Service via alias is `{Release}-litellm`.
        assert env["LITELLM_GATEWAY_URL"]["value"] == (
            "http://test-release-litellm.default.svc.cluster.local:4000"
        )

    def test_master_key_mounted_from_secret(
        self, helm_available, chart_dir,
    ) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = _envs(dep)
        assert "LITELLM_MASTER_KEY" in env
        ref = env["LITELLM_MASTER_KEY"]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "ll-key"
        assert ref["key"] == "LITELLM_MASTER_KEY"

    def test_audit_envs_default_to_compliance_safe(
        self, helm_available, chart_dir,
    ) -> None:
        dep = _find_deployment(self._docs_on(chart_dir))
        env = _envs(dep)
        assert env["LITELLM_AUDIT_ENABLED"]["value"] == "true"
        assert env["LITELLM_AUDIT_FAILURE_MODE"]["value"] == "closed"
        assert env["LITELLM_AUDIT_FULL_TEXT"]["value"] == "false"


@pytest.mark.helm
class TestLiteLLMSubchartValidation:
    """Schema + template validators fire at helm template time."""

    def test_missing_master_key_rejected(
        self, helm_available, chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "litellm.enabled=true",
            ],
        )
        # Schema-level rejection mentions the path and the constraint.
        assert "masterKeySecretName" in stderr

    def test_invalid_failure_mode_rejected(
        self, helm_available, chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "litellm.enabled=true",
                "litellm.masterKeySecretName=ll-key",
                "litellm.audit.failureMode=halfway-open",
            ],
        )
        assert "failureMode" in stderr

    def test_full_text_capture_without_enabled_rejected(
        self, helm_available, chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "litellm.audit.fullTextCapture=true",
            ],
        )
        # Either the schema's allOf branch OR the template-level guard
        # surfaces — both mention "enabled".
        assert "enabled" in stderr


@pytest.mark.helm
class TestLiteLLMExternalGatewayOverride:
    """`gatewayUrl` overrides the in-cluster Service URL — operator
    has their own LiteLLM deployment outside this chart."""

    def test_external_url_used_verbatim(
        self, helm_available, chart_dir,
    ) -> None:
        docs = _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "litellm.enabled=true",
                "litellm.masterKeySecretName=ll-key",
                "litellm.gatewayUrl=http://external.svc:4000",
            ],
        )
        dep = _find_deployment(docs)
        env = _envs(dep)
        assert env["LITELLM_GATEWAY_URL"]["value"] == (
            "http://external.svc:4000"
        )


@pytest.mark.helm
class TestLiteLLMGrafanaDashboard:
    """Grafana dashboard ConfigMap — gated by BOTH toggles."""

    def test_configmap_renders_with_dashboard_json(
        self, helm_available, chart_dir,
    ) -> None:
        docs = _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "litellm.enabled=true",
                "litellm.masterKeySecretName=ll-key",
                "monitoring.grafanaDashboards.enabled=true",
            ],
        )
        cm = _find_grafana_configmap(docs)
        assert cm is not None, "Grafana dashboard ConfigMap not rendered"
        # Discovery label that the Grafana operator sidecar looks for.
        assert cm["metadata"]["labels"]["grafana_dashboard"] == "1"
        # JSON payload parses + has the expected top-level structure.
        raw = cm["data"]["litellm.json"]
        parsed = json.loads(raw)
        assert parsed["title"] == "AIFactory — LiteLLM gateway"
        assert parsed["schemaVersion"] >= 38
        # All six panels present.
        assert len(parsed["panels"]) == 6
        # Both template variables present.
        var_names = {v["name"] for v in parsed["templating"]["list"]}
        assert {"org_id", "model"}.issubset(var_names)
