"""
Trailing-gate runner (#376 solution D)
======================================

The two slowest steps of a build were the final ``mypy --strict`` and ``pytest``
gates: each ran as its own full agent turn that re-hydrates context, iterates,
and self-verifies. For a batched parallel wave that's wasteful — the gates only
need to run *once* over the merged result, and an agent should only be spun up
if a gate actually fails.

This module runs detected project gates **directly as subprocesses** (no agent
turn) and reports structured pass/fail. The caller decides what to do with a
failure (typically: hand the failing output to the existing QA/fix loop). It is
deliberately best-effort and side-effect-free beyond running the commands:
detection is conservative, missing tools are reported as *skipped* (not failed),
and nothing here ever mutates the plan or the repo.

Command execution is injected (``runner``) so the detection and
result-aggregation logic is unit-testable without spawning real processes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# How long any single gate may run before we treat it as failed (seconds).
GATE_TIMEOUT_SECONDS = 600

# Max characters of captured output to retain per gate (keeps logs/markers sane).
_OUTPUT_TAIL_CHARS = 4000


@dataclass
class Gate:
    """A single verification command to run over the merged build."""

    name: str  # e.g. "mypy", "pytest"
    command: list[str]  # argv, e.g. ["mypy", "--strict", "."]


@dataclass
class GateResult:
    """Outcome of running one gate."""

    name: str
    passed: bool
    skipped: bool = False
    exit_code: int | None = None
    output_tail: str = ""

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped"
        return "passed" if self.passed else "failed"


def detect_gates(project_dir: Path) -> list[Gate]:
    """Detect likely lint/type/test gates from project marker files.

    Conservative on purpose — only well-known, low-risk commands. Returns an
    empty list when nothing recognizable is present (the caller then skips the
    gate step entirely).
    """
    gates: list[Gate] = []
    p = project_dir

    # --- Python ---
    has_pyproject = (p / "pyproject.toml").exists()
    has_mypy_cfg = (
        (p / "mypy.ini").exists()
        or (p / ".mypy.ini").exists()
        or (has_pyproject and _file_contains(p / "pyproject.toml", "[tool.mypy]"))
    )
    if has_mypy_cfg:
        gates.append(Gate("mypy", ["mypy", "."]))

    has_pytest = (
        (p / "pytest.ini").exists()
        or (p / "tests").is_dir()
        or (has_pyproject and _file_contains(p / "pyproject.toml", "[tool.pytest"))
    )
    if has_pytest:
        gates.append(Gate("pytest", ["pytest", "-q"]))

    # --- Node / TypeScript ---
    pkg = p / "package.json"
    if pkg.exists():
        if (p / "tsconfig.json").exists():
            gates.append(Gate("tsc", ["npx", "--no-install", "tsc", "--noEmit"]))
        scripts = _package_scripts(pkg)
        if "lint" in scripts:
            gates.append(Gate("lint", ["npm", "run", "lint", "--if-present"]))
        if "test" in scripts:
            gates.append(Gate("test", ["npm", "test", "--if-present"]))

    # --- Rust / Go ---
    if (p / "Cargo.toml").exists():
        gates.append(Gate("cargo-test", ["cargo", "test", "--quiet"]))
    if (p / "go.mod").exists():
        gates.append(Gate("go-test", ["go", "test", "./..."]))

    return gates


def _file_contains(path: Path, needle: str) -> bool:
    try:
        return needle in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _package_scripts(pkg_path: Path) -> dict:
    try:
        import json

        return json.loads(pkg_path.read_text(encoding="utf-8")).get("scripts", {}) or {}
    except (OSError, ValueError):
        return {}


def _default_runner(command: list[str], cwd: Path) -> tuple[int | None, str]:
    """Run a command, returning (exit_code, combined_output_tail).

    exit_code is None when the tool is missing (FileNotFoundError) — the caller
    treats that as *skipped*, not failed.
    """
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GATE_TIMEOUT_SECONDS,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, combined[-_OUTPUT_TAIL_CHARS:]
    except FileNotFoundError:
        return None, f"command not found: {command[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"gate timed out after {GATE_TIMEOUT_SECONDS}s"


def _sandbox_runner(image: str) -> Callable[[list[str], Path], tuple[int | None, str]]:
    """Run a gate inside a per-task factory-sandbox container (RFC-0005 #61).

    Same ``(command, cwd) -> (exit_code, output_tail)`` contract as the host
    runner. The worktree is mounted rw at /work; exit 127 maps to None so a
    missing tool is still reported as *skipped*, matching the host runner.
    """
    from core.factory_sandbox import FactorySandbox

    network = os.environ.get("AIFACTORY_SANDBOX_NETWORK", "none")

    def run(command: list[str], cwd: Path) -> tuple[int | None, str]:
        try:
            res = FactorySandbox(image, network=network, repo_rw=True).run(
                [shlex.join(command)], workdir=str(cwd), timeout=GATE_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - sandbox issues are gate failures, never crashes
            return 1, f"factory-sandbox error: {exc}"
        code = None if res.exit_code == 127 else res.exit_code
        return code, res.output[-_OUTPUT_TAIL_CHARS:]

    return run


def _kube_runner(image: str) -> Callable[[list[str], Path], tuple[int | None, str]]:
    """Run a gate as an ephemeral k8s Job (#68 in-cluster backend). Same contract.

    The task worktree (``cwd``) is co-mounted into the Job via the data PVC's
    subPath (``AIFACTORY_SANDBOX_REPO_PVC``, default ``aifactory-data``; mount
    prefix ``AIFACTORY_DATA_ROOT``, default ``/home/nonroot/.aifactory``), so
    code-reading gates run against real files. Set the PVC to "" to disable the
    mount and run toolchain-only Jobs. Unlike the docker runner this reports
    Job success/failure (not the container's real exit code), so a missing tool
    surfaces as a *failure* rather than *skipped* — acceptable, since a per-task
    image is expected to carry its own toolchain.
    """
    from core.kube_sandbox import KubeJobSandbox

    repo_pvc = os.environ.get("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data") or None
    data_root = os.environ.get("AIFACTORY_DATA_ROOT", "/home/nonroot/.aifactory")

    def run(command: list[str], cwd: Path) -> tuple[int | None, str]:
        try:
            res = KubeJobSandbox(image, repo_pvc=repo_pvc, data_root=data_root).run(
                [shlex.join(command)], workdir=str(cwd), timeout=GATE_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - sandbox issues are gate failures, never crashes
            return 1, f"kube-sandbox error: {exc}"
        return (res.exit_code if res.ok else 1), res.output[-_OUTPUT_TAIL_CHARS:]

    return run


def _select_runner() -> Callable[[list[str], Path], tuple[int | None, str]]:
    """Default runner: host subprocess, unless AIFACTORY_SANDBOX_GATES routes gates
    into a per-task sandbox (#61 runtime adoption). OFF by default → no change.
    AIFACTORY_SANDBOX_BACKEND selects docker (host runtime) or kubejob (in-cluster).
    """
    enabled = os.environ.get("AIFACTORY_SANDBOX_GATES", "").lower() in (
        "1",
        "true",
        "yes",
    )
    image = os.environ.get("AIFACTORY_SANDBOX_IMAGE", "")
    if enabled and image:
        backend = os.environ.get("AIFACTORY_SANDBOX_BACKEND", "docker").lower()
        if backend == "kubejob":
            logger.info("[gate] routing gates through k8s Job sandbox image %s", image)
            return _kube_runner(image)
        logger.info(
            "[gate] routing gates through docker factory-sandbox image %s", image
        )
        return _sandbox_runner(image)
    if enabled and not image:
        logger.warning(
            "[gate] AIFACTORY_SANDBOX_GATES set but AIFACTORY_SANDBOX_IMAGE empty; using host runner"
        )
    return _default_runner


async def run_gates(
    project_dir: Path,
    gates: list[Gate] | None = None,
    *,
    runner: Callable[[list[str], Path], tuple[int | None, str]] | None = None,
) -> list[GateResult]:
    """Run gates once over the merged build and return structured results.

    Args:
        project_dir: The task worktree to run gates in.
        gates: Gates to run; auto-detected from ``project_dir`` when omitted.
        runner: Injectable ``(command, cwd) -> (exit_code, output_tail)`` for
            tests. ``exit_code is None`` means the tool was missing → skipped.

    Returns:
        One GateResult per gate (empty list if no gates detected). Never raises
        on a gate failure — failure is data, returned to the caller.
    """
    if gates is None:
        gates = detect_gates(project_dir)
    if not gates:
        return []

    run = runner or _select_runner()
    results: list[GateResult] = []
    for gate in gates:
        exit_code, output = await asyncio.to_thread(run, gate.command, project_dir)
        if exit_code is None:
            results.append(
                GateResult(gate.name, passed=True, skipped=True, output_tail=output)
            )
            logger.info("[gate] %s skipped (tool not available)", gate.name)
            continue
        passed = exit_code == 0
        results.append(
            GateResult(
                gate.name,
                passed=passed,
                exit_code=exit_code,
                output_tail=output,
            )
        )
        logger.info(
            "[gate] %s %s (exit %s)",
            gate.name,
            "passed" if passed else "FAILED",
            exit_code,
        )
    return results


def summarize_gates(results: list[GateResult]) -> str:
    """One-line human summary, e.g. 'mypy: passed, pytest: FAILED, tsc: skipped'."""
    return ", ".join(f"{r.name}: {r.status}" for r in results) or "no gates detected"


def failing_gates(results: list[GateResult]) -> list[GateResult]:
    """Gates that actually failed (skipped tools are not failures)."""
    return [r for r in results if not r.passed and not r.skipped]
