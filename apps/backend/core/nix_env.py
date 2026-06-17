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
from pathlib import Path

from core.nix_provisioner import Manifest, generate_flake

logger = logging.getLogger(__name__)

_FLAKE = "flake.nix"


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
