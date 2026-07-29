"""Work out which branch holds a task's work (#1073).

The approve path (create-pr, then merge) used to read the worktree's current
HEAD and treat that as the task branch. That holds only for the in-pod
subprocess build backend, which builds *in* the worktree.

Under ``AIFACTORY_BUILD_BACKEND=kubejob`` -- the deployed configuration -- the
build runs in a separate Job pod on its own emptyDir and the code escapes by
``git push``. The control plane's worktree is created at the base branch and
never switched, so reading its HEAD yields ``main``: create-pr then asked
GitHub to open ``main -> main`` and the button could not work at all.

The branch is not hardcoded here. It is DISCOVERED, by looking for a ref whose
final path segment is the spec id, so this keeps working if the ``aifactory/``
prefix ever changes (``core.worktree.get_branch_name`` owns that convention).

## Refusing beats guessing

Every failure returns ``None`` plus a reason. Returning the base branch is
never acceptable -- silently doing that is the original bug, and merging the
base branch into itself is the *harmless* version. The dangerous version is
picking the wrong task's branch, so an ambiguous match refuses too.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _git(args: list[str], cwd: Path) -> list[str]:
    """Run git, returning stdout lines. A failed git call yields no lines."""
    try:
        # S603/S607: the executable is the literal "git" and every element of
        # *args is built in this module from fixed strings -- no shell, and no
        # caller-supplied argv. spec_id reaches git only as a comparison value
        # in _matches, never as an argument.
        out = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("git %s failed in %s: %s", args[0], cwd, exc)
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _matches(refs: list[str], spec_id: str) -> list[str]:
    """Refs whose last path segment is exactly *spec_id*.

    Suffix matching, not substring: ``aifactory/001-add-thing`` must not match
    a lookup for ``thing``, and ``001-add-thing-extra`` must not match
    ``001-add-thing``.
    """
    return [r for r in refs if r.rsplit("/", 1)[-1] == spec_id]


def resolve_task_branch(
    *,
    worktree_path: Path,
    project_path: Path,
    spec_id: str,
    base_branch: str,
) -> tuple[str | None, str | None]:
    """Return ``(branch, None)`` or ``(None, reason)``.

    Order matters. The worktree's own HEAD is checked first because when the
    build happened *in* the worktree it is the most direct evidence; the
    discovered branch is the fallback for builds that happened elsewhere.
    """
    # 1. The worktree's HEAD -- but only if it is a real branch that is not the
    #    base. `main` here means "this worktree was never switched", which is
    #    the kubejob case, not a task branch.
    if worktree_path.is_dir():
        head = _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path)
        if head:
            branch = head[0]
            if branch not in {"HEAD", base_branch}:
                return branch, None

    # 2. A local branch for this spec.
    local = _matches(
        _git(["for-each-ref", "--format=%(refname:short)", "refs/heads"], project_path),
        spec_id,
    )
    if len(local) == 1:
        return local[0], None
    if len(local) > 1:
        return (
            None,
            f"ambiguous: {len(local)} local branches match {spec_id!r}: {local}",
        )

    # 3. A remote branch -- the kubejob build pushes there and may not leave a
    #    local ref behind at all.
    remote = _matches(
        _git(
            ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"],
            project_path,
        ),
        spec_id,
    )
    if len(remote) == 1:
        # Strip the remote name: callers push/compare by branch, not by ref.
        return remote[0].split("/", 1)[1], None
    if len(remote) > 1:
        return None, f"ambiguous: {len(remote)} remote branches match {spec_id!r}"

    return None, (
        f"no branch found for {spec_id!r}: the worktree is on {base_branch!r} and "
        f"no local or origin branch ends with that spec id. The build may not "
        f"have pushed."
    )
