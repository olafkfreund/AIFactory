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

from server.specpath import safe_spec_component

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
        # args[0] is a literal subcommand; cwd is caller-derived and left out.
        logger.warning("git %s failed: %s", args[0], exc)
        return []
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def _matches(refs: list[str], spec_id: str) -> list[str]:
    """Refs whose last path segment is exactly *spec_id*.

    Suffix matching, not substring: ``aifactory/001-add-thing`` must not match
    a lookup for ``thing``, and ``001-add-thing-extra`` must not match
    ``001-add-thing``.
    """
    return [r for r in refs if r.rsplit("/", 1)[-1] == spec_id]


_MARKER = ".task_branch"


def _marker_path(project_path: Path, spec_id: str) -> Path:
    """The marker path, built from the trusted project root.

    Takes (project_path, spec_id) rather than a ready-made spec_dir on purpose:
    a caller-supplied PATH cannot be sanitised -- safe_spec_component barriers a
    single COMPONENT, so a tainted parent survives it. Building from the root
    with the barriered component is the shape the rest of this codebase uses
    (#565) and the only one that actually cuts the flow.
    """
    return (
        project_path
        / ".aifactory"
        / "specs"
        / safe_spec_component(spec_id)
        / _MARKER
    )


def record_branch(project_path: Path, spec_id: str, branch: str) -> None:
    """Record the branch a build will push. Raises OSError; callers decide."""
    _marker_path(project_path, spec_id).write_text(branch + "\n")


def recorded_branch(project_path: Path, spec_id: str) -> str | None:
    """The branch the build recorded at dispatch, if it left one.

    Written by the kubejob workspace preparation, which is the only place the
    control plane knows the branch -- /work is deliberately left on the base
    branch, so nothing downstream can read it off a directory.
    """
    marker = _marker_path(project_path, spec_id)
    if not marker.is_file():
        return None
    try:
        return marker.read_text().strip() or None
    except OSError:
        return None


def _exists(branch: str, project_path: Path) -> bool:
    """Is *branch* a real ref, locally or on origin?"""
    refs = _git(
        [
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads",
            "refs/remotes/origin",
        ],
        project_path,
    )
    return branch in refs or f"origin/{branch}" in refs


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
    # 0. What the build recorded at dispatch -- data beats archaeology.
    #
    #    VALIDATED, not trusted: a recorded branch that no longer exists (deleted
    #    after a previous merge, or a spec dir reused) must fall through to
    #    discovery rather than being handed to git as a merge source. Returning a
    #    branch because a file says so is how you merge the wrong thing.
    recorded = recorded_branch(project_path, spec_id)
    if recorded and recorded != base_branch and _exists(recorded, project_path):
        return recorded, None

    # 1. The worktree's HEAD -- but only if it is a real branch that is not the
    #    base. `main` here means "this worktree was never switched", which is
    #    the kubejob case, not a task branch.
    if worktree_path.is_dir():
        head = _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path)
        if head:
            branch = head[0]
            if branch not in {"HEAD", base_branch}:
                return branch, None

    return _discover(project_path, spec_id, base_branch, worktree_path)


def _discover(
    project_path: Path, spec_id: str, base_branch: str, worktree_path: Path | None = None
) -> tuple[str | None, str | None]:
    """Find the branch from git refs when nothing else identified it.

    Searches the worktree FIRST. Under the kubejob backend the task directory
    is a standalone clone (`git clone --local --no-hardlinks`, see
    build_backend), not a `git worktree add` -- so it has its OWN refs, and the
    branch the build pushed exists there and NOT in the project repo. Searching
    only the project found nothing and refused a task whose branch was sitting
    one directory away.
    """
    roots = [r for r in (worktree_path, project_path) if r is not None and r.is_dir()]
    for root in roots:
        for scope, refspec in (("local", "refs/heads"), ("origin", "refs/remotes/origin")):
            found = _matches(
                _git(["for-each-ref", "--format=%(refname:short)", refspec], root),
                spec_id,
            )
            if len(found) > 1:
                return None, (
                    f"ambiguous: {len(found)} {scope} branches match {spec_id!r}: {found}"
                )
            if len(found) == 1:
                # Strip the remote name: callers compare by branch, not by ref.
                return (
                    found[0].split("/", 1)[1] if scope == "origin" else found[0]
                ), None

    return None, (
        f"no branch found for {spec_id!r}: the worktree is on {base_branch!r} and "
        f"no local or origin branch ends with that spec id, in either the "
        f"worktree or the project. The build may not have pushed."
    )
