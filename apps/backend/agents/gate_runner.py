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

from core.nix_env import nix_in_image

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
    # Exit code by which this gate reports "I could not determine anything".
    # Reported as *skipped*, never as *passed*, so a check that never really ran
    # reads differently from one that ran clean — otherwise a dead lane is
    # quieter than a failing one (#1123). None = no such code.
    skip_code: int | None = None
    # Where to run it. None = the project root. A monorepo keeps its build file
    # in the module (lanes/kotlin-core), and `gradle test` at the root finds no
    # build to run.
    cwd: Path | None = None


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

    gates.extend(_descriptor_gates(p, already={g.name for g in gates}))

    return gates


# Build files that mark the root of a module the descriptor's command can run in.
# Keyed by descriptor name, deliberately NOT one shared list: this fleet's repos
# are polyglot, and a generic marker at the root — a package.json beside
# lanes/kotlin-core — would capture the Kotlin gate and run `gradle test` in a
# directory with no Gradle build.
_MODULE_MARKERS_BY_LANGUAGE: dict[str, tuple[str, ...]] = {
    "kotlin": (
        "settings.gradle.kts",
        "settings.gradle",
        "build.gradle.kts",
        "build.gradle",
    ),
    "java": ("pom.xml", "settings.gradle.kts", "build.gradle.kts", "build.gradle"),
    "scala": ("build.sbt", "build.gradle.kts", "build.gradle"),
    "swift": ("Package.swift",),
    "rust": ("Cargo.toml",),
    "go": ("go.mod",),
    "python": ("pyproject.toml", "setup.py", "setup.cfg"),
    "javascript": ("package.json",),
    "typescript": ("package.json",),
}
_MODULE_MAX_DEPTH = 4
_MODULE_SKIP_DIRS = frozenset(
    {"node_modules", "vendor", "build", "dist", "target", "out", ".git", ".gradle"}
)


def _module_dir_for(project_dir: Path, language: str) -> Path | None:
    """The nearest directory holding a build file — the root, or a nested module.

    The hardcoded families above all test ``project_dir / marker``, so a repo
    whose build lives under lanes/ or services/ produced no gates at all.
    """
    markers = _MODULE_MARKERS_BY_LANGUAGE.get(language.lower())
    if not markers:
        return None
    for marker in markers:
        if (project_dir / marker).exists():
            return project_dir
    for depth in range(1, _MODULE_MAX_DEPTH + 1):
        prefix = "/".join(["*"] * depth)
        for marker in markers:
            for hit in sorted(project_dir.glob(f"{prefix}/{marker}")):
                relative = hit.relative_to(project_dir)
                if not _MODULE_SKIP_DIRS.intersection(relative.parts):
                    return hit.parent
    return None


def _descriptor_gates(project_dir: Path, *, already: set[str]) -> list[Gate]:
    """Gates from the detected languages' own descriptors.

    detect_gates knew Python, Node, Rust and Go, and nothing else — so a Kotlin
    project produced no gates, the gate step ran nothing, and the build reported
    success having executed no test at all (AIFactory#1491, #1496). Rather than
    hardcode another family, ask the language descriptor: it already declares
    the unit lane and its command, and says why when a lane cannot run.
    """
    try:
        from core.language_descriptors import resolve_language  # noqa: PLC0415
    except ImportError:
        try:
            from language_descriptors import resolve_language  # noqa: PLC0415
        except ImportError:
            return []
    try:
        from project.stack_detector import StackDetector  # noqa: PLC0415
    except ImportError:
        return []

    try:
        languages = StackDetector(project_dir).detect_all().languages or []
    except Exception as exc:  # noqa: BLE001 - detection must never break the build
        logger.info("[gate] stack detection failed (%s)", type(exc).__name__)
        return []

    out: list[Gate] = []
    for language in languages:
        descriptor = resolve_language(str(language))
        if descriptor is None:
            continue
        unit = descriptor.lane("unit")
        if unit is None or not unit.available or not unit.command:
            # A lane the descriptor marks unavailable states its reason; that is
            # an honest absence, not a gate.
            if unit is not None and not unit.available:
                logger.info(
                    "[gate] %s unit lane unavailable: %s", descriptor.name, unit.reason
                )
            continue
        name = f"{descriptor.name}-unit"
        if name in already:
            continue
        module_dir = _module_dir_for(project_dir, descriptor.name)
        if module_dir is None:
            # The language is present but its build file is not. Running its
            # command from the repo root would fail for a reason that has
            # nothing to do with the code under test.
            logger.info(
                "[gate] %s detected but no build file found; no gate", descriptor.name
            )
            continue
        out.append(Gate(name, shlex.split(unit.command), cwd=module_dir))
        already.add(name)
    return out


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


