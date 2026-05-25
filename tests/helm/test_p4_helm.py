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
@pytest.mark.skip(reason="P4.3 implementation pending: NetworkPolicy")
def test_network_policy_present_and_strict(helm_template) -> None:
    """The chart emits a NetworkPolicy with default-deny + explicit allowlist."""
    pytest.fail("P4.3 not landed")


@pytest.mark.helm
@pytest.mark.skip(reason="P4.3 implementation pending: PSS=restricted")
def test_pss_restricted_security_contexts(helm_template) -> None:
    """Pod + container security contexts satisfy PSS-restricted policy.

    Verifies: runAsNonRoot, runAsUser >= 1000, fsGroup >= 1000,
    allowPrivilegeEscalation=false, dropped ALL capabilities,
    readOnlyRootFilesystem=true, seccompProfile=RuntimeDefault.
    """
    pytest.fail("P4.3 not landed")


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
