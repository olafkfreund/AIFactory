"""
Identifier validation — validate untrusted IDs/paths independently of the LLM
=============================================================================

Agent/MCP tools and CLI entrypoints take ``spec_name`` / ``task_id`` values that
are LLM-chosen or externally supplied and then build filesystem paths or HTTP
URLs from them. Without an independent check these allow path/URL traversal
(``../``, leading ``/``) regardless of what the model "decided" (#371, #325
audit).

These validators are deliberately strict allowlists — they raise ``ValueError``
on anything that isn't a single, safe path/URL segment. Callers either let the
error surface (can't proceed without a valid id) or degrade gracefully.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

# A spec name is a single directory segment: ``001-add-auth`` etc. No slashes,
# no traversal, no leading dot/dash.
_SPEC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# A task id is ``<project>:<spec>`` or a bare id — letters, digits and a few
# separators, but never ``/`` (would change a URL path) or ``..``.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")

_MAX_LEN = 200


def validate_spec_name(spec_name: str) -> str:
    """Return ``spec_name`` if it is a safe single path segment, else raise.

    Rejects empty/over-long values, anything containing ``/`` or ``\\`` or
    ``..``, and anything outside the allowlisted charset — so it can never
    escape the ``specs/`` directory.
    """
    if not spec_name or not isinstance(spec_name, str):
        raise ValueError("spec_name must be a non-empty string")
    if len(spec_name) > _MAX_LEN:
        raise ValueError("spec_name is too long")
    # Must be a single segment with no traversal.
    if spec_name != PurePosixPath(spec_name).name or ".." in spec_name:
        raise ValueError(f"spec_name {spec_name!r} is not a single safe path segment")
    if not _SPEC_NAME_RE.match(spec_name):
        raise ValueError(f"spec_name {spec_name!r} contains disallowed characters")
    return spec_name


def validate_task_id(task_id: str) -> str:
    """Return ``task_id`` if it is a safe URL-path segment, else raise.

    Rejects ``/``, ``..`` and any character outside the allowlist, so it can't
    traverse or restructure the ``/api/tasks/{task_id}`` URL path.
    """
    if not task_id or not isinstance(task_id, str):
        raise ValueError("task_id must be a non-empty string")
    if len(task_id) > _MAX_LEN:
        raise ValueError("task_id is too long")
    if "/" in task_id or "\\" in task_id or ".." in task_id:
        raise ValueError(f"task_id {task_id!r} must not contain path separators")
    if not _TASK_ID_RE.match(task_id):
        raise ValueError(f"task_id {task_id!r} contains disallowed characters")
    return task_id