def _nix_wrap(command: list[str], *, mount: str = "/work") -> list[str]:
    """Wrap a gate command to run inside the per-task Nix dev shell (RFC-0005
    Tier A). `nix develop path:/work#default -c bash -c "<cmd>"`. ``path:`` (not a
    bare dir) is REQUIRED for a co-mounted git worktree — a bare ref makes nix use
    the git fetcher, which rejects the repo on a uid mismatch and ignores the
    untracked generated flake.nix (proven live in TFactory 2026-06-17).
    """
    inner = shlex.join(command)
    return [
        "nix",
        "develop",
        f"path:{mount}#default",
        "--command",
        "bash",
        "-c",
        inner,
    ]


_STORE_ENV_VARS = (
    "S3_ENDPOINT",
    "S3_BUCKET",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_REGION",
)


def _store_env() -> dict[str, str]:
    """The object-store vars the unpack initContainer needs, as present."""
    return {k: v for k in _STORE_ENV_VARS if (v := os.environ.get(k))}


def _packed_workspace_for(mount_root: Path) -> str | None:
    """Pack the worktree so a gate Job can unpack it; None when it cannot.

    AIFactory#1524: on the packed path the code lives in the build Job's own
    emptyDir, which no other pod can mount, and the gate cannot simply run
    in-process either — the build image's Nix store has no toolchain closure and
    no egress to fetch one, so `nix develop` tries to build gcc from source.
    Sending the code to the gate image (which does have the closure, and runs as
    root so nix can write its store) is what actually reaches a green gate.
    """
    if not os.environ.get("S3_ENDPOINT"):
        return None
    try:
        from core.artifact_store import (  # noqa: PLC0415
            ArtifactRef,
            ArtifactStore,
            pack_workspace,
        )

        ref = ArtifactRef(
            service="aifactory", job_id=f"gate-{mount_root.name}", role="workspace"
        )
        uri = pack_workspace(ArtifactStore(), ref, mount_root)
    except Exception as exc:  # noqa: BLE001 - packing must never crash a gate
        logger.warning("[gate] could not pack the worktree for a gate Job: %s", exc)
        return None
    logger.info("[gate] packed the worktree for the gate Job -> %s", uri)
    return uri


