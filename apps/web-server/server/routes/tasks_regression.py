"""POST /api/tasks/{task_id}/post-merge-regression (#2054).

Called when a post-merge regression run fails on a commit the pipeline merged:
opens a revert PR (never merged here). See services/revert_pr.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from server.project_registry import load_projects, org_id_for_path
from server.routes.project_authz import require_task_access
from server.services import github_app, revert_pr
from server.specpath import safe_spec_component

router = APIRouter()
logger = logging.getLogger(__name__)

_URI = re.compile(r"s3://[A-Za-z0-9._\-/]{1,512}")


class PostMergeRegression(BaseModel):
    merge_sha: str = Field(min_length=7, max_length=40, pattern=r"^[0-9a-fA-F]+$")
    run_url: str = Field(max_length=500, pattern=r"^https://\S+$")


def _transcript_uris(spec_dir: Path) -> list[str]:
    """``transcriptUris`` from the task record; only plain s3:// URIs, as the file
    is writable by project members and the values go into a PR body."""
    try:
        meta = json.loads((spec_dir / "task_metadata.json").read_text())
        raw = meta.get("transcriptUris") or []
    except (OSError, ValueError, AttributeError):
        return []
    return [u for u in raw if isinstance(u, str) and _URI.fullmatch(u)][:20]


@router.post("/{task_id}/post-merge-regression")
async def post_merge_regression(
    task_id: str,
    body: PostMergeRegression,
    _access: Annotated[dict[str, Any], Depends(require_task_access("member"))],
) -> dict[str, Any]:
    """Open a revert PR for ``body.merge_sha`` if the pipeline merged it."""
    project_id, _, spec_id = task_id.partition(":")
    try:
        spec_id = safe_spec_component(spec_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid task ID format"
        ) from None
    project = load_projects().get(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        )
    project_path = Path(project["path"])
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    org = org_id_for_path(project_path)
    await github_app.warm_org_token(org)

    def _run() -> dict[str, Any]:
        with github_app.org_github_token(org, pinned=True):
            return revert_pr.open_revert_pr(
                project_id=project_id,
                spec_id=spec_id,
                failing_sha=body.merge_sha,
                run_url=body.run_url,
                transcript_uris=_transcript_uris(spec_dir),
            )

    result = await asyncio.to_thread(_run)
    if not result["ok"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=result["reason"]
        )
    return result
