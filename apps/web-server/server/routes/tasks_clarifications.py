"""Task clarification endpoints — extracted from routes/tasks.py (#556 split).

Clarification question generation + answer submission, carved out of
routes/tasks.py; tasks.py re-mounts this sub-router via router.include_router so
the public /api/tasks paths are unchanged. Models come from routes/task_models
(no cycle); the _resolve_task/spec_to_task helpers are imported lazily inside
the handlers to avoid the import cycle (tasks.py mounts this at its tail).

    POST /api/tasks/{task_id}/clarifications
    POST /api/tasks/{task_id}/clarifications/answers
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends

from .project_authz import require_task_access
from .task_models import (
    ClarificationAnswersRequest,
    ClarificationQuestion,
    ClarificationResponse,
    Task,
)

# These helpers are OWNED by task_service; tasks.py only re-exports them. Going
# to the owner keeps this sub-router below tasks.py in the import graph, so the
# imports no longer have to be deferred into the request handlers (#1302).
from .task_service import _resolve_task, spec_to_task

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/{task_id}/clarifications", response_model=ClarificationResponse)
async def generate_clarifications(
    task_id: str, _access: dict = Depends(require_task_access("member"))
):
    """Generate clarification questions for a task using an LLM."""
    from ..services.clarification_service import generate_clarification_questions

    project_id, spec_id, project_path, spec_dir = _resolve_task(task_id)

    # Load task title and description from requirements.json
    req_file = spec_dir / "requirements.json"
    if not req_file.exists():
        return ClarificationResponse(skip=True, skipReason="No requirements found.")

    requirements = json.loads(req_file.read_text())
    title = requirements.get("title", "")
    description = requirements.get("description", "")

    result = await generate_clarification_questions(title, description, project_path)

    return ClarificationResponse(
        questions=[ClarificationQuestion(**q) for q in result.get("questions", [])],
        skip=result.get("skip", False),
        skipReason=result.get("skipReason", ""),
    )


@router.post("/{task_id}/clarifications/answers", response_model=Task)
async def submit_clarification_answers(
    task_id: str,
    request: ClarificationAnswersRequest,
    _access: dict = Depends(require_task_access("member")),
):
    """Submit answers to clarification questions and append them to the task."""
    project_id, spec_id, project_path, spec_dir = _resolve_task(task_id)

    if not request.answers:
        return spec_to_task(project_id, spec_dir)

    # Build clarification appendix
    lines = ["\n\n## Clarifications\n"]
    for answer in request.answers:
        if answer.answer.strip():
            lines.append(f"**Q: {answer.question}**")
            lines.append(f"A: {answer.answer.strip()}\n")
    appendix = "\n".join(lines)

    # Update requirements.json description
    req_file = spec_dir / "requirements.json"
    if req_file.exists():
        requirements = json.loads(req_file.read_text())
        requirements["description"] = requirements.get("description", "") + appendix
        req_file.write_text(json.dumps(requirements, indent=2))

    # Append to spec.md
    spec_file = spec_dir / "spec.md"
    if spec_file.exists():
        content = spec_file.read_text()
        # Insert before ## Notes section if it exists, otherwise append
        if "\n## Notes\n" in content:
            content = content.replace("\n## Notes\n", f"{appendix}\n## Notes\n")
        else:
            content += appendix
        spec_file.write_text(content)

    return spec_to_task(project_id, spec_dir)
