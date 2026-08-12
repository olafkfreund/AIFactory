"""A failed initialise must not answer HTTP 200 (#1126).

`initialize_project` ends in `except Exception: return {"success": False, ...}`,
and that body used to leave as a **200**. A client judging the call on its
status line — which is the normal thing to do, and what the cockpit does — reads
the refusal as a success. That is the Factory#460 defect class: a merge GitHub
refused was reported to the reviewer as "Done." and written into the audit trail
as `ok=true`.

`projects.py` had exactly one such return site; every other failure path in the
module raises `HTTPException` already. So this file closes the module.

Both directions are asserted. The success path must stay a plain dict at 200 —
`honest_status` keys on the body, so a decorator that wrapped everything would
be a different bug, and only checking the failure would not notice.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.responses import JSONResponse
from server.routes import projects
from server.services.http_verdict import REFUSED_STATUS


def _projects_file(tmp_path: Path, project_path: Path) -> Path:
    f = tmp_path / "projects.json"
    f.write_text(json.dumps({"p1": {"path": str(project_path), "name": "p1"}}))
    return f


def _run(projects_file: Path) -> Any:
    with patch.object(projects, "get_projects_file", return_value=projects_file):
        return asyncio.run(projects.initialize_project("p1", _access={}))


def test_a_failed_initialise_answers_409_not_200(tmp_path: Path) -> None:
    # A FILE where `.aifactory/specs` has to be created: mkdir raises
    # NotADirectoryError, which is the handler's `except` path — a real failure
    # rather than a mocked one.
    project_path = tmp_path / "not-a-dir"
    project_path.write_text("i am a file\n")

    result = _run(_projects_file(tmp_path, project_path))

    assert isinstance(result, JSONResponse), (
        f"a failed initialise still left as a plain dict (HTTP 200); got {result!r}"
    )
    assert result.status_code == REFUSED_STATUS, result.status_code
    body = json.loads(bytes(result.body))
    # The body must be untouched, or consumers that already read `success` break.
    assert body["success"] is False, body
    assert body["error"], "the reason must survive the translation"


def test_a_successful_initialise_is_untouched(tmp_path: Path) -> None:
    """The decorator keys on the BODY, so success must stay a 200 dict."""
    project_path = tmp_path / "real-project"
    project_path.mkdir()

    result = _run(_projects_file(tmp_path, project_path))

    assert not isinstance(result, JSONResponse), (
        f"the success path was translated too — that is a different bug; got {result!r}"
    )
    assert result["success"] is True, result
    assert (project_path / ".aifactory" / "specs").is_dir()
