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


def contained_path(candidate: object, roots: Iterable[object], what: str) -> Path:
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
    """
    resolved = Path(str(candidate)).resolve()
    for root in roots:
        try:
            root_resolved = Path(str(root)).resolve()
        except (OSError, ValueError):
            continue
        if resolved == root_resolved or root_resolved in resolved.parents:
            return resolved
    raise ValueError(f"path is outside the {what}: {str(candidate)[:120]!r}")
