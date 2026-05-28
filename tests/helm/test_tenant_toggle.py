"""Helm chart acceptance tests for the tenant.isolation toggle.

Epic #35 #36 PR-3. Mirrors the shape of test_audit_anchor_toggle.py +
test_otel_toggle.py.

When ``tenant.isolationEnabled=false`` (default):
  • No TENANT_* env vars on the web container.
  • No reconciler ClusterRole / ClusterRoleBinding rendered.
  • No tenant-teardown CronJob rendered.
  • No CNI pre-install probe Job rendered.
  • No Gatekeeper templates rendered (independent of gatekeeperEnabled).
  • Chart byte-for-byte same as pre-#36 deployments.

When ``tenant.isolationEnabled=true``:
  • TENANT_ISOLATION_ENABLED, TENANT_NAMESPACE_PREFIX,
    TENANT_CNI_BACKEND, TENANT_DELETION_GRACE_DAYS,
    TENANT_LIMIT_RANGE_CPU/MEMORY, TENANT_QUOTA_MAX_PODS/PVCS
    all injected on the web pod.
  • ClusterRole + ClusterRoleBinding for the reconciler rendered with
    the expected verb set on Namespaces / RoleBindings / NetPols.
  • Tenant tear-down CronJob rendered with the configured schedule
    + concurrencyPolicy=Forbid + the correct command.
  • CNI pre-install probe Job rendered with the right hook annotations.

When ``tenant.isolationEnabled=true`` + ``tenant.gatekeeperEnabled=true``:
  • Both ConstraintTemplates (namespace-prefix + rolebinding-tenant-scope)
    render.
  • Both Constraints render with the operator's prefix + reconciler SA.

Schema validators:
  • ``tenant.deletionGraceDays`` >= 0 — 0 is allowed (concept doc
    notes the operator WARNING on every reconcile tick).
  • ``tenant.networkPolicy.cniBackend`` enum ∈ {auto, calico, cilium}.
  • ``tenant.namespacePrefix`` matches the DNS-label-safe pattern.
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


def _find_deployment(docs: list[dict]) -> dict:
    for d in docs:
        if d.get("kind") == "Deployment":
            return d
    raise AssertionError("no Deployment in rendered chart")


def _find_by_kind_name(
    docs: list[dict], kind: str, name_suffix: str,
) -> dict | None:
    for d in docs:
        if d.get("kind") != kind:
            continue
        name = d.get("metadata", {}).get("name", "")
        if name.endswith(name_suffix):
            return d
    return None


def _find_cronjob(docs: list[dict], component: str) -> dict | None:
    for d in docs:
        if d.get("kind") != "CronJob":
            continue
        labels = d.get("metadata", {}).get("labels", {})
        if labels.get("app.kubernetes.io/component") == component:
            return d
    return None


def _find_all_by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d.get("kind") == kind]


# ---------------------------------------------------------------------------
# Default (isolation OFF) — chart byte-for-byte unchanged from pre-#36
# ---------------------------------------------------------------------------


@pytest.mark.helm
class TestTenantIsolationOff:
    """Default state — no tenant env vars, no RBAC, no CronJob, no
    CNI probe, no Gatekeeper templates."""

    def _docs(self, chart_dir) -> list[dict]:
        return _render(chart_dir, ["postgres.externalSecretName=test-pg"])

    def test_no_tenant_env_vars(self, helm_available, chart_dir) -> None:
        dep = _find_deployment(self._docs(chart_dir))
        env = dep["spec"]["template"]["spec"]["containers"][0].get("env", [])
        names = [e["name"] for e in env]
        for name in (
            "TENANT_ISOLATION_ENABLED",
            "TENANT_NAMESPACE_PREFIX",
            "TENANT_CNI_BACKEND",
            "TENANT_DELETION_GRACE_DAYS",
            "TENANT_LIMIT_RANGE_CPU",
            "TENANT_LIMIT_RANGE_MEMORY",
            "TENANT_QUOTA_MAX_PODS",
            "TENANT_QUOTA_MAX_PVCS",
        ):
            assert name not in names, f"unexpected {name} in default render"

    def test_no_reconciler_rbac(self, helm_available, chart_dir) -> None:
        docs = self._docs(chart_dir)
        assert _find_by_kind_name(
            docs, "ClusterRole", "-tenant-reconciler",
        ) is None
        assert _find_by_kind_name(
            docs, "ClusterRoleBinding", "-tenant-reconciler",
        ) is None

    def test_no_teardown_cronjob(self, helm_available, chart_dir) -> None:
        assert _find_cronjob(
            self._docs(chart_dir), "tenant-teardown-cron",
        ) is None

    def test_no_cni_probe(self, helm_available, chart_dir) -> None:
        docs = self._docs(chart_dir)
        jobs = [
            j for j in _find_all_by_kind(docs, "Job")
            if j.get("metadata", {}).get("labels", {}).get(
                "app.kubernetes.io/component",
            ) == "tenant-cni-probe"
        ]
        assert jobs == []

    def test_no_gatekeeper_templates(self, helm_available, chart_dir) -> None:
        docs = self._docs(chart_dir)
        for kind in ("ConstraintTemplate", "AIFactoryTenantNamespacePrefix",
                     "AIFactoryTenantRoleBindingScope"):
            assert _find_all_by_kind(docs, kind) == [], (
                f"Gatekeeper {kind} rendered when isolation disabled"
            )


# ---------------------------------------------------------------------------
# Isolation enabled — env vars, RBAC, CronJob, CNI probe all render
# ---------------------------------------------------------------------------


@pytest.mark.helm
class TestTenantIsolationOn:
    """``tenant.isolationEnabled=true`` flips the full set of tenant
    resources on."""

    def _docs(self, chart_dir) -> list[dict]:
        return _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "tenant.isolationEnabled=true",
            ],
        )

    def test_web_pod_has_all_tenant_env_vars(
        self, helm_available, chart_dir,
    ) -> None:
        dep = _find_deployment(self._docs(chart_dir))
        env = {
            e["name"]: e.get("value")
            for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env["TENANT_ISOLATION_ENABLED"] == "true"
        assert env["TENANT_NAMESPACE_PREFIX"] == "aifactory-tenant"
        assert env["TENANT_CNI_BACKEND"] == "auto"
        assert env["TENANT_DELETION_GRACE_DAYS"] == "30"
        assert env["TENANT_LIMIT_RANGE_CPU"] == "500m"
        assert env["TENANT_LIMIT_RANGE_MEMORY"] == "512Mi"
        assert env["TENANT_QUOTA_MAX_PODS"] == "50"
        assert env["TENANT_QUOTA_MAX_PVCS"] == "20"

    def test_reconciler_clusterrole_rendered(
        self, helm_available, chart_dir,
    ) -> None:
        cr = _find_by_kind_name(
            self._docs(chart_dir), "ClusterRole", "-tenant-reconciler",
        )
        assert cr is not None, "tenant reconciler ClusterRole not rendered"
        # Spot-check the verbs we documented in the design.
        rules = cr["spec"]["rules"] if "spec" in cr else cr["rules"]
        # The kubernetes RBAC schema puts rules at the top level
        # of ClusterRole (no .spec). Handle both shapes.
        if "rules" in cr:
            rules = cr["rules"]
        ns_rule = next(
            r for r in rules
            if "namespaces" in r.get("resources", [])
        )
        for verb in ("create", "update", "delete", "get", "list"):
            assert verb in ns_rule["verbs"], (
                f"namespace rule missing verb {verb!r}"
            )

    def test_reconciler_clusterrolebinding_targets_web_sa(
        self, helm_available, chart_dir,
    ) -> None:
        crb = _find_by_kind_name(
            self._docs(chart_dir),
            "ClusterRoleBinding", "-tenant-reconciler",
        )
        assert crb is not None
        # Subject points at the chart's main ServiceAccount.
        subj = crb["subjects"][0]
        assert subj["kind"] == "ServiceAccount"
        assert subj["name"].endswith("test-release-aifactory") or \
               subj["name"].endswith("aifactory")

    def test_teardown_cronjob_rendered_with_defaults(
        self, helm_available, chart_dir,
    ) -> None:
        cron = _find_cronjob(self._docs(chart_dir), "tenant-teardown-cron")
        assert cron is not None, "tenant-teardown CronJob not rendered"
        assert cron["spec"]["schedule"] == "0 3 * * *"
        assert cron["spec"]["concurrencyPolicy"] == "Forbid"
        assert cron["spec"]["timeZone"] == "UTC"
        container = (
            cron["spec"]["jobTemplate"]["spec"]
                ["template"]["spec"]["containers"][0]
        )
        assert container["command"] == [
            "python", "-m", "server.services.tenant_teardown",
        ]

    def test_teardown_cronjob_passes_grace_days(
        self, helm_available, chart_dir,
    ) -> None:
        cron = _find_cronjob(self._docs(chart_dir), "tenant-teardown-cron")
        container = (
            cron["spec"]["jobTemplate"]["spec"]
                ["template"]["spec"]["containers"][0]
        )
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env["TENANT_DELETION_GRACE_DAYS"] == "30"
        assert env["TENANT_TEARDOWN_DRY_RUN_HOURS"] == "24"

    def test_cni_probe_job_rendered(
        self, helm_available, chart_dir,
    ) -> None:
        docs = self._docs(chart_dir)
        probes = [
            j for j in _find_all_by_kind(docs, "Job")
            if j.get("metadata", {}).get("labels", {}).get(
                "app.kubernetes.io/component",
            ) == "tenant-cni-probe"
        ]
        assert len(probes) == 1, "expected exactly one CNI probe Job"
        ann = probes[0]["metadata"]["annotations"]
        # Helm hook annotations gate when the Job runs.
        assert "pre-install" in ann["helm.sh/hook"]
        assert "pre-upgrade" in ann["helm.sh/hook"]
        # And the cleanup annotation so re-installs don't trip.
        assert "before-hook-creation" in ann["helm.sh/hook-delete-policy"]

    def test_no_gatekeeper_without_opt_in(
        self, helm_available, chart_dir,
    ) -> None:
        """Even with isolation ON, Gatekeeper templates stay off until
        ``gatekeeperEnabled=true``."""
        docs = self._docs(chart_dir)
        for kind in ("ConstraintTemplate", "AIFactoryTenantNamespacePrefix",
                     "AIFactoryTenantRoleBindingScope"):
            assert _find_all_by_kind(docs, kind) == []


# ---------------------------------------------------------------------------
# Isolation + Gatekeeper opt-in — sample policies render
# ---------------------------------------------------------------------------


@pytest.mark.helm
class TestTenantIsolationWithGatekeeper:
    """``tenant.gatekeeperEnabled=true`` ships the OPA sample policies
    that defence-in-depth the reconciler RBAC."""

    def _docs(self, chart_dir) -> list[dict]:
        return _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "tenant.isolationEnabled=true",
                "tenant.gatekeeperEnabled=true",
            ],
        )

    def test_constraint_templates_rendered(
        self, helm_available, chart_dir,
    ) -> None:
        docs = self._docs(chart_dir)
        templates = _find_all_by_kind(docs, "ConstraintTemplate")
        names = {t["metadata"]["name"] for t in templates}
        assert "aifactorytenantnamespaceprefix" in names
        assert "aifactorytenantrolebindingscope" in names

    def test_constraints_rendered(
        self, helm_available, chart_dir,
    ) -> None:
        docs = self._docs(chart_dir)
        ns_constraint = _find_all_by_kind(
            docs, "AIFactoryTenantNamespacePrefix",
        )
        rb_constraint = _find_all_by_kind(
            docs, "AIFactoryTenantRoleBindingScope",
        )
        assert len(ns_constraint) == 1
        assert len(rb_constraint) == 1

    def test_namespace_constraint_uses_configured_prefix(
        self, helm_available, chart_dir,
    ) -> None:
        docs = self._docs(chart_dir)
        c = _find_all_by_kind(docs, "AIFactoryTenantNamespacePrefix")[0]
        # Constraint passes the prefix WITH the trailing dash so the
        # Rego startswith() check denies bare prefix-name collisions.
        assert c["spec"]["parameters"]["prefix"] == "aifactory-tenant-"

    def test_rolebinding_constraint_targets_reconciler_sa(
        self, helm_available, chart_dir,
    ) -> None:
        docs = self._docs(chart_dir)
        c = _find_all_by_kind(docs, "AIFactoryTenantRoleBindingScope")[0]
        params = c["spec"]["parameters"]
        assert "aifactory" in params["reconcilerServiceAccount"]
        assert params["tenantNamespacePrefix"] == "aifactory-tenant-"


# ---------------------------------------------------------------------------
# Schema validators
# ---------------------------------------------------------------------------


@pytest.mark.helm
class TestTenantSchemaValidators:
    """JSON-schema rejects malformed values at helm template time."""

    def test_invalid_cni_backend_rejected(
        self, helm_available, chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "tenant.isolationEnabled=true",
                "tenant.networkPolicy.cniBackend=flannel",
            ],
        )
        # Either the message mentions the field OR the bad value.
        assert "cniBackend" in stderr or "flannel" in stderr

    def test_negative_grace_days_rejected(
        self, helm_available, chart_dir,
    ) -> None:
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "tenant.deletionGraceDays=-1",
            ],
        )
        assert "deletionGraceDays" in stderr or "-1" in stderr

    def test_zero_grace_days_allowed(
        self, helm_available, chart_dir,
    ) -> None:
        """Day-0 is documented as allowed but produces a WARNING log
        on every reconcile pass (concept doc §grace-period). Helm
        template MUST succeed."""
        docs = _render(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "tenant.isolationEnabled=true",
                "tenant.deletionGraceDays=0",
            ],
        )
        # Sanity: deployment still renders with the override in env.
        dep = _find_deployment(docs)
        env = {
            e["name"]: e.get("value")
            for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        assert env["TENANT_DELETION_GRACE_DAYS"] == "0"

    def test_invalid_namespace_prefix_rejected(
        self, helm_available, chart_dir,
    ) -> None:
        """Uppercase / leading-digit / trailing-dash prefixes fail the
        DNS-label pattern."""
        stderr = _render_expect_error(
            chart_dir,
            [
                "postgres.externalSecretName=test-pg",
                "tenant.namespacePrefix=BadPrefix",
            ],
        )
        assert "namespacePrefix" in stderr or "BadPrefix" in stderr


# ---------------------------------------------------------------------------
# Fixture-based smoke: the prepackaged values file renders cleanly
# ---------------------------------------------------------------------------


@pytest.mark.helm
class TestTenantIsolationFixture:
    """The fixture at tests/helm/fixtures/tenant-isolation-enabled.yaml
    captures a minimal multi-tenant operator config. If the fixture
    rendering breaks, the operator-onboarding flow is broken too."""

    def test_fixture_renders(self, helm_available, chart_dir) -> None:
        from pathlib import Path
        fixture = (
            Path(__file__).parent / "fixtures" / "tenant-isolation-enabled.yaml"
        )
        assert fixture.is_file(), f"fixture missing: {fixture}"
        result = subprocess.run(
            [
                "helm", "template", "test-release", str(chart_dir),
                "-f", str(fixture),
            ],
            capture_output=True, text=True, check=True,
        )
        docs = [d for d in yaml.safe_load_all(result.stdout) if d]
        # Sanity: CRD + Gatekeeper + RBAC + CronJob + Probe all show up.
        kinds = {d.get("kind") for d in docs}
        assert "ClusterRole" in kinds
        assert "ClusterRoleBinding" in kinds
        assert "CronJob" in kinds
        assert "ConstraintTemplate" in kinds
        assert "Job" in kinds  # the CNI probe hook
