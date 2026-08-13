"""Validation for request-supplied path components (#1056).

Deliberately import-side-effect free, unlike ``server.paths``, which runs
``migrate_legacy_data()`` at module load. A validator that copies files when
you import it cannot be used from a route module, which is exactly where it
is needed most.

Lives at server level rather than under ``routes/`` or ``services/`` because
both layers need it: routes must reject a bad component before building a
path, and services keep their own barrier for the paths they build directly.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

# Deliberately a `fullmatch` against a restrictive allow-list rather than an
# ad-hoc `if "/" in value` check. Two reasons, and the second is not cosmetic:
# an allow-list cannot be defeated by an encoding trick the deny-list author
# did not think of, and CodeQL recognises a `fullmatch` against a restrictive
# pattern as a sanitizer while it does not recognise ad-hoc containment checks.
# The first version of this barrier elsewhere in the fleet hardened the code
# without clearing the alert, for exactly that reason.
# Same shape as PFactory's safe_spec_component (PFactory#335).
_SPEC_COMPONENT_RE = re.compile(r"[A-Za-z0-9._-]{1,255}")

# Rejected even though the character class permits them: "." and ".." are the
# traversal primitives themselves.
_RESERVED_COMPONENTS = frozenset({".", ".."})


def safe_spec_component(value: object, field: str = "spec_id") -> str:
    """Return *value* if it is safe to join onto a trusted directory root.

    ``spec_id`` reaches the server from the API and is interpolated into
    filesystem paths. ``Path`` joins collapse traversal SILENTLY --
    ``Path("/srv/specs") / "../../etc"`` is ``/etc`` -- so the component must
    be validated BEFORE it is joined, never after.

    Raises rather than sanitising: a spec id that needed rewriting is a caller
    bug or an attack, and quietly building a different path than the caller
    asked for is how both go unnoticed.
    """
    text = str(value)
    if text in _RESERVED_COMPONENTS or not _SPEC_COMPONENT_RE.fullmatch(text):
        raise ValueError(f"invalid {field}: {text[:80]!r}")
    return text


def contained_path(
    candidate: object, roots: Iterable[object], what: str, expand_user: bool = False
) -> Path:
    """Return *candidate*, resolved, if it lands inside one of *roots*.

    The second half of the traversal story. ``safe_spec_component`` covers the
    case where the request supplies one path COMPONENT and the server owns the
    root; this covers the case where the request supplies a whole PATH and the
    server owns a set of permitted roots (a registered project directory, the
    user's home, ...).

    The resolve happens HERE, on the way in, and the containment test is made
    against the resolved form. That ordering is the whole point: a caller that
    resolves first and checks afterwards has already handed the analyser -- and
    an attacker -- a path it never constrained, and a caller that checks the
    raw string first is defeated by ``..`` and by symlinks.

    Raises ``ValueError`` rather than returning a fallback path. Callers at the
    HTTP boundary map that to 403; quietly substituting a different path is how
    a traversal attempt turns into a confusing bug report instead of a refusal.

    Deliberately NOT satisfied by ``exists()`` / ``is_file()`` / ``is_dir()``:
    those say whether a path is THERE, not whether it is ALLOWED, and a
    containment helper that accepted them would clear real findings.

    ``expand_user`` exists so that callers which legitimately honour ``~`` (the
    add-project flow, which has always accepted ``~/code/thing``) do the
    expansion INSIDE the barrier. A caller writing
    ``contained_path(Path(x).expanduser(), ...)`` builds a path expression out
    of the raw request value one line before confining it -- which is a real
    sink, correctly reported, and no less real for being immediately followed
    by the check. Off by default: the file-browser routes deliberately do not
    expand ``~``, so that ``~/.aws/credentials`` stays a literal outside every
    root rather than resolving into the operator's home.
    """
    raw = Path(str(candidate))
    resolved = (raw.expanduser() if expand_user else raw).resolve()
    for root in roots:
        try:
            root_resolved = Path(str(root)).resolve()
        except (OSError, ValueError):
            continue
        if resolved == root_resolved or root_resolved in resolved.parents:
            return resolved
    raise ValueError(f"path is outside the {what}: {str(candidate)[:120]!r}")


# --------------------------------------------------------------------------
# Root tiers (#1278)
# --------------------------------------------------------------------------
#
# ``contained_path`` answers "is this path inside these roots". These two
# functions answer the other half -- WHICH roots -- and they live here rather
# than in ``routes/files.py`` (where they started, #320) because the answer is
# now needed by every layer that accepts a whole path from a request: the
# add-project flow, the git routes, terminal cwd, worktree launch.
#
# The ``load_projects`` import is deliberately function-local. This module is a
# leaf and must stay one; a module-level import back into ``routes`` would put
# a cycle on the boot path.


def within_roots(
    candidate: object, roots: Iterable[object], what: str, expand_user: bool = False
) -> Path:
    """``contained_path``, but raising HTTP 403 instead of ``ValueError``.

    The single HTTP-boundary wrapper, so that every route confining a
    request-supplied path produces the same status and the same wording. It
    RETURNS the confined path rather than asserting and discarding it: an
    assert-and-continue helper leaves the raw request value live in the caller,
    so neither a reader nor the analyser can tell the checked path from the
    unchecked one.

    ``fastapi`` is imported inside the function to keep this module free of a
    framework dependency at import time -- it is also imported by plain
    services and by tests that never build an app.
    """
    from fastapi import HTTPException, status

    try:
        return contained_path(candidate, roots, what, expand_user)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: path is outside the {what}",
        )


def registered_project_roots() -> list[Path]:
    """Resolved paths of every registered project.

    The strict tier. Used where the request operates on a project that already
    exists -- reading file content, launching a terminal, opening a worktree.
    """
    from server.routes.projects import load_projects

    roots: list[Path] = []
    for p in load_projects().values():
        try:
            roots.append(Path(p["path"]).resolve())
        except (OSError, KeyError, TypeError):
            pass
    return roots


def browse_roots() -> list[Path]:
    """Registered projects + ``$HOME`` + ``APP_FILE_BROWSE_ROOTS``.

    The tier for flows that legitimately point at a directory that is NOT
    registered yet -- browsing for a project to add, scanning for candidates,
    ``git init`` on a brand-new one. Confining those to registered projects
    would make it impossible to add the first project.

    ``$HOME`` is the sane default, not a claim that projects only live there.
    ``APP_FILE_BROWSE_ROOTS`` (an ``os.pathsep`` list) is the operator's escape
    hatch for a deployment that keeps code on another mount, and it is the
    documented answer to "the portal will not let me add /srv/code".

    The trade is explicit: a path outside every root can no longer be
    registered through the API. That was already true through the UI -- the
    directory browser (``routes/files.py``) has listed only these roots since
    #320, so the browser could never offer such a path. This closes the gap
    between what the UI can produce and what the API will accept.
    """
    roots = registered_project_roots()
    try:
        roots.append(Path.home().resolve())
    except (OSError, RuntimeError):
        pass
    for extra in os.environ.get("APP_FILE_BROWSE_ROOTS", "").split(os.pathsep):
        extra = extra.strip()
        if extra:
            try:
                roots.append(Path(extra).resolve())
            except OSError:
                pass
    return roots
