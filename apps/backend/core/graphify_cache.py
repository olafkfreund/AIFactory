"""Best-effort MinIO cache for the graphify code graph (#804).

The graphify build (Tree-sitter, token-free) is rebuilt per task and lost with
the per-task k8s Job (RFC-0016). This module caches ``graph.json`` in the same
S3/MinIO seam the packed-workspace path already uses (``core.artifact_store``),
keyed by ``graphify/{repo_slug}/{head_commit_sha}/graph.json`` so a second task
on the same repo+commit reuses the graph instead of rebuilding it.

Exact-hit only in v1 — no nearest-ancestor lookup (v2 if needed). Every storage
or git failure is swallowed: a cache problem must never fail a build.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from pathlib import Path

_log = logging.getLogger(__name__)


def _git(project_dir: Path, *args: str) -> str | None:
    """Run a git query in the task worktree; None on any failure."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        res = subprocess.run(  # noqa: S603
            ["git", "-C", str(project_dir), *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout.strip() or None
    return None


def _repo_slug(project_dir: Path) -> str:
    """``owner/repo`` from the origin remote, else the directory name.

    Handles both ``git@host:owner/repo.git`` and ``https://host/owner/repo.git``.
    """
    url = _git(project_dir, "config", "--get", "remote.origin.url")
    if url:
        parts = [p for p in url.removesuffix(".git").replace(":", "/").split("/") if p]
        with contextlib.suppress(ValueError):  # fewer than 2 path segments
            *_, owner, name = parts
            return f"{owner}/{name}"
    return project_dir.name


def cache_key(project_dir: Path) -> str | None:
    """The exact-hit cache key, or None when HEAD cannot be resolved.

    HEAD is resolved from the TASK WORKTREE (not the default branch), so the
    cached graph matches the exact code the coder sees.
    """
    head = _git(project_dir, "rev-parse", "HEAD")
    if not head:
        return None
    return f"graphify/{_repo_slug(project_dir)}/{head}/graph.json"


def fetch_cached_graph(project_dir: Path, graph_json: Path) -> bool:
    """Exact-hit fetch of a cached graph into ``graph_json``; True on hit.

    All storage errors (store unreachable, key missing, unwritable dest) are
    swallowed — a miss just means the caller builds as usual.
    """
    key = cache_key(project_dir)
    if key is None:
        return False
    with contextlib.suppress(Exception):
        from core.artifact_store import ArtifactStore  # noqa: PLC0415

        data = ArtifactStore().get_bytes(key)
        graph_json.parent.mkdir(parents=True, exist_ok=True)
        graph_json.write_bytes(data)
        _log.info("[graphify_cache] cache hit %s — skipping graph build", key)
        return True
    return False


def ensure_graph(project_dir: Path, graph_json: Path) -> None:
    """Fetch-or-build the code graph at ``graph_json`` (the pre-coder step).

    Cache first (exact repo+commit hit skips the token-free Tree-sitter build);
    on miss, build as before then upload best-effort. Every failure path is
    swallowed — worst case the coder simply runs without the graph tool.
    """
    if graph_json.exists() or fetch_cached_graph(project_dir, graph_json):
        return
    # best-effort token-free build; graphify is our own CLI on PATH in the
    # coder image. Degrade to no graph tool on any failure.
    with contextlib.suppress(FileNotFoundError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603
            ["graphify", "update", str(project_dir), "--no-cluster"],  # noqa: S607
            timeout=180,
            capture_output=True,
            check=False,
        )
    store_cached_graph(project_dir, graph_json)


def store_cached_graph(project_dir: Path, graph_json: Path) -> bool:
    """Best-effort upload of a freshly built graph; True when stored.

    No-op when the build produced no graph. Never raises — a cache failure must
    never fail a build.
    """
    if not graph_json.is_file():
        return False
    key = cache_key(project_dir)
    if key is None:
        return False
    with contextlib.suppress(Exception):
        from core.artifact_store import ArtifactStore  # noqa: PLC0415

        ArtifactStore().put_bytes(key, graph_json.read_bytes(), "application/json")
        _log.info("[graphify_cache] stored graph at %s", key)
        return True
    return False
