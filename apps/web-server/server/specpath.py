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
