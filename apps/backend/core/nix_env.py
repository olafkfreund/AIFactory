"""RFC-0005 Tier A (AIFactory build side) — materialize the per-task Nix flake.

The planner declares the toolchain in the contract ``environment`` manifest; the
coder builds/verifies inside it. This writes a ``flake.nix`` into the task
worktree (from the vendored ``nix_provisioner``) so the Nix gate runner can
``nix develop path:/work -c <gate>`` against it — the SAME flake TFactory verifies
in, so the build env and verify env cannot drift.

Keep the provisioner in sync with the hub + TFactory's vendored copy.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from core.nix_provisioner import Manifest, generate_flake

logger = logging.getLogger(__name__)

_FLAKE = "flake.nix"

# Historical name (RFC-0017 #190) — it predates the gate path also honouring it.
_ENV_NIX_IN_IMAGE = "AIFACTORY_PACKED_NIX_IN_IMAGE"


def nix_in_image() -> bool:
    """True when a Job should source ``/nix`` from its image, not the warm PVC.

    The warm ``*-nix-store`` PVC is RWO ``local-path``, so its PV is nodeAffinity
    -pinned to whichever node first consumed it. Mounting it (a) re-pins the Job
    to that node — deadlocking outright when the repo PVC stranded on a *different*
    node, since no node then satisfies both — and (b) serialises concurrent Jobs
    on one mutex (TFactory#623).

    The ``-nix`` images bake the very store the PVC is seeded from (kube_sandbox's
    seed initContainer copies the image's ``/nix`` into it), so dropping the mount
    is not a correctness trade: it costs only the closures realised *during* a
    task, which are re-fetched per Job instead of persisting. Speed for
    schedulability.

    Read by the gate/verify path (``gate_runner``), including gates inside the
    build Job. The build Job forwards both ``AIFACTORY_PACKED_NIX_IN_IMAGE`` and
    ``AIFACTORY_NIX_STORE_PVC`` (#1736), so one gitops flip reaches every gate; a
    gate missing the flip is how it kept its pin after #258 (#253). The build Job
    itself mounts no store.
    """
    return os.environ.get(_ENV_NIX_IN_IMAGE, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def is_nix_environment(env: dict | None) -> bool:
    if not env:
        return False
    return (env.get("provisioning") or {}).get("method") == "nix"


def environment_of(contract: dict | None) -> dict | None:
    """The contract ``environment`` block, or None."""
    if not contract:
        return None
    env = contract.get("environment")
    return env if isinstance(env, dict) else None


def materialize_flake_into(project_dir: Path, env: dict | None) -> bool:
    """Write ``flake.nix`` into ``project_dir`` from the contract environment.

    Returns True when a flake was written (nix env present), else False. Respects
    a repo-owned flake unless the manifest is ``generated``.
    """
    if not is_nix_environment(env):
        return False
    assert env is not None
    m = Manifest.from_contract(env)
    flake_path = Path(project_dir) / _FLAKE
    if flake_path.exists() and not m.provisioning_generated:
        logger.info("nix_env: respecting repo-owned %s", _FLAKE)
        return True
    flake_path.write_text(
        generate_flake(_with_language_from_project(env, Path(project_dir))),
        encoding="utf-8",
    )
    logger.info("nix_env: wrote generated %s into %s", _FLAKE, project_dir)
    return True


_PY_PROJECT_MARKERS = (
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    "pytest.ini",
)


def _with_language_from_project(
    env: dict[str, Any], project_dir: Path
) -> dict[str, Any]:
    """Fill an UNSET language from the project when it proves JavaScript (#1443).

    `generate_flake` treats an unset language as python, deliberately, for
    manifests that omit it. That is wrong for a project that is plainly JS: it
    got a python + pytest shell and no node, which is how a JS build came to
    offer `pytest -q` as its test command. Only a package.json with no python
    marker overrides the default; anything else keeps it.
    """
    if env.get("language") or "pytest" in " ".join(env.get("verify_commands") or []):
        return env
    if not (project_dir / "package.json").is_file():
        return env
    if any((project_dir / m).exists() for m in _PY_PROJECT_MARKERS):
        return env
    return {**env, "language": "javascript"}


_MAX_MODULE_DEPTH = 4
_MAX_FLAKE_JDK = 21  # the vendored provisioner's java table ships jdk21
_SKIP_DIRS = frozenset(
    {"node_modules", "vendor", "build", "dist", "target", "out", ".git", ".gradle"}
)
_GRADLE_FILES = frozenset({"build.gradle", "build.gradle.kts", "gradlew"})
_JDK_PROP = re.compile(
    r"<(?:maven\.compiler\.release|maven\.compiler\.source|java\.version)>"
    r"\s*(?:1\.)?(\d+)"
)
# java is found by _scan_java markers only: StackDetector globs **/*.java into
# nested task worktrees.
_DESCRIPTORLESS = {"rust": "rust", "go": "go"}
_VERIFY = {
    "rust": ["cargo test --quiet"],
    "go": ["go test ./..."],
    "node": ["npm test --if-present"],
}


def _scan_java(project_dir: Path) -> tuple[list[Path], bool]:
    """(pom.xml paths, any Gradle build file) at the root or nested modules."""
    poms: list[Path] = []
    gradle = False
    for directory, dirs, files in os.walk(project_dir):
        depth = len(Path(directory).relative_to(project_dir).parts)
        dirs[:] = (
            []
            if depth >= _MAX_MODULE_DEPTH
            else [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        )
        if "pom.xml" in files:
            poms.append(Path(directory) / "pom.xml")
        gradle = gradle or bool(_GRADLE_FILES & set(files))
    return poms, gradle


def _warn_if_pom_needs_newer_jdk(poms: list[Path]) -> None:
    # Regex, not an XML parser: the pom is untrusted repo content.
    for pom in poms:
        try:
            text = pom.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _JDK_PROP.finditer(text):
            if int(match.group(1)) > _MAX_FLAKE_JDK:
                logger.warning(
                    "nix_env: %s asks for Java %s but the generated flake ships jdk21",
                    pom,
                    match.group(1),
                )


def _builtin_languages(
    languages: list[str], project_dir: Path, poms: list[Path], gradle: bool
) -> list[str]:
    """Descriptor-less languages present, in detection order, node folded in."""
    found: list[str] = []
    for language in languages:
        name = (
            "node"
            if language in ("javascript", "typescript")
            else _DESCRIPTORLESS.get(language)
        )
        if name and name not in found:
            found.append(name)
    for marker, name in (("Cargo.toml", "rust"), ("go.mod", "go")):
        if (project_dir / marker).is_file() and name not in found:
            found.append(name)
    if (poms or gradle) and "java" not in found:
        found.append("java")
    return found


def _verify_commands(descriptor: Any, primary: str, has_pom: bool) -> list[str]:
    if descriptor is not None:
        unit = descriptor.lane("unit")
        # A lane the descriptor itself marks unavailable is not a verify
        # command; the descriptor states why, and inventing one would be worse
        # than leaving it out.
        if unit is not None and unit.available and unit.command:
            return [unit.command]
        return []
    if primary == "java":
        return [
            "mvn -B -q test" if has_pom else "gradle test --no-daemon --console=plain"
        ]
    return list(_VERIFY.get(primary, []))


def infer_environment(project_dir: Path) -> dict[str, object] | None:
    """An environment manifest derived from what the project actually is.

    The contract is the source of truth when there is one. A task created
    without one — `POST /api/tasks/create-and-run` from a plain description,
    for instance — carries `environment: null`, so
    :func:`materialize_flake_into` writes nothing and returns False WITHOUT
    logging. The consequences were invisible and severe: no flake means
    `nix develop path:/work#default` has nothing to enter, so the gate lane
    cannot supply a toolchain, so the coder falls back to bare shell in a
    control-plane pod. A live Kotlin task reported `gradle: exit 127`, ran no
    gate at all (`build_report.json` had `gates: null`), and was still returned
    as APPROVED on inspection (#1491, #1496).

    Descriptors supply Kotlin and Swift; builtin language markers supply the
    other toolchains. Python alone keeps its existing default harness.
    """
    try:
        # Lazy: the descriptors are a vendored drop that a consumer may not
        # carry, and `core` must not import them at module scope.
        from language_descriptors import resolve_language  # noqa: PLC0415
    except ImportError:  # pragma: no cover - environment guard
        try:
            from .language_descriptors import resolve_language  # noqa: PLC0415
        except ImportError:
            logger.info("nix_env: language descriptors unavailable; cannot infer")
            return None

    try:
        # Lazy: `core` importing the `project` package at module scope would
        # close an import cycle (project.analyzer reaches back into core).
        from project.stack_detector import StackDetector  # noqa: PLC0415
    except ImportError:  # pragma: no cover - environment guard
        logger.info("nix_env: stack detector unavailable; cannot infer")
        return None

    try:
        stack = StackDetector(Path(project_dir)).detect_all()
    except Exception as exc:  # noqa: BLE001 - detection must never break a build
        logger.info(
            "nix_env: stack detection failed (%s); cannot infer", type(exc).__name__
        )
        return None

    languages = [str(x) for x in (getattr(stack, "languages", []) or [])]
    project_dir = Path(project_dir)
    descriptor = next(
        (d for d in map(resolve_language, languages) if d is not None), None
    )
    poms, gradle = _scan_java(project_dir)
    builtins = _builtin_languages(languages, project_dir, poms, gradle)

    if descriptor is not None:
        primary = descriptor.name
    elif "python" in languages and builtins:
        primary = "python"  # keep the pytest harness; builtins ride as packages
    elif builtins:
        primary = builtins[0]
    else:
        logger.info(
            "nix_env: no supported language detected (%s)",
            ", ".join(languages) or "none",
        )
        return None

    verify = _verify_commands(descriptor, primary, bool(poms))

    _warn_if_pom_needs_newer_jdk(poms)
    # Every other detected toolchain rides in as system_packages so gates for all
    # of them run in one flake. A Gradle-only "java" beside a descriptor language
    # is that same build (the descriptor ships jdk21 + gradle): skip maven.
    packages = [
        name
        for name in builtins
        if name != primary and not (name == "java" and descriptor and not poms)
    ]
    if "rust" in builtins:
        packages.append("clippy")
    if gradle:
        packages.append("gradle")
    env: dict[str, object] = {
        "language": "javascript" if primary == "node" else primary,
        "provisioning": {
            "method": "nix",
            "generated": not (project_dir / _FLAKE).exists(),
        },
        "verify_commands": verify,
        "network": descriptor.network if descriptor is not None else None,
        "inferred": True,
    }
    if packages:
        env["system_packages"] = packages
    logger.info("nix_env: inferred %s environment (%s)", primary, ", ".join(packages))
    return env
