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
from pathlib import Path

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

    Read by both Job paths (build: ``build_backend``; gate/verify: ``gate_runner``)
    so one gitops flip cannot land on one path and silently miss the other — which
    is exactly how the gate path kept its pin after #258 flipped the build path.
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
    flake_path.write_text(generate_flake(env), encoding="utf-8")
    logger.info("nix_env: wrote generated %s into %s", _FLAKE, project_dir)
    return True


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

    Nothing here is invented: the language comes from the same StackDetector the
    command allowlist is built from, and its toolchain and verify command come
    from that language's own descriptor (`contracts/languages/<name>.yaml`) —
    the file that already declares, for Kotlin, `nix.packages: [kotlin, gradle,
    jdk21]` and a unit lane of `gradle test --no-daemon --console=plain`.

    Returns None when no detected language has a descriptor, so a project this
    cannot speak for keeps today's behaviour rather than getting a guessed
    toolchain.
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

    for language in getattr(stack, "languages", []) or []:
        descriptor = resolve_language(str(language))
        if descriptor is None:
            continue
        unit = descriptor.lane("unit")
        # A lane the descriptor itself marks unavailable is not a verify
        # command; the descriptor states why, and inventing one would be worse
        # than leaving it out.
        verify = (
            [unit.command]
            if unit is not None and unit.available and unit.command
            else []
        )
        logger.info(
            "nix_env: inferred %s environment from the detected stack (%s)",
            descriptor.name,
            ", ".join(descriptor.nix_packages),
        )
        return {
            "language": descriptor.name,
            "provisioning": {"method": "nix", "generated": True},
            "verify_commands": verify,
            "network": descriptor.network,
            "inferred": True,
        }

    logger.info(
        "nix_env: no detected language has a descriptor (%s); not inferring",
        ", ".join(getattr(stack, "languages", []) or []) or "none detected",
    )
    return None
