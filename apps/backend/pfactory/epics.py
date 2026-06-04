"""Enumerate a PFactory epic's child issues (epic #327, #338).

PFactory epics list their executable children as a GitHub task-list in the
issue body, e.g.::

    ## Implementation (child issues)

    - [ ] #328 — Create the taxonomy labels
    - [x] #329 — Ingest governed specs

This module extracts those child issue numbers so the importer can pull them in
when an epic is imported. Pure + tolerant — non-string / malformed bodies yield
an empty list, never raise.
"""

from __future__ import annotations

import re

__all__ = ["extract_child_issue_numbers"]

# Markdown task-list items referencing an issue: "- [ ] #329 …" / "* [x] #329".
_CHILD_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*#(\d+)\b", re.MULTILINE)


def extract_child_issue_numbers(body: object) -> list[int]:
    """Return the child issue numbers referenced in an epic body, in order.

    Only task-list checklist items (``- [ ] #NNN``) count — plain ``#NNN``
    mentions elsewhere in the body are ignored, so cross-references don't get
    mistaken for children. Duplicates are collapsed, original order preserved.
    """
    if not isinstance(body, str):
        return []
    out: list[int] = []
    seen: set[int] = set()
    for match in _CHILD_RE.finditer(body):
        num = int(match.group(1))
        if num not in seen:
            seen.add(num)
            out.append(num)
    return out
