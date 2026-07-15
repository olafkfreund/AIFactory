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
