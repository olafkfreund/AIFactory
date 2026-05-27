"""Portal-managed project workspaces (#82 PR-A).

When AIFactory runs on a developer laptop, the user's git repo lives on
the same filesystem as the portal and the existing ``POST /api/projects
{path}`` route just registers that directory. That model breaks for
every other deployment shape:

- **Single-user VPS** — repo is on the laptop, portal on the VPS, no
  shared filesystem.
- **Kubernetes** — portal pod has no view into the user's machine.
- **Shared/SaaS** — the path concept doesn't even map.

This service backs the alternative path: the portal accepts a Git URL
and clones it into a local workspace directory. The workspace root is
configurable via ``PROJECT_WORKSPACE_ROOT`` (defaults to
``~/.aifactory/workspaces/`` on laptop installs, expected to be a
mounted PVC in K8s installs). The returned path is what the rest of
AIFactory (Auto-Fix, agent_service, etc.) uses as the project's
on-disk root — they don't need to know whether the project was added
via path or URL.

Auth in PR-A is whatever the host's git config already provides —
i.e. public HTTPS URLs and SSH keys configured in ``~/.ssh/``. Stored
git credentials (Deploy Keys, GitHub App install IDs, PATs) land in
PR-C.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_ROOT = Path.home() / ".aifactory" / "workspaces"

# Default git operation timeout — long enough for a fresh clone of a
# medium-sized repo over a slow link, short enough that a hung remote
# doesn't lock up the portal forever.
DEFAULT_GIT_TIMEOUT_SECONDS = 600


def workspace_root() -> Path:
    """Resolve the directory under which all portal-managed clones live.

    Looks at ``PROJECT_WORKSPACE_ROOT`` env first (the K8s/SaaS path),
    falls back to ``~/.aifactory/workspaces/`` (laptop path).
    """
    env = os.environ.get("PROJECT_WORKSPACE_ROOT")
    if env:
        return Path(env).expanduser()
    return DEFAULT_WORKSPACE_ROOT


def slug_from_git_url(git_url: str) -> str:
    """Turn a git URL into a filesystem-safe directory slug.

    ``git@github.com:olaf/AIFactory.git`` → ``olaf-AIFactory``
    ``https://github.com/olaf/AIFactory.git`` → ``olaf-AIFactory``
    ``https://gitlab.com/group/sub/repo`` → ``group-sub-repo``

    The slug is used as the directory name under ``workspace_root()``.
    """
    # SCP-style ("git@host:owner/repo.git") — split on the colon, drop the host.
    if git_url.startswith("git@") and ":" in git_url:
        _, path = git_url.split(":", 1)
    else:
        parsed = urlparse(git_url)
        path = parsed.path.lstrip("/")
    # Strip .git suffix + lowercase + replace path separators with hyphens.
    if path.endswith(".git"):
        path = path[:-4]
    # Replace any non-alnum/hyphen char with hyphen; collapse repeats.
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", path).strip("-")
    return slug or "workspace"


async def clone_or_update(
    git_url: str,
    branch: str | None = None,
    slug: str | None = None,
    *,
    root: Path | None = None,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> Path:
    """Clone the repo into the workspace root, or fast-forward an existing clone.

    Args:
        git_url: HTTPS or SSH URL to the repository.
        branch: Optional branch to checkout after clone. ``None`` uses
            the remote's HEAD.
        slug: Optional override for the workspace directory name.
            Defaults to ``slug_from_git_url(git_url)``.
        root: Optional override for the workspace root. Defaults to
            ``workspace_root()`` (PROJECT_WORKSPACE_ROOT env or
            ``~/.aifactory/workspaces/``).
        timeout_seconds: Per-operation timeout.

    Returns:
        Absolute path to the local clone.

    Raises:
        GitOperationError: On any non-zero ``git`` exit code or timeout.
    """
    workspace = (root or workspace_root()) / (slug or slug_from_git_url(git_url))
    workspace.parent.mkdir(parents=True, exist_ok=True)

    if (workspace / ".git").is_dir():
        # Existing clone — fetch + reset/fast-forward
        await _run_git(
            ["fetch", "--prune", "origin"],
            cwd=workspace,
            timeout=timeout_seconds,
        )
        if branch:
            await _run_git(
                ["checkout", branch],
                cwd=workspace,
                timeout=timeout_seconds,
            )
        await _run_git(
            ["pull", "--ff-only"],
            cwd=workspace,
            timeout=timeout_seconds,
        )
        logger.info("[workspace] pulled latest into %s", workspace)
        return workspace

    # Fresh clone
    cmd = ["clone"]
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([git_url, str(workspace)])
    await _run_git(cmd, cwd=workspace.parent, timeout=timeout_seconds)
    logger.info("[workspace] cloned %s → %s", git_url, workspace)
    return workspace


class GitOperationError(RuntimeError):
    """Raised when a git operation fails or times out."""


async def _run_git(args: list[str], *, cwd: Path, timeout: float) -> str:
    """Run ``git <args>`` with a timeout. Returns stdout on success."""
    cmd = ["git", *args]
    logger.debug("[workspace] running: git %s (cwd=%s)", " ".join(args), cwd)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise GitOperationError(f"git executable not found on PATH: {e}") from e

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError as e:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise GitOperationError(
            f"git {' '.join(args)} timed out after {timeout}s"
        ) from e

    if proc.returncode != 0:
        raise GitOperationError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): "
            f"{stderr.decode('utf-8', 'replace').strip() or 'no stderr'}"
        )
    return stdout.decode("utf-8", "replace")
