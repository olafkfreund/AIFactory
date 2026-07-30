"""Factory#460: a refusal must not be dressed as HTTP 200.

``merge_worktree`` and ``create_pr_from_task`` answer with a
``{"success": bool, "error": str}`` envelope and used to return every refusal --
GitHub declining the merge, a missing worktree, an unparseable task id -- inside
an HTTP **200**. The cockpit judged the call on the status line, told the
reviewer "Done." for a merge GitHub had refused, and wrote ``ok=true`` into its
audit trail. The MCP ``merge_pr`` tool has the same shape: its HTTP client only
raises on a non-2xx, so it reported ``{"merged": true}`` for the same refusal.

The status line is now honest. The JSON body is unchanged, so consumers that
already read ``success`` are unaffected.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.responses import JSONResponse
from server.routes import pr, worktree_merge
from server.services.http_verdict import REFUSED_STATUS, honest_status


def _body(response: JSONResponse) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(bytes(response.body))
    return parsed


def _empty_projects_file(tmp_path: Path) -> Path:
    projects_file = tmp_path / "projects.json"
    projects_file.write_text(json.dumps({}))
    return projects_file


# --- the decorator itself -------------------------------------------------
#
# Applied as a plain call rather than with `@` syntax: the code-quality
# ratchet measures each file with `--follow-imports=silent` from the repo
# root, where `server.*` does not resolve, so decorator syntax reports a
# spurious "untyped decorator" against the test. Same wrapper, same coverage.


async def _returning(body: dict[str, Any]) -> dict[str, Any]:
    return body


def _wrapped(body: dict[str, Any]) -> object:
    return asyncio.run(honest_status(_returning)(body))


def test_success_false_becomes_a_refusal_status() -> None:
    refusal = {"success": False, "error": "GitHub refused it"}
    result = _wrapped(refusal)
    assert isinstance(result, JSONResponse)
    assert result.status_code == REFUSED_STATUS
    # Body preserved byte for byte: existing consumers keep working.
    assert _body(result) == refusal


def test_success_true_is_left_alone() -> None:
    ok = {"success": True, "data": {"merged": True}}
    assert _wrapped(ok) == ok  # plain dict, so still a 200


def test_a_body_without_a_success_key_is_left_alone() -> None:
    assert _wrapped({"exists": False}) == {"exists": False}


def test_signature_is_preserved_for_fastapi_dependency_injection() -> None:
    # FastAPI reads the signature to resolve Depends(require_task_access(...)).
    # A wrapper that hid it behind (*args, **kwargs) would silently drop the
    # authorization dependency.
    async def handler(
        task_id: str, access: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {"success": True, "task_id": task_id, "access": access}

    wrapped = honest_status(handler)
    assert list(inspect.signature(wrapped).parameters) == ["task_id", "access"]


# --- the real handlers ----------------------------------------------------


def test_merge_worktree_refusal_is_not_a_200(tmp_path: Path) -> None:
    # An unknown project: one of the early returns the ticket names.
    projects_file = _empty_projects_file(tmp_path)
    with patch.object(worktree_merge, "get_projects_file", return_value=projects_file):
        result = asyncio.run(worktree_merge.merge_worktree("nope:spec", _access={}))

    assert isinstance(result, JSONResponse)
    assert result.status_code == REFUSED_STATUS
    assert _body(result)["success"] is False


def test_create_pr_refusal_is_not_a_200(tmp_path: Path) -> None:
    projects_file = _empty_projects_file(tmp_path)
    with patch.object(pr, "get_projects_file", return_value=projects_file):
        result = asyncio.run(pr.create_pr_from_task("nope:spec", _access={}))

    assert isinstance(result, JSONResponse)
    assert result.status_code == REFUSED_STATUS
    assert _body(result)["success"] is False