def _nix_kube_runner(image: str) -> Callable[[list[str], Path], tuple[int | None, str]]:
    """k8s-Job gate runner that runs each gate inside the per-task Nix dev shell.

    Identical to ``_kube_runner`` except the gate command is wrapped in
    ``nix develop`` so the toolchain comes from the worktree's flake.nix (built by
    the planner/coder from the contract `environment`), not baked into the image.
    The build env thus matches TFactory's verify env — no drift. Requires
    flake.nix in the co-mounted worktree (materialize_flake_into writes it).
    """
    from core.kube_sandbox import KubeJobSandbox, repo_is_mountable

    repo_pvc = os.environ.get("AIFACTORY_SANDBOX_REPO_PVC", "aifactory-data") or None
    data_root = os.environ.get("AIFACTORY_DATA_ROOT", "/home/nonroot/.aifactory")
    # RFC-0016 #197: opt-in warm /nix/store PVC so the toolchain closure persists
    # across Nix Jobs instead of cold-fetching each run. Absent/empty → cold
    # behavior (no mount), so nothing breaks if the PVC is not provisioned.
    # #253: but drop it when nix is baked into the image. This Job already mounts
    # the RWO repo PVC; adding the RWO nix-store PVC makes the pod unschedulable
    # whenever the two PVs stranded on different nodes (the live factory cluster:
    # aifactory-data on the server, aifactory-nix-store on the agent). See
    # nix_in_image for why the image is a sufficient /nix source.
    nix_store_pvc = (
        None
        if nix_in_image()
        else (os.environ.get("AIFACTORY_NIX_STORE_PVC", "") or None)
    )
    if nix_store_pvc:
        logger.info("[gate] warm Nix store PVC %s mounted at /nix", nix_store_pvc)
    else:
        logger.info("[gate] no warm Nix store PVC — /nix resolves from image %s", image)

    def run(command: list[str], cwd: Path) -> tuple[int | None, str]:
        # Mount the directory that actually holds the flake, and step down into
        # the gate's own module from inside the shell. Mounting the module would
        # hide the flake from `nix develop path:/work#default`.
        mount_root = _flake_root_for(cwd)
        argv = _mounted_at(command, cwd, mount_root)
        if not repo_is_mountable(str(mount_root), data_root):
            # AIFactory#1491: on the packed path /work is a pod-local emptyDir, so
            # a nested Job mounting the data PVC would see no repo and run the
            # gate against an empty directory — a red that measured nothing.
            # Send the code to the gate Job instead (#1524).
            packed = _packed_workspace_for(mount_root)
            if packed:
                try:
                    res = KubeJobSandbox(
                        image,
                        nix_store_pvc=nix_store_pvc,
                        workspace_uri=packed,
                        unpack_image=os.environ.get("AIFACTORY_BUILD_IMAGE") or None,
                        store_env=_store_env(),
                    ).run(
                        [shlex.join(_nix_wrap(argv))],
                        timeout=GATE_TIMEOUT_SECONDS,
                    )
                except Exception as exc:  # noqa: BLE001 - sandbox issues are gate failures
                    return 1, f"nix-kube-sandbox error: {exc}"
                return (res.exit_code if res.ok else 1), res.output[
                    -_OUTPUT_TAIL_CHARS:
                ]
            # No object store configured: run the dev shell in-process. The
            # build image may lack the toolchain closure (#1524), in which case
            # this fails with a readable reason rather than a bare exit 1.
            logger.info(
                "[gate] %s is not on the data PVC and no object store is "
                "configured — running the Nix shell in-process",
                mount_root,
            )
            return _default_runner(_nix_wrap(argv, mount=str(mount_root)), mount_root)
        try:
            res = KubeJobSandbox(
                image,
                repo_pvc=repo_pvc,
                data_root=data_root,
                nix_store_pvc=nix_store_pvc,
            ).run(
                [shlex.join(_nix_wrap(argv))],
                workdir=str(mount_root),
                timeout=GATE_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - sandbox issues are gate failures
            return 1, f"nix-kube-sandbox error: {exc}"
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
        if backend == "nixjob":
            logger.info(
                "[gate] routing gates through Nix k8s Job (RFC-0005 Tier A) image %s",
                image,
            )
            return _nix_kube_runner(image)
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


def _flake_root_for(cwd: Path) -> Path:
    """The nearest ancestor of *cwd* holding a flake, or *cwd* itself.

    Only the Nix runner needs this: it mounts the cwd it is given at /work, and
    ``nix develop path:/work#default`` reads the flake from there. A gate runs in
    the module that holds its build file (lanes/kotlin-core), while
    materialize_flake_into writes the flake at the worktree root — so mounting
    the module made nix look for a flake inside it and report

        error: path '/nix/store/…-source/flake.nix' does not exist

    with a store hash that never moved however the root was edited, because the
    root was not what it was reading (AIFactory#1491).
    """
    for candidate in (cwd, *cwd.parents):
        if (candidate / "flake.nix").exists():
            return candidate
    return cwd


def _mounted_at(command: list[str], cwd: Path, mount_root: Path) -> list[str]:
    """``command``, entered from *mount_root* instead of *cwd*."""
    if mount_root == cwd:
        return command
    try:
        relative = cwd.relative_to(mount_root)
    except ValueError:
        return command
    return ["bash", "-c", f"cd {shlex.quote(str(relative))} && {shlex.join(command)}"]


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
        exit_code, output = await asyncio.to_thread(
            run, gate.command, gate.cwd or project_dir
        )
        if exit_code is None or exit_code == gate.skip_code:
            results.append(
                GateResult(gate.name, passed=True, skipped=True, output_tail=output)
            )
            logger.info(
                "[gate] %s skipped (%s)",
                gate.name,
                "tool not available" if exit_code is None else "nothing determined",
            )
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
