"""RFC-0017 Stage E (#190) consumer — materialize ``/work`` from object storage.

The build_backend producer (``apps/web-server``) packs the populated build
worktree to object storage and sets the Job's ``WORKSPACE_URI`` env when
``AIFACTORY_PACK_WORKSPACE`` is on. On the Job side, run.py must reconstitute
``/work`` from that archive BEFORE it resolves the spec and builds — replacing the
single-node RWO co-mount that pins a Job to one node (the multi-node scale-out
path, ``apis/concurrency-conventions.md`` §2.3).

Inert by default: ``WORKSPACE_URI`` is only set on a packed-workspace Job, so on
the co-mount path (and every non-Job invocation) ``maybe_unpack_workspace`` is a
no-op and today's behaviour is unchanged.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

_log = logging.getLogger(__name__)

WORKSPACE_URI_ENV = "WORKSPACE_URI"


def _mark_git_safe_directory(dest: Path) -> None:
    """Tell git the unpacked repo is trusted (RFC-0017 #190).

    The packed workspace is unpacked into ``/work``, whose emptyDir mount root is
    created ``root``-owned by the kubelet while the build runs as nonroot. git
    then refuses every repo op with ``detected dubious ownership in repository at
    '/work'`` (caught in live #190 validation, downstream of a *successful*
    unpack). The single-node co-mount path never hit this — its PVC subPath was
    created by the nonroot control plane, so ownership already matched. Marking
    the dir safe is correct for a single-shot, single-tenant build container.

    Best-effort: a failure here is logged, not raised — the unpack already
    succeeded, and any genuine git problem still surfaces in the build itself.
    """
    # S603/S607 suppressed below: fixed `git` argv (no shell, no untrusted input);
    # `git` by name matches every other git call site in core/ (worktree.py) and
    # the build image puts it on PATH.
    for path in {str(dest), str(dest.resolve())}:
        try:
            subprocess.run(  # noqa: S603
                ["git", "config", "--global", "--add", "safe.directory", path],  # noqa: S607
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            _log.warning("[workspace_fetch] could not mark %s git-safe: %s", path, exc)


def maybe_unpack_workspace(project_dir: str | os.PathLike[str]) -> bool:
    """Materialize ``project_dir`` (``/work``) from the packed ``WORKSPACE_URI``.

    Returns ``True`` when an unpack happened, ``False`` (no-op) when
    ``WORKSPACE_URI`` is absent — today's single-node co-mount path is unchanged.

    Fail-LOUD: unlike the producer (which can fall back to the co-mount), once a
    Job is on the URI path it has no other source for ``/work``, so a fetch /
    extract error propagates rather than letting the build run on an empty tree.
    The extract itself is path-traversal-guarded by ``unpack_workspace`` —
    a poisoned archive is refused before anything is written.
    """
    uri = os.environ.get(WORKSPACE_URI_ENV, "").strip()
    if not uri:
        return False
    # Deferred import — keep boto3 off the import path of every non-Job run; core.*
    # is on sys.path at startup (the cli entrypoint adds the backend dir).
    from core.artifact_store import ArtifactStore, unpack_workspace  # noqa: PLC0415

    dest = Path(project_dir)
    _log.info(
        "[workspace_fetch] unpacking packed workspace %s -> %s (RFC-0017 #190)",
        uri,
        dest,
    )
    unpack_workspace(ArtifactStore(), uri, dest)
    # The unpacked repo lives on a root-owned emptyDir mount but the build runs
    # nonroot → git "dubious ownership". Mark it trusted before run.py touches git.
    _mark_git_safe_directory(dest)
    return True


def maybe_push_workspace_branch(
    project_dir: str | os.PathLike[str], spec_id: str
) -> bool:
    """Push the built worktree branch to origin on the packed path (RFC-0017 #190).

    Symmetric to :func:`maybe_unpack_workspace`. On the packed (multi-node) path the
    build worktree lives on the Job's ephemeral ``/work`` emptyDir, which is GONE
    once the Job exits — so the control-plane handoff/PR-endgame push
    (``tfactory_client._git_info_and_push`` / ``create-pr``), which reads the
    control-plane data-PVC worktree, finds nothing and degrades to ``main`` (the
    produced code is lost). Pushing the branch HERE, from inside the Job where
    ``/work`` still holds the self-contained clone (origin + ``GITHUB_TOKEN``),
    persists the build output to GitHub before the Job dies.

    No-op (returns ``False``) when ``WORKSPACE_URI`` is absent — the co-mount path
    keeps ``/work`` on the data PVC, so the existing control-plane push is unchanged.
    Best-effort: never raises (a push failure must not fail an otherwise-green build).
    """
    if not os.environ.get(WORKSPACE_URI_ENV, "").strip():
        return False
    wt = Path(project_dir) / ".aifactory" / "worktrees" / "tasks" / spec_id
    if not wt.is_dir():
        _log.warning("[workspace_fetch] no worktree at %s; nothing to push", wt)
        return False

    def _git(*args: str) -> str:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout.strip()

    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        url = _git("remote", "get-url", "origin")
        if not branch or not url:
            _log.warning("[workspace_fetch] worktree has no branch/origin; skip push")
            return False
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        push_url = url
        if token and url.startswith("https://github.com/"):
            push_url = url.replace("https://", f"https://x-access-token:{token}@", 1)
        res = subprocess.run(  # noqa: S603
            ["git", "push", push_url, f"HEAD:{branch}"],  # noqa: S607
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=180,
        )
        if res.returncode != 0:
            _log.warning("[workspace_fetch] branch push failed: %s", res.stderr[:200])
            return False
        _log.info(
            "[workspace_fetch] pushed build branch %s to origin (packed path)", branch
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:  # never break a green build
        _log.warning("[workspace_fetch] branch push errored: %s", exc)
        return False
