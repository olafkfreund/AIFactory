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

import contextlib
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


def spec_dir_for(project_path: object, spec_id: object) -> Path:
    """The spec directory for *spec_id* under *project_path*, barrier applied.

    One constructor rather than a guard at each join (#1410). The service layer
    open-coded this join 31 times across 13 modules and guarded only two of them
    -- the validation lived in the route handlers, which sanitise `spec_id` by
    reassignment before joining, while the services built the same paths
    straight from a task id. `agent_kubejob` was the clearest case: it split a
    job id on ":" and joined the tail onto a path with nothing in between.

    This module's own docstring already said services "keep their own barrier
    for the paths they build directly". They did not. Adding a barrier to 23
    call sites would leave the 24th unguarded the day someone adds it, so the
    join itself moves here and the barrier is not optional.

    Raises ValueError on an unsafe component -- see `safe_spec_component` for
    why it raises rather than sanitising.
    """
    return Path(project_path) / ".aifactory" / "specs" / safe_spec_component(spec_id)


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
# The ``load_projects`` import is deliberately function-local. It used to be a
# cycle break -- the registry lived in ``routes.projects``, which imports THIS
# module -- and since #1317 it is purely about late binding: resolving the name
# at call time is what lets a test point ``server.project_registry.load_projects``
# at a fixture registry and have these roots follow.


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
    from fastapi import HTTPException, status  # noqa: PLC0415 - see docstring

    try:
        return contained_path(candidate, roots, what, expand_user)
    except ValueError:
        # `from None`, not `from err`: the ValueError carries the rejected path,
        # and chaining it would put that path into the traceback a client can
        # see. The 403 wording is deliberately identical for every rejection so
        # the response cannot be used to probe which paths exist.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: path is outside the {what}",
        ) from None


def registered_project_roots() -> list[Path]:
    """Resolved paths of every registered project.

    The strict tier. Used where the request operates on a project that already
    exists -- reading file content, launching a terminal, opening a worktree.
    """
    # Function-local so the loader is resolved at CALL time, which is what lets
    # a test point `server.project_registry.load_projects` at a fixture registry.
    # No longer a cycle (the registry moved below this module in #1317) -- the
    # deferral is now purely about that late binding.
    from server.project_registry import load_projects  # noqa: PLC0415 - see above

    roots: list[Path] = []
    for p in load_projects().values():
        # A malformed registry entry drops out of the root set rather than
        # taking the request down; a path that cannot resolve is not a root.
        with contextlib.suppress(OSError, KeyError, TypeError):
            roots.append(Path(p["path"]).resolve())
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
    # No resolvable $HOME (a container with no passwd entry) simply means one
    # fewer root, never an error to the caller.
    with contextlib.suppress(OSError, RuntimeError):
        roots.append(Path.home().resolve())
    for raw_extra in os.environ.get("APP_FILE_BROWSE_ROOTS", "").split(os.pathsep):
        extra = raw_extra.strip()
        if extra:
            # An operator-configured root that does not resolve is skipped, not
            # fatal: a typo in the env var must not stop the server starting.
            with contextlib.suppress(OSError):
                roots.append(Path(extra).resolve())
    return roots
