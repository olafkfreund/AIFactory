"""Deterministic deploy-artifact scaffolder (RFC-0013, consuming side).

PFactory's planner resolves a managed-PaaS target from the plan text and records
it on the contract ``deployment`` block (``deploy_system`` +
``managed_services``). This module turns that block into the concrete deploy
artifacts — the cloud's Terraform, the GitHub Actions deploy workflow, and a
TFactory verification target — by materialising the *proven* templates under
``deploy_templates/`` into the code worktree. It is deterministic (no LLM): the
templates are the ones the Factory has deployed and tested live, so the infra is
never a model guess.

Conservative: only PaaS deploy systems are scaffolded, existing files are never
clobbered (the coder's own output wins), and a block with no PaaS target writes
nothing.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from pathlib import Path

_TEMPLATES = Path(__file__).parent / "deploy_templates"

# Contract deploy_system -> vendored template directory.
_PAAS_DIRS: dict[str, str] = {
    "gcp-cloud-run": "gcp",
    "azure-container-apps": "azure",
    "aws-app-runner": "aws",
}


def _place(src: Path, dest: Path, written: list[str], root: Path) -> None:
    """Copy ``src`` to ``dest`` unless ``dest`` already exists (don't clobber the
    coder's output). Records the worktree-relative path on success."""
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    written.append(str(dest.relative_to(root)))


def scaffold_deploy(deployment: Mapping[str, object] | None, dest: Path) -> list[str]:
    """Write deploy artifacts for a PaaS ``deployment`` block into ``dest``.

    Materialises ``infra/main.tf``, ``.github/workflows/deploy.yml`` and
    ``.tfactory.yml`` from the vendored template for the block's ``deploy_system``.
    Returns the sorted worktree-relative paths actually written (empty when the
    block names no PaaS target or every file already exists).
    """
    system = deployment.get("deploy_system") if deployment else None
    cloud = _PAAS_DIRS.get(system) if isinstance(system, str) else None
    if cloud is None:
        return []

    src = _TEMPLATES / cloud
    written: list[str] = []
    _place(src / "main.tf", dest / "infra" / "main.tf", written, dest)
    _place(
        src / "deploy.yml",
        dest / ".github" / "workflows" / "deploy.yml",
        written,
        dest,
    )
    _place(_TEMPLATES / "tfactory.yml", dest / ".tfactory.yml", written, dest)
    return sorted(written)
