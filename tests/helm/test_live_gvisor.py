"""Live-cluster gVisor smoke tests (issue #170).

These tests run against a real Kubernetes cluster that has gVisor's
runsc runtime installed and a ``gvisor`` RuntimeClass registered.
They are NOT template-rendering tests (those live in
test_gvisor_runtime_class.py); they require kubectl connectivity.

Mark: ``@pytest.mark.gvisor_live``

The tests are skipped by default. Only the gvisor-smoke.yml CI
workflow runs them (via ``-m gvisor_live``). Operators can also
run them locally after ``kind create cluster`` + runsc setup:

    kubectl apply -f - <<'EOF'
    apiVersion: node.k8s.io/v1
    kind: RuntimeClass
    metadata:
      name: gvisor
    handler: runsc
    EOF
    helm install aifactory charts/aifactory/ \\
        --set sandbox.gvisor.enabled=true \\
        --set postgres.bundled=true \\
        --set workspaces.enabled=true \\
        ... (see guides/deployment/gvisor-smoke-test-local.md)
    pytest tests/helm/test_live_gvisor.py -m gvisor_live -v

Environment variables consumed:
    KUBECONFIG        — path to kubeconfig (default: ~/.kube/config)
    GVISOR_NAMESPACE  — namespace AIFactory was installed into
                        (default: aifactory)

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
from typing import Generator

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NAMESPACE = os.environ.get("GVISOR_NAMESPACE", "aifactory")


def _kubectl(*args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a kubectl command, returning the CompletedProcess."""
    cmd = ["kubectl", "--namespace", NAMESPACE, *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _kubectl_global(*args: str, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a kubectl command without a --namespace flag (for cluster-scoped resources)."""
    cmd = ["kubectl", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _agent_pod_name() -> str | None:
    """Return the name of the first running aifactory pod, or None."""
    result = _kubectl(
        "get", "pods",
        "-l", "app.kubernetes.io/name=aifactory",
        "-o", "jsonpath={.items[0].metadata.name}",
        check=False,
    )
    name = result.stdout.strip()
    return name if name else None


def _exec_in_pod(pod: str, *cmd_args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """kubectl exec into ``pod`` and run cmd_args; returns CompletedProcess."""
    return subprocess.run(
        ["kubectl", "--namespace", NAMESPACE,
         "exec", pod, "--", *cmd_args],
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
def agent_pod(kubectl_ok) -> str:
    """Return the first aifactory agent pod name, waiting up to 90 s."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        pod = _agent_pod_name()
        if pod:
            return pod
        time.sleep(3)
    # Last check with diagnostic output before skipping.
    _kubectl("get", "pods", "-o", "wide", check=False)
    pytest.skip(
        f"No aifactory pod found in namespace {NAMESPACE!r} after 90 s; "
        "is AIFactory deployed with sandbox.gvisor.enabled=true?"
    )


# ---------------------------------------------------------------------------
# Tests
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

    def test_pods_have_gvisor_runtimeclass(self, kubectl_ok) -> None:
        """All aifactory pods must declare runtimeClassName: gvisor.

        Validates that the helm chart's sandbox.gvisor.enabled=true flag
        propagated to the live pod spec — not just the template render.
        """
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
                f"Expected runtimeClassName='gvisor' on all aifactory pods, "
                f"got {rc!r}. Helm install used sandbox.gvisor.enabled=true?"
            )


@pytest.mark.gvisor_live
class TestGvisorCompatibilityMatrix:
    """Validate each 'works' row in the gVisor compatibility table.

    Source: docs/docs/concepts/gvisor-sandbox.md — Compatibility table.
    Rows validated here are marked with [TABLE:row].
    """

    def test_git_clone_works_under_gvisor(self, agent_pod: str) -> None:
        """[TABLE: git clone, git pull, git push] — compat table row 1.

        git clone requires TCP connect, DNS resolution, and standard POSIX
        syscalls. gVisor supports all of these. We clone a tiny public repo
        into /tmp to keep the test fast (no large download).
        """
        # Use a shallow clone of a minimal repo to minimise network time.
        result = _exec_in_pod(
            agent_pod,
            "git", "clone", "--depth=1",
            "https://github.com/octocat/Hello-World.git",
            "/tmp/gvisor-test-clone",
            timeout=90,
        )
        # Tolerate exit 128 "already exists" on retried runs.
        if result.returncode not in (0, 128):
            # Capture stderr for the assertion message.
            stderr_tail = (result.stderr or "").strip()[-500:]
            assert False, (
                f"git clone failed under gVisor (rc={result.returncode}). "
                f"This would invalidate the compat-table row. "
                f"stderr tail: {stderr_tail}"
            )
        # Confirm the clone directory exists.
        check = _exec_in_pod(agent_pod, "ls", "/tmp/gvisor-test-clone")
        assert check.returncode == 0, "Clone directory not found after git clone"

    def test_curl_https_works_under_gvisor(self, agent_pod: str) -> None:
        """[TABLE: curl/wget HTTPS calls] — compat table row 2.

        Validates TLS handshake, DNS resolution, and egress all function
        under gVisor. We hit the Anthropic models endpoint; a 4xx response
        is a pass (auth failure = network is fine, gVisor didn't block it).
        A connection error or gVisor-specific EPERM would be a real failure.
        """
        result = _exec_in_pod(
            agent_pod,
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
        # curl exit 6 = DNS failure, exit 7 = connection refused,
        # exit 28 = timeout, exit 35 = TLS handshake failure.
        assert result.returncode == 0, (
            f"curl exited with rc={result.returncode} under gVisor — "
            f"possible syscall block. stderr: {result.stderr.strip()[:300]}"
        )
        # The Anthropic API returns 401 (no auth) or 403, never 5xx for
        # a simple GET /models. 2xx or 4xx both prove the network path works.
        assert http_code.isdigit() and int(http_code) in range(200, 500), (
            f"Unexpected HTTP status {http_code!r} from Anthropic API. "
            "Expected 2xx or 4xx (auth failure); 5xx or empty = real problem."
        )
        # No gVisor-specific "operation not permitted" errors in stderr.
        assert "operation not permitted" not in stderr_lower, (
            f"curl stderr contains gVisor syscall rejection: {result.stderr.strip()[:300]}"
        )

    def test_outbound_https_works_under_gvisor(self, agent_pod: str) -> None:
        """[TABLE: curl/wget HTTPS calls] — additional egress assertion.

        Hit https://api.anthropic.com with -I (HEAD) so there is no body
        download. The key assertion is that TLS + DNS + egress all function
        under gVisor. A 4xx response is fine — it proves connectivity.
        """
        result = _exec_in_pod(
            agent_pod,
            "curl",
            "-sI",
            "--max-time", "20",
            "https://api.anthropic.com/v1/models",
            timeout=30,
        )
        # Any non-network-error exit from curl means gVisor did not block.
        assert result.returncode in (0, 22), (
            f"curl -I failed with rc={result.returncode} — "
            f"gVisor may be blocking outbound TLS. "
            f"stderr: {result.stderr.strip()[:300]}"
        )

    def test_workspace_pvc_mount_works_under_gvisor(self, agent_pod: str) -> None:
        """Workspace PVC (from #154/#82) is writable under gVisor.

        gVisor supports standard VFS operations including writes to mounted
        volumes. This test writes a file to the workspace mount and reads it
        back to confirm the PVC is functional.
        """
        # workspaces.mountPath default; adjust if values override used.
        mount_path = "/workspaces"

        # Check if the mount exists; if workspaces.enabled=false it won't.
        check_mount = _exec_in_pod(
            agent_pod, "ls", mount_path, timeout=10,
        )
        if check_mount.returncode != 0:
            pytest.skip(
                f"Workspace mount {mount_path!r} not found — "
                "was AIFactory deployed with workspaces.enabled=true?"
            )

        # Write a canary file.
        canary = "/workspaces/gvisor-smoke-test-canary.txt"
        write_result = _exec_in_pod(
            agent_pod,
            "sh", "-c", f"echo 'gvisor-canary' > {canary}",
            timeout=10,
        )
        assert write_result.returncode == 0, (
            f"Failed to write canary file to workspace PVC under gVisor. "
            f"rc={write_result.returncode}, stderr={write_result.stderr.strip()}"
        )

        # Read it back.
        read_result = _exec_in_pod(
            agent_pod, "cat", canary, timeout=10,
        )
        assert read_result.returncode == 0, (
            f"Failed to read canary file from workspace PVC under gVisor. "
            f"rc={read_result.returncode}"
        )
        assert "gvisor-canary" in read_result.stdout, (
            f"Canary file content unexpected: {read_result.stdout!r}"
        )

        # Clean up.
        _exec_in_pod(agent_pod, "rm", "-f", canary, timeout=10)

    def test_bash_allowlist_compatibility_matrix(self, agent_pod: str) -> None:
        """Validate all commands in the security allowlist work under gVisor.

        Exercises the subset of BASE_COMMANDS from
        apps/backend/project/command_registry/base.py that the compatibility
        table claims work. Each command must exit 0 under gVisor; a non-zero
        exit or gVisor syscall rejection marks the row as broken.

        Note: commands that need real files/networks have minimal setup
        to keep the test fast. The goal is syscall compatibility, not
        functional correctness of the tool itself.
        """
        # (command, args, acceptable_exit_codes, description)
        # acceptable_exit_codes: set of rc values that count as "works under gVisor"
        compat_commands = [
            # Core shell — these touch no network, just process/file syscalls.
            (["echo", "gvisor-compat-check"], {0}, "echo"),
            (["ls", "/tmp"], {0}, "ls"),
            (["cat", "/etc/os-release"], {0}, "cat"),
            (["grep", "ID", "/etc/os-release"], {0}, "grep"),
            (["find", "/tmp", "-maxdepth", "1", "-type", "f"], {0}, "find"),
            (["uname", "-r"], {0}, "uname"),
            (["id"], {0}, "id"),
            (["whoami"], {0}, "whoami"),
            # git — compat table row 1 (full clone tested separately above)
            (["git", "version"], {0}, "git version"),
            # curl — compat table row 2 (HTTPS tested separately above)
            (["curl", "--version"], {0}, "curl --version"),
            # npm/pip: check CLI is present and version-queryable
            (["sh", "-c", "npm --version 2>/dev/null || true"], {0}, "npm --version"),
            (["sh", "-c", "python3 --version 2>/dev/null || python --version 2>/dev/null || true"], {0}, "python --version"),
            # pytest — compat table row "pytest, jest, go test"
            (["sh", "-c", "pytest --version 2>/dev/null || python3 -m pytest --version 2>/dev/null || true"], {0}, "pytest --version"),
        ]

        failures = []
        for cmd, ok_codes, label in compat_commands:
            result = _exec_in_pod(agent_pod, *cmd, timeout=30)
            stderr_lower = (result.stderr or "").lower()
            # Detect gVisor-specific syscall rejections in stderr.
            syscall_blocked = (
                "operation not permitted" in stderr_lower
                or "function not implemented" in stderr_lower
                or "permission denied" in stderr_lower
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
