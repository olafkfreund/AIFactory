"""
Execution context for validators (#364)
=======================================

The Bash security hook knows the agent's working directory (the worktree root),
but the per-command validators historically received only the command string.
Path-sensitive validators (``rm`` / ``chmod``) need the worktree root to decide
whether a target escapes the tree.

Rather than thread ``cwd`` through every validator signature, the hook stashes it
in a :class:`contextvars.ContextVar` for the duration of one validation pass; the
validators read it via :func:`get_worktree_root`. Sync validators called from the
async hook run in the same context, so the value is visible.
"""

from __future__ import annotations

import contextvars
import os

_worktree_root: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "aifactory_worktree_root", default=None
)


def set_worktree_root(path: str | None) -> contextvars.Token:
    """Set the active worktree root; returns a token to reset with."""
    return _worktree_root.set(path)


def reset_worktree_root(token: contextvars.Token) -> None:
    _worktree_root.reset(token)


def get_worktree_root() -> str | None:
    return _worktree_root.get()


def target_escapes_worktree(path_str: str, root: str | None) -> bool:
    """True if a filesystem ``path_str`` would resolve outside ``root``.

    Resolves absolute paths and ``..`` segments lexically (``os.path.normpath``,
    not ``Path.resolve`` — we don't want to follow symlinks or hit the disk).
    When ``root`` is unknown, fail closed: treat any absolute path or any path
    containing a ``..`` segment as an escape.
    """
    # Strip a trailing glob so ``build/*`` is judged by its directory.
    cleaned = path_str
    if not cleaned:
        return False

    if root is None:
        if cleaned.startswith(("/", "~")):
            return True
        parts = cleaned.replace("\\", "/").split("/")
        return ".." in parts

    root_norm = os.path.normpath(os.path.abspath(root))
    target = cleaned
    if not os.path.isabs(target):
        target = os.path.join(root_norm, target)
    target_norm = os.path.normpath(target)
    # Containment: target must be the root or live under it.
    try:
        return os.path.commonpath([root_norm, target_norm]) != root_norm
    except ValueError:
        # Different drives / invalid — treat as escape.
        return True


__all__ = [
    "get_worktree_root",
    "reset_worktree_root",
    "set_worktree_root",
    "target_escapes_worktree",
]
