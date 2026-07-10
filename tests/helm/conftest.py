"""Pytest fixtures for P4 Helm chart acceptance tests.

Tests are marked ``@pytest.mark.helm``. They run against locally-
installed `helm`, `kubeconform`, and (for end-to-end install tests)
a `kind` cluster. CI's helm-acceptance job installs all three.

Locally, tests skip cleanly when the required binaries aren't on
PATH — operators who don't run Kubernetes still get a passing test
suite for the rest of the codebase.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART_DIR = REPO_ROOT / "charts" / "aifactory"


def pytest_collection_modifyitems(config, items):
    """Skip ``gvisor_live`` tests unless they were explicitly selected.

    These are live-cluster gVisor smoke tests (issue #170). They assert the
    ``gvisor`` RuntimeClass and runsc shim are present — which is only true on
    the Kind cluster bootstrapped by ``gvisor-smoke.yml`` (run via
    ``-m gvisor_live``). Their fixtures skip when *no* cluster is reachable,
    but on a reachable non-gVisor cluster (e.g. the k3d dev/deploy cluster)
    the RuntimeClass assertion would *fail* — which previously broke the
    husky pre-commit pytest gate and forced ``git commit --no-verify``.

    The marker's documented contract is "skipped by default; run via
    gvisor-smoke.yml CI or ``-m gvisor_live``". This hook enforces that
    contract for every invocation path: skip unless the ``-m`` expression
    names ``gvisor_live`` (how CI selects them) or ``GVISOR_LIVE=1`` is set.
    """
    markexpr = config.getoption("markexpr", default="") or ""
    explicitly_requested = "gvisor_live" in markexpr or os.environ.get(
        "GVISOR_LIVE", ""
    ).lower() in ("1", "true", "yes")
    if explicitly_requested:
        return

    skip_marker = pytest.mark.skip(
        reason="gvisor_live: skipped by default — run via gvisor-smoke.yml CI "
        "or '-m gvisor_live' (requires a Kind cluster with the runsc shim)"
    )
    for item in items:
        if "gvisor_live" in item.keywords:
            item.add_marker(skip_marker)


def _binary_available(name: str) -> bool:
    """True iff ``name`` resolves on PATH (and is executable)."""
    return shutil.which(name) is not None


def _binary_version(name: str) -> str | None:
    """Return the binary's version string or None if it doesn't run."""
    try:
        result = subprocess.run(
            [name, "version", "--short"] if name == "helm" else [name, "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or result.stderr.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


@pytest.fixture
def helm_available() -> bool:
    if not _binary_available("helm"):
        pytest.skip("helm not installed")
    return True


@pytest.fixture
def kubeconform_available() -> bool:
    if not _binary_available("kubeconform"):
        pytest.skip("kubeconform not installed")
    return True


@pytest.fixture
def kind_available() -> bool:
    if not _binary_available("kind"):
        pytest.skip("kind not installed")
    return True


@pytest.fixture
def kubectl_available() -> bool:
    if not _binary_available("kubectl"):
        pytest.skip("kubectl not installed")
    return True


@pytest.fixture(scope="session")
def _chart_deps_pulled() -> bool:
    """Run `helm dep update` once per session.

    Required since Epic #35 #38 PR-3 added the LiteLLM sub-chart
    dependency — `helm template` fails immediately on a chart with
    unfilled dependencies, regardless of whether the test toggles
    that sub-chart on. Skips when helm itself isn't on PATH so the
    suite still passes on workstations without a Kubernetes setup.
    """
    if not _binary_available("helm"):
        return False
    if not CHART_DIR.is_dir():
        return False
    result = subprocess.run(
        ["helm", "dep", "update", str(CHART_DIR)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    # Don't pytest.fail here — let individual tests skip via
    # helm_available if their environment is incomplete. We just
    # log a warning to the captured output so debugging is easier.
    if result.returncode != 0:
        print(
            f"[conftest] helm dep update failed (rc={result.returncode}):"
            f" {result.stderr[-500:]}"
        )
    return result.returncode == 0


@pytest.fixture
def chart_dir(_chart_deps_pulled) -> Path:
    """Absolute path to the aifactory Helm chart directory.

    Depends on _chart_deps_pulled so sub-chart deps are present before
    any test renders the chart.
    """
    if not CHART_DIR.is_dir():
        pytest.skip(f"chart not present at {CHART_DIR} (pre-P4.1 state)")
    return CHART_DIR


@pytest.fixture
def helm_template(helm_available, chart_dir):
    """Render the chart via `helm template`; return the YAML string.

    Tests that need to inspect specific manifests (e.g. NetworkPolicy,
    Deployment securityContext) parse this output once per test
    rather than re-rendering.
    """
    result = subprocess.run(
        ["helm", "template", "aifactory", str(chart_dir)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.fail(
            f"`helm template` failed (exit {result.returncode}):\n"
            f"stderr: {result.stderr[-1500:]}"
        )
    return result.stdout
