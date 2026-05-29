"""Live-cluster gVisor smoke tests (issue #170).

These tests run against a real Kubernetes cluster that has gVisor's
runsc runtime installed and a ``gvisor`` RuntimeClass registered.
They are NOT template-rendering tests (those live in
test_gvisor_runtime_class.py); they require kubectl connectivity.

Mark: ``@pytest.mark.gvisor_live``

The tests are skipped by default. Only the gvisor-smoke.yml CI
workflow runs them (via ``-m gvisor_live``). Operators can also
run them locally with a Kind cluster — see
docs/docs/concepts/gvisor-smoke-test-local.md for the full recipe.

Architecture
------------
The test suite uses two pods:

1. AIFactory deployment pod (``app.kubernetes.io/name=aifactory``):
   - Deployed via ``helm install`` with ``sandbox.gvisor.enabled=true``.
   - Used only to verify the RuntimeClass is correctly wired to the pod
     spec by the Helm chart. We do NOT exec into this pod.

2. gVisor compatibility test pod (``gvisor-compat-tester``):
   - A standalone ``ubuntu:22.04`` pod with ``runtimeClassName: gvisor``.
   - Has bash, curl, git installed.
   - Used for all exec-based compatibility-matrix tests.
   - Keeps compat test requirements out of the AIFactory container image.

Environment variables consumed:
    KUBECONFIG            — path to kubeconfig (default: ~/.kube/config)
    GVISOR_NAMESPACE      — namespace AIFactory was installed into
                            (default: aifactory)
    GVISOR_COMPAT_POD     — name of the compatibility test pod
                            (default: gvisor-compat-tester)

Compatibility table cross-reference
------------------------------------
The compat table at the top of docs/docs/concepts/gvisor-sandbox.md
lists what is and is not supported under gVisor. Each test that
exercises a "works" row is annotated with the table row it validates.
Rows marked as "not supported" (docker build, tcpdump, /dev/kvm) are
NOT exercised — gVisor blocking them is expected behaviour, not a
failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NAMESPACE = os.environ.get("GVISOR_NAMESPACE", "aifactory")
COMPAT_POD = os.environ.get("GVISOR_COMPAT_POD", "gvisor-compat-tester")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kubectl(*args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run ``kubectl --namespace NAMESPACE`` command."""
    cmd = ["kubectl", "--namespace", NAMESPACE, *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _kubectl_global(*args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a kubectl command without a --namespace flag (cluster-scoped resources)."""
    return subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _exec_in_pod(pod: str, *cmd_args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """kubectl exec into ``pod`` in NAMESPACE and run cmd_args."""
    return subprocess.run(
        ["kubectl", "--namespace", NAMESPACE, "exec", pod, "--", *cmd_args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def kubectl_ok() -> bool:
    """Skip the entire module if kubectl is not on PATH or not reachable."""
    if not shutil.which("kubectl"):
        pytest.skip("kubectl not on PATH — live-cluster tests skipped")
    result = _kubectl_global("cluster-info", check=False, timeout=10)
    if result.returncode != 0:
        pytest.skip(
            f"kubectl cluster-info failed (rc={result.returncode}): "
            f"{result.stderr.strip()[:200]} — no live cluster available"
        )
    return True


@pytest.fixture(scope="module")
def aifactory_pod_name(kubectl_ok) -> str | None:
    """Return the name of the first aifactory pod or None if not found.

    This pod is used only to inspect the pod spec (runtimeClassName).
    We do not require it to be Running because a busybox stub may be in
    a different lifecycle state.
    """
    result = _kubectl(
        "get", "pods",
        "-l", "app.kubernetes.io/name=aifactory",
        "-o", "jsonpath={.items[0].metadata.name}",
        check=False,
    )
    name = result.stdout.strip()
    return name if name else None


@pytest.fixture(scope="module")
def compat_pod(kubectl_ok) -> str:
    """Return the compat-tester pod name, waiting up to 90 s for Running.

    The compat-tester pod (ubuntu:22.04 + runtimeClassName: gvisor) is
    launched in the CI workflow before pytest runs. This fixture waits
    for it to be ready so exec-based tests can run immediately.
    """
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = _kubectl(
            "get", "pod", COMPAT_POD,
            "-o", "jsonpath={.status.phase}",
            check=False,
        )
        phase = result.stdout.strip()
        if phase == "Running":
            return COMPAT_POD
        time.sleep(3)

    # Last diagnostic before skipping.
    _kubectl("describe", "pod", COMPAT_POD, check=False)
    pytest.skip(
        f"Compat-tester pod {COMPAT_POD!r} not Running after 90 s in "
        f"namespace {NAMESPACE!r}. "
        "Was the 'Launch gVisor compatibility test pod' workflow step run?"
    )


# ---------------------------------------------------------------------------
# Tests: RuntimeClass wiring
# ---------------------------------------------------------------------------


@pytest.mark.gvisor_live
class TestGvisorRuntimeClassOnCluster:
    """Validate that the RuntimeClass is wired through to the live pods."""

    def test_runtime_class_exists_in_cluster(self, kubectl_ok) -> None:
        """The gVisor RuntimeClass must be present in the cluster.

        If this fails it means gVisor was not registered during cluster
        bootstrap — the runsc install or containerd config step failed.
        """
        result = _kubectl_global(
            "get", "runtimeclass", "gvisor",
            "-o", "jsonpath={.handler}",
            check=False,
        )
        assert result.returncode == 0, (
            "RuntimeClass 'gvisor' not found in cluster. "
            "Ensure runsc is installed on nodes and containerd config "
            "includes the runsc shim. See gvisor-smoke.yml step 5."
        )
        handler = result.stdout.strip()
        assert handler == "runsc", (
            f"RuntimeClass handler expected 'runsc', got {handler!r}"
        )

    def test_pods_have_gvisor_runtimeclass(
        self, kubectl_ok, aifactory_pod_name
    ) -> None:
        """All aifactory pods must declare runtimeClassName: gvisor.

        Validates that the helm chart's sandbox.gvisor.enabled=true flag
        propagated to the live pod spec — not just the template render.
        """
        if not aifactory_pod_name:
            pytest.skip(
                f"No aifactory pods found in namespace {NAMESPACE!r}. "
                "Ensure AIFactory is deployed with sandbox.gvisor.enabled=true."
            )
        result = _kubectl(
            "get", "pods",
            "-l", "app.kubernetes.io/name=aifactory",
            "-o", "jsonpath={.items[*].spec.runtimeClassName}",
            check=False,
        )
        raw = result.stdout.strip()
        assert raw, (
            f"No aifactory pods found in namespace {NAMESPACE!r}. "
            "Ensure AIFactory is deployed before running this test."
        )
        runtime_classes = raw.split()
        for rc in runtime_classes:
            assert rc == "gvisor", (
                "Expected runtimeClassName='gvisor' on all aifactory pods, "
                f"got {rc!r}. Helm install used sandbox.gvisor.enabled=true?"
            )


# ---------------------------------------------------------------------------
# Tests: Compatibility matrix (exec into the compat-tester pod)
# ---------------------------------------------------------------------------


@pytest.mark.gvisor_live
class TestGvisorCompatibilityMatrix:
    """Validate each 'works' row in the gVisor compatibility table.

    Source: docs/docs/concepts/gvisor-sandbox.md — Compatibility table.
    Rows validated here are annotated with [TABLE:row].

    All tests exec into the ubuntu:22.04 ``gvisor-compat-tester`` pod
    which runs under gVisor (runtimeClassName: gvisor). Using a separate
    pod keeps these requirements out of the AIFactory container image.
    """

    def test_git_clone_works_under_gvisor(self, compat_pod: str) -> None:
        """[TABLE: git clone, git pull, git push] — compat table row 1.

        git clone requires TCP connect, DNS resolution, and standard POSIX
        syscalls. gVisor supports all of these. We clone a tiny public repo
        into /tmp to keep the test fast (no large download).
        """
        # Use a shallow clone of a minimal public repo.
        result = _exec_in_pod(
            compat_pod,
            "git", "clone", "--depth=1",
            "https://github.com/octocat/Hello-World.git",
            "/tmp/gvisor-test-clone",
            timeout=90,
        )
        # rc=128 = "already exists" (idempotent on retried runs).
        if result.returncode not in (0, 128):
            stderr_tail = (result.stderr or "").strip()[-500:]
            assert False, (
                f"git clone failed under gVisor (rc={result.returncode}). "
                "This would invalidate the compat-table row. "
                f"stderr tail: {stderr_tail}"
            )
        # Confirm the clone directory exists.
        check = _exec_in_pod(compat_pod, "ls", "/tmp/gvisor-test-clone")
        assert check.returncode == 0, "Clone directory not found after git clone"

    def test_curl_https_works_under_gvisor(self, compat_pod: str) -> None:
        """[TABLE: curl/wget HTTPS calls] — compat table row 2.

        Validates TLS handshake, DNS resolution, and egress all function
        under gVisor. We hit the Anthropic models endpoint; a 4xx response
        is a pass (auth failure = network is fine, gVisor didn't block it).
        A connection error or gVisor-specific EPERM would be a real failure.
        """
        result = _exec_in_pod(
            compat_pod,
            "curl",
            "--silent",
            "--max-time", "20",
            "--write-out", "%{http_code}",
            "--output", "/dev/null",
            "https://api.anthropic.com/v1/models",
            timeout=30,
        )
        http_code = result.stdout.strip()
        stderr_lower = (result.stderr or "").lower()

        # curl exit 0 + any HTTP response (even 4xx) means TLS + network OK.
        assert result.returncode == 0, (
            f"curl exited with rc={result.returncode} under gVisor — "
            f"possible syscall block. stderr: {result.stderr.strip()[:300]}"
        )
        # 2xx or 4xx both prove the network path works.
        assert http_code.isdigit() and int(http_code) in range(200, 500), (
            f"Unexpected HTTP status {http_code!r} from Anthropic API. "
            "Expected 2xx or 4xx (auth failure); 5xx or empty = real problem."
        )
        assert "operation not permitted" not in stderr_lower, (
            f"curl stderr contains gVisor syscall rejection: {result.stderr.strip()[:300]}"
        )

    def test_outbound_https_works_under_gvisor(self, compat_pod: str) -> None:
        """[TABLE: curl/wget HTTPS calls] — additional egress assertion.

        Hit https://api.anthropic.com with -I (HEAD) so there is no body
        download. The key assertion is that TLS + DNS + egress all function
        under gVisor. A 4xx response is fine — it proves connectivity.
        """
        result = _exec_in_pod(
            compat_pod,
            "curl",
            "-sI",
            "--max-time", "20",
            "https://api.anthropic.com/v1/models",
            timeout=30,
        )
        # rc=22 = HTTP error (4xx/5xx) which is still a successful connection.
        assert result.returncode in (0, 22), (
            f"curl -I failed with rc={result.returncode} — "
            "gVisor may be blocking outbound TLS. "
            f"stderr: {result.stderr.strip()[:300]}"
        )

    def test_workspace_pvc_mount_works_under_gvisor(
        self, kubectl_ok, aifactory_pod_name
    ) -> None:
        """Workspace PVC (from #154/#82) is writable under gVisor.

        gVisor supports standard VFS operations including writes to mounted
        volumes. This test writes a file to the workspace mount on the
        AIFactory pod and reads it back.

        Uses the AIFactory pod (not compat-tester) because the PVC is
        mounted there — the compat pod doesn't have the workspaces volume.
        Skipped if: no aifactory pod exists, pod is not Running, or
        workspaces.enabled=false.
        """
        if not aifactory_pod_name:
            pytest.skip(
                f"No aifactory pods found in namespace {NAMESPACE!r}. "
                "Ensure AIFactory is deployed with workspaces.enabled=true."
            )

        # Verify the pod is Running before trying exec.
        pod_phase = _kubectl(
            "get", "pod", aifactory_pod_name,
            "-o", "jsonpath={.status.phase}",
            check=False,
        ).stdout.strip()
        if pod_phase != "Running":
            pytest.skip(
                f"AIFactory pod {aifactory_pod_name!r} is in phase {pod_phase!r} "
                "(not Running) — kubectl exec would fail. "
                "The workspace PVC test requires a Running pod."
            )

        # workspaces.mountPath default is /workspaces.
        mount_path = "/workspaces"
        check_mount = _exec_in_pod(
            aifactory_pod_name, "ls", mount_path, timeout=10,
        )
        if check_mount.returncode != 0:
            pytest.skip(
                f"Workspace mount {mount_path!r} not found — "
                "was AIFactory deployed with workspaces.enabled=true?"
            )

        canary = "/workspaces/gvisor-smoke-test-canary.txt"
        write_result = _exec_in_pod(
            aifactory_pod_name,
            "sh", "-c", f"echo 'gvisor-canary' > {canary}",
            timeout=10,
        )
        assert write_result.returncode == 0, (
            "Failed to write canary file to workspace PVC under gVisor. "
            f"rc={write_result.returncode}, stderr={write_result.stderr.strip()}"
        )
        read_result = _exec_in_pod(
            aifactory_pod_name, "cat", canary, timeout=10,
        )
        assert read_result.returncode == 0, (
            "Failed to read canary file from workspace PVC under gVisor."
        )
        assert "gvisor-canary" in read_result.stdout
        _exec_in_pod(aifactory_pod_name, "rm", "-f", canary, timeout=10)

    def test_bash_allowlist_compatibility_matrix(self, compat_pod: str) -> None:
        """Validate all commands in the security allowlist work under gVisor.

        Exercises the subset of BASE_COMMANDS from
        apps/backend/project/command_registry/base.py that the compatibility
        table claims work. Each command must exit 0 under gVisor; a non-zero
        exit or gVisor syscall rejection marks the row as broken.
        """
        # (command, args, acceptable_exit_codes, description)
        compat_commands = [
            # Core shell
            (["echo", "gvisor-compat-check"], {0}, "echo"),
            (["ls", "/tmp"], {0}, "ls"),
            (["cat", "/etc/os-release"], {0}, "cat"),
            (["grep", "ID", "/etc/os-release"], {0}, "grep"),
            (["find", "/tmp", "-maxdepth", "1", "-type", "f"], {0}, "find"),
            (["uname", "-r"], {0}, "uname"),
            (["id"], {0}, "id"),
            (["whoami"], {0}, "whoami"),
            # git — compat table row 1
            (["git", "version"], {0}, "git version"),
            # curl — compat table row 2
            (["curl", "--version"], {0}, "curl --version"),
            # Standard coreutils
            (["sh", "-c", "date"], {0}, "date"),
            (["sh", "-c", "wc -l /etc/os-release"], {0}, "wc"),
        ]

        failures = []
        for cmd, ok_codes, label in compat_commands:
            result = _exec_in_pod(compat_pod, *cmd, timeout=30)
            stderr_lower = (result.stderr or "").lower()
            syscall_blocked = (
                "operation not permitted" in stderr_lower
                or "function not implemented" in stderr_lower
                # Note: "permission denied" can be a legit filesystem error,
                # not necessarily a gVisor block. We check rc separately.
            )
            if result.returncode not in ok_codes or syscall_blocked:
                failures.append(
                    f"  [{label}]: rc={result.returncode}, "
                    f"syscall_blocked={syscall_blocked}, "
                    f"stderr={result.stderr.strip()[:200]!r}"
                )

        assert not failures, (
            "The following allowlist commands failed under gVisor:\n"
            + "\n".join(failures)
            + "\n\nThese findings should be surfaced as compat-table updates in "
            "docs/docs/concepts/gvisor-sandbox.md."
        )
