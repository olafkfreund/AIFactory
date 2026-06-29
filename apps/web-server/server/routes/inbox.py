"""
Inbox / inter-agent messaging routes (#264).

Extracted verbatim from ``routes/tasks.py`` (issue #556) as a behavior-preserving
sub-router. The endpoints are mounted onto the tasks router via ``include_router``
so the public paths and request/response shapes are unchanged:

    POST /{task_id}/inbox
    GET  /{task_id}/inbox

The handlers depend only on already-extractable collaborators --
``require_task_access`` from ``routes/project_authz``, ``_resolve_task`` from the
leaf ``routes/task_service`` (the single canonical resolver, #769 -- importing it
from there imports neither ``.tasks`` nor ``.inbox``, so no circular import), and
the ``inbox_service`` (imported lazily inside the handlers). The small worktree
path helper (``_get_worktree_spec_dir``) remains inlined here.

The models (``InboxMessageCreate``, ``InboxMessage``, ``InboxEnqueueResponse``)
and the handlers (``enqueue_inbox_message``, ``list_inbox_messages``) historically
lived in ``routes/tasks.py`` and are re-exported there for backward compatibility.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .project_authz import require_task_access
from .task_service import _resolve_task

router = APIRouter()


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class InboxMessageCreate(BaseModel):
    """Request to deliver a directed message to a running task's agent."""

    text: str = Field(..., min_length=1, description="Message body for the agent")
    recipient: str = Field(
        "agent",
        description="Logical agent recipient (e.g. 'agent', 'coder', 'planner')",
    )
    sender: str = Field("user", description="Sender identity")
    summary: str | None = Field(
        None, description="Optional short summary (auto-derived if omitted)"
    )


class InboxMessage(BaseModel):
    """A stored inbox message."""

    from_: str = Field(..., alias="from")
    text: str
    summary: str
    timestamp: str
    messageId: str
    read: bool

    model_config = {"populate_by_name": True}


class InboxEnqueueResponse(BaseModel):
    """Response after enqueuing an inbox message."""

    messageId: str
    delivered: bool
    recipient: str


# --------------------------------------------------------------------------
# Pure path helpers
# --------------------------------------------------------------------------
# _resolve_task is imported from routes/task_service.py (#769) -- the single
# canonical definition shared with routes/tasks.py.


def _get_worktree_spec_dir(project_path: Path, spec_id: str) -> Path | None:
    """Get the worktree spec directory if it exists.

    Worktree layout: .aifactory/worktrees/tasks/{spec_id}/.aifactory/specs/{spec_id}/
    """
    worktree_spec_dir = (
        project_path
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / spec_id
        / ".aifactory"
        / "specs"
        / spec_id
    )
    if worktree_spec_dir.exists():
        return worktree_spec_dir
    return None


def _inbox_target_spec_dir(project_path: Path, spec_id: str, spec_dir: Path) -> Path:
    """Resolve where to write the inbox so a RUNNING agent will see it.

    A running build executes inside its isolated worktree, reading from the
    worktree spec dir. If that worktree exists we target it; otherwise we fall
    back to the main spec dir (message will be picked up when a build starts).
    """
    worktree_spec_dir = _get_worktree_spec_dir(project_path, spec_id)
    return worktree_spec_dir if worktree_spec_dir else spec_dir


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@router.post(
    "/{task_id}/inbox",
    response_model=InboxEnqueueResponse,
    status_code=status.HTTP_201_CREATED,
)
async def enqueue_inbox_message(
    task_id: str,
    message: InboxMessageCreate,
    _access: dict = Depends(require_task_access("member")),
):
    """Deliver a directed message to a task's agent inbox (#264).

    The message is written atomically and verified on disk. The returned
    `delivered` flag is the read-back proof that the message persisted.
    """
    from ..services import inbox_service

    _project_id, spec_id, project_path, spec_dir = _resolve_task(task_id)
    target_dir = _inbox_target_spec_dir(project_path, spec_id, spec_dir)

    try:
        result = inbox_service.enqueue(
            target_dir,
            text=message.text,
            recipient=message.recipient,
            sender=message.sender,
            summary=message.summary,
        )
    except inbox_service.DeliveryVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Message could not be verified after write: {exc}",
        ) from exc
    except inbox_service.InboxError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return InboxEnqueueResponse(
        messageId=result["messageId"],
        delivered=result["delivered"],
        recipient=result["recipient"],
    )


@router.get("/{task_id}/inbox", response_model=list[InboxMessage])
async def list_inbox_messages(
    task_id: str,
    recipient: str = Query("agent", description="Recipient inbox to read"),
    unread_only: bool = Query(False, description="Only return unread messages"),
    _access: dict = Depends(require_task_access("viewer")),
):
    """List messages in a task's agent inbox."""
    from ..services import inbox_service

    _project_id, spec_id, project_path, spec_dir = _resolve_task(task_id)
    target_dir = _inbox_target_spec_dir(project_path, spec_id, spec_dir)

    try:
        messages = inbox_service.list_messages(
            target_dir, recipient=recipient, unread_only=unread_only
        )
    except inbox_service.InboxError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    return [InboxMessage(**m) for m in messages]
