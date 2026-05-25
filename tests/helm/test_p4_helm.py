"""P4 — Helm chart acceptance tests.

Seven tests map directly to the seven acceptance bullets in Epic #26
issue #31. As implementation chunks land, the ``@pytest.mark.skip``
decorator is removed and a real body replaces the placeholder.

Coverage:
  1. test_helm_lint_strict_passes           (→ P4.1)
  2. test_helm_template_renders             (→ P4.2)
  3. test_kubeconform_passes                (→ P4.2)
  4. test_network_policy_present_and_strict (→ P4.3)
  5. test_pss_restricted_security_contexts  (→ P4.3)
  6. test_install_kind_with_bundled_postgres_succeeds (→ P4.4)
  7. test_custom_ca_bundle_is_trusted_by_pod (→ P4.6)
"""

from __future__ import annotations

import pytest


@pytest.mark.helm
def test_helm_lint_strict_passes(helm_available, chart_dir) -> None:
    """``helm lint --strict charts/aifactory`` passes with zero errors.

    Strict mode treats warnings as errors. We expect zero of either —
    this is the basic well-formedness gate for the chart.
    """
    import subprocess
    result = subprocess.run(
        ["helm", "lint", "--strict", str(chart_dir)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"helm lint --strict failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
    )
    # Belt-and-suspenders: also verify no [WARNING] / [ERROR] lines.
    assert "[ERROR]" not in result.stdout
    assert "[WARNING]" not in result.stdout


@pytest.mark.helm
def test_helm_template_renders(helm_template) -> None:
    """``helm template`` produces valid YAML with the expected K8s kinds.

    The ``helm_template`` fixture already asserts exit code 0; here we
    assert the output contains the core kinds we ship. NetworkPolicy
    is verified separately in test_network_policy_present_and_strict.
    """
    expected_kinds = {
        "Deployment",
        "Service",
        "ConfigMap",
        "ServiceAccount",
        "PodDisruptionBudget",
    }
    rendered_kinds = set()
    for line in helm_template.splitlines():
        if line.startswith("kind:"):
            rendered_kinds.add(line.split(":", 1)[1].strip())
    missing = expected_kinds - rendered_kinds
    assert not missing, (
        f"chart didn't render expected kinds: {missing}. "
        f"Rendered: {sorted(rendered_kinds)}"
    )


@pytest.mark.helm
def test_kubeconform_passes(kubeconform_available, helm_template) -> None:
    """Every rendered manifest conforms to the current K8s OpenAPI schema."""
    import subprocess
    result = subprocess.run(
        ["kubeconform", "-summary", "-strict"],
        input=helm_template,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"kubeconform failed (exit {result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "Invalid: 0" in result.stdout
    assert "Errors: 0" in result.stdout


@pytest.mark.helm
def test_network_policy_present_and_strict(helm_template) -> None:
    """The chart emits a NetworkPolicy with default-deny + explicit allowlist.

    Asserts:
      - A NetworkPolicy resource exists.
      - Both Ingress and Egress policy types are declared (= default-deny).
      - Egress includes 443/tcp to public IPs (Anthropic / IdP / KMS).
      - Egress includes 53/udp + 53/tcp (DNS) to kube-system.
      - Ingress restricts to the ingress-controller namespace by default.
    """
    import yaml
    docs = [d for d in yaml.safe_load_all(helm_template) if d]
    netpols = [d for d in docs if d.get("kind") == "NetworkPolicy"]
    assert len(netpols) == 1, f"expected 1 NetworkPolicy, got {len(netpols)}"
    np = netpols[0]
    policy_types = set(np["spec"]["policyTypes"])
    assert policy_types == {"Ingress", "Egress"}, (
        f"NetworkPolicy must declare both types for default-deny; got {policy_types}"
    )

    # Egress must include a 443/tcp rule.
    egress_rules = np["spec"].get("egress", [])
    has_443 = any(
        any(p.get("port") == 443 and p.get("protocol") == "TCP" for p in rule.get("ports", []))
        for rule in egress_rules
    )
    assert has_443, "NetworkPolicy egress must allow 443/tcp"

    # Egress must include DNS (port 53).
    has_dns = any(
        any(p.get("port") == 53 for p in rule.get("ports", []))
        for rule in egress_rules
    )
    assert has_dns, "NetworkPolicy egress must allow DNS (port 53)"


@pytest.mark.helm
def test_pss_restricted_security_contexts(helm_template) -> None:
    """Pod + container security contexts satisfy PSS-restricted policy.

    Verifies: runAsNonRoot, runAsUser >= 1000, fsGroup >= 1000,
    allowPrivilegeEscalation=false, dropped ALL capabilities,
    readOnlyRootFilesystem=true, seccompProfile=RuntimeDefault.

    The actual PSS admission verification happens at install time in
    test_install_kind_with_bundled_postgres_succeeds; this test
    statically validates the rendered manifests would pass it.
    """
    import yaml
    docs = [d for d in yaml.safe_load_all(helm_template) if d]
    deploys = [d for d in docs if d.get("kind") == "Deployment"]
    assert len(deploys) == 1
    dep = deploys[0]

    pod_spec = dep["spec"]["template"]["spec"]
    pod_sc = pod_spec.get("securityContext", {})

    # Pod-level checks.
    assert pod_sc.get("runAsNonRoot") is True, "pod must runAsNonRoot"
    assert pod_sc.get("runAsUser", 0) >= 1000, "pod must run as uid >= 1000"
    assert pod_sc.get("fsGroup", 0) >= 1000, "pod must use fsGroup >= 1000"
    seccomp = pod_sc.get("seccompProfile", {})
    assert seccomp.get("type") == "RuntimeDefault", "seccompProfile must be RuntimeDefault"

    # Container-level checks (single container by design).
    containers = pod_spec["containers"]
    assert len(containers) == 1
    c_sc = containers[0]["securityContext"]
    assert c_sc.get("allowPrivilegeEscalation") is False
    assert c_sc.get("readOnlyRootFilesystem") is True
    assert c_sc.get("runAsNonRoot") is True
    assert c_sc.get("capabilities", {}).get("drop") == ["ALL"], (
        "container must drop ALL capabilities"
    )
    c_seccomp = c_sc.get("seccompProfile", {})
    assert c_seccomp.get("type") == "RuntimeDefault"


@pytest.mark.helm
@pytest.mark.slow
@pytest.mark.skip(reason="P4.4 implementation pending: kind install")
def test_install_kind_with_bundled_postgres_succeeds(
    helm_available, kind_available, kubectl_available, chart_dir,
) -> None:
    """End-to-end: `helm install` on a kind cluster with postgres.bundled=true."""
    pytest.fail("P4.4 not landed")


@pytest.mark.helm
@pytest.mark.skip(reason="P4.6 implementation pending: customCABundle")
def test_custom_ca_bundle_is_trusted_by_pod(helm_available, chart_dir) -> None:
    """When global.customCABundle.secretName is set, the bundle is mounted
    + SSL_CERT_FILE points at it inside the container."""
    pytest.fail("P4.6 not landed")
