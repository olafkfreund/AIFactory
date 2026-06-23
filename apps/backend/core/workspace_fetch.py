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
