"""
Global Events WebSocket with per-client routing.

Supports both broadcast (legacy) and targeted delivery based on
user identity. When a JWT-authenticated user connects, events
can be routed only to members of the relevant organization.
Legacy (bearer-token) connections receive all events (backward
compatible).

Epic #35 #40 PR-1: the three public functions (``broadcast_event``,
``send_to_user``, ``send_to_org``) are now thin shims over
``event_bus.publish_event``. Their signatures, behavior, and the
client registry semantics are unchanged. Setting ``REDIS_URL``
enables cross-replica delivery; leaving it unset preserves v1.0
in-process-only behavior.
"""

import asyncio
import json
import logging

from factory_common.logsafe import sanitize_log
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..auth import WebSocketAuthError, authenticate_websocket
from . import event_bus
from .event_bus import (
    BroadcastScope,
    ConnectedClient,
    OrgScope,
    UserScope,
    active_connections,
    register_client,
    unregister_client,
    update_client_orgs,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# Re-export so any external code that imported these from events.py
# (legacy module path) keeps working unchanged.
__all__ = [
    "ConnectedClient",
    "active_connections",
    "broadcast_event",
    "emit_changelog_progress",
    "emit_insights_chunk",
    "emit_insights_status",
    "emit_profile_switch",
    "emit_subtask_update",
    "emit_task_error",
    "emit_task_log",
    "emit_task_logs_stream",
    "emit_task_progress",
    "emit_task_status",
    "emit_task_update",
    "router",
    "send_to_org",
    "send_to_user",
    "update_client_orgs",
]


# ---------------------------------------------------------------------------
# Public event-emit functions — thin shims over the event bus.
#
# The 65+ call sites across the codebase use these. Their signatures
# stay frozen; only the internals route through the bus so a single
# code change unlocks cross-replica delivery for everyone.
# ---------------------------------------------------------------------------


async def broadcast_event(event_type: str, payload: dict):
    """Broadcast an event to all connected clients (legacy behavior)."""
    await event_bus.publish_event(BroadcastScope(), event_type, payload)


async def send_to_user(user_id: str, event_type: str, payload: dict):
    """Send an event to a specific user (all their connections)."""
    await event_bus.publish_event(UserScope(user_id=user_id), event_type, payload)


async def send_to_org(org_id: str, event_type: str, payload: dict):
    """Send an event only to members of a specific organization.

    Falls back to broadcast for legacy (non-JWT) connections so they
    aren't excluded.
    """
    await event_bus.publish_event(OrgScope(org_id=org_id), event_type, payload)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@router.websocket("/ws/events")
async def events_websocket(websocket: WebSocket):
    """WebSocket endpoint for global events."""
    await websocket.accept()

    # Authenticate — get user info if JWT, None for legacy token
    try:
        user_info = await authenticate_websocket(websocket)
    except WebSocketAuthError:
        return

    client = register_client(websocket, user_info)

    # If authenticated user, load their org memberships for routing
    if user_info and user_info.get("id"):
        try:
            from sqlalchemy import select

            from ..database import OrgMember
            from ..database.engine import async_session_factory

            async with async_session_factory() as session:
                result = await session.execute(
                    select(OrgMember.org_id).where(OrgMember.user_id == user_info["id"])
                )
                client.org_ids = {row[0] for row in result.all()}
        except Exception:
            logger.debug("Could not load org memberships for WS client", exc_info=True)

    try:
        # Keep connection alive and listen for pings
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)

                # Handle ping/pong
                if data == "ping":
                    await websocket.send_text("pong")

            except TimeoutError:
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break

    except WebSocketDisconnect:
        logger.debug("Events WebSocket client disconnected")
    except Exception:  # noqa: BLE001 - log the unexpected drop, then clean up in finally
        logger.warning("Events WebSocket closed unexpectedly", exc_info=True)
    finally:
        unregister_client(websocket)


# ---------------------------------------------------------------------------
# Helper functions for different event types
# ---------------------------------------------------------------------------


async def emit_task_progress(task_id: str, progress: dict):
    logging.getLogger(__name__).info(
        f"[WebSocket] Emitting task:progress - taskId: {task_id}, percentage: {progress.get('percentage', 'N/A')}%"
    )
    await broadcast_event("task:progress", {"taskId": task_id, **progress})


async def emit_task_error(task_id: str, error: str):
    logging.getLogger(__name__).info(
        f"[WebSocket] Emitting task:error - taskId: {sanitize_log(task_id)}, "
        f"error: {sanitize_log(error[:100])}..."
    )
    await broadcast_event("task:error", {"taskId": task_id, "error": error})


async def emit_task_status(task_id: str, status: str, review_reason: str | None = None):
    payload = {"taskId": task_id, "status": status}
    if review_reason:
        payload["reviewReason"] = review_reason
        logging.getLogger(__name__).info(
            f"[WebSocket] Emitting task:status - taskId: {sanitize_log(task_id)}, "
            f"status: {sanitize_log(status)}, "
            f"reviewReason: {sanitize_log(review_reason)}"
        )
    else:
        logging.getLogger(__name__).info(
            f"[WebSocket] Emitting task:status - taskId: {sanitize_log(task_id)}, "
            f"status: {sanitize_log(status)}"
        )
    await broadcast_event("task:status", payload)


async def emit_task_log(task_id: str, log: str):
    # Only log the first 50 chars to avoid flooding logs with full log content
    log_preview = sanitize_log(log[:50])
    logging.getLogger(__name__).debug(
        f"[WebSocket] Emitting task:log - taskId: {sanitize_log(task_id)}, "
        f"log: {log_preview}..."
    )
    await broadcast_event("task:log", {"taskId": task_id, "log": log})


async def emit_task_update(task_id: str, task_data: dict):
    """Emit task data update for frontend to refresh task card."""

    exec_progress = task_data.get("executionProgress", {})
    phase = exec_progress.get("phase", "N/A") if exec_progress else "N/A"
    progress = exec_progress.get("phaseProgress", "N/A") if exec_progress else "N/A"
    logging.getLogger(__name__).info(
        f"[WebSocket] Emitting task:update - taskId: {sanitize_log(task_id)}, "
        f"phase: {sanitize_log(phase)}, progress: {sanitize_log(progress)}%"
    )
    await broadcast_event("task:update", {"taskId": task_id, **task_data})


async def emit_changelog_progress(project_id: str, progress: dict):
    await broadcast_event("changelog:progress", {"projectId": project_id, **progress})


async def emit_insights_chunk(project_id: str, chunk: str):
    await broadcast_event("insights:chunk", {"projectId": project_id, "chunk": chunk})


async def emit_insights_status(project_id: str, status: str):
    await broadcast_event(
        "insights:status", {"projectId": project_id, "status": status}
    )


async def emit_profile_switch(task_id: str, switch_data: dict):
    """Emit profile switch event for reactive failover."""

    from_profile = switch_data.get("fromProfile", "N/A")
    to_profile = switch_data.get("toProfile", "N/A")
    logging.getLogger(__name__).info(
        f"[WebSocket] Emitting task:profile-switch - taskId: {task_id}, from: {from_profile}, to: {to_profile}"
    )
    await broadcast_event("task:profile-switch", {"taskId": task_id, **switch_data})


async def emit_task_logs_stream(spec_id: str, chunk: dict):
    """Emit a task log chunk for real-time streaming to open task detail modals.

    This event streams individual log entries as they're added to task_logs.json,
    enabling live updates in the frontend without file polling.

    Args:
        spec_id: The spec/task identifier (e.g., "007-task-update-progress-logs")
        chunk: The log chunk dict matching TaskLogStreamChunk interface:
            - type: 'text' | 'tool_start' | 'tool_end' | 'phase_start' | 'phase_end' | 'error'
            - content: (optional) Log message content
            - phase: (optional) Current phase (planning, coding, validation)
            - timestamp: (optional) ISO timestamp
            - tool: (optional) { name: string, input?: string, success?: boolean }
            - subtask_id: (optional) Current subtask identifier
    """

    chunk_type = sanitize_log(chunk.get("type", "unknown"))
    content_preview = sanitize_log((chunk.get("content") or "")[:50])
    logging.getLogger(__name__).debug(
        f"[WebSocket] Emitting task-logs:stream - specId: {sanitize_log(spec_id)}, "
        f"type: {chunk_type}, content: {content_preview}..."
    )
    await broadcast_event("task-logs:stream", {"specId": spec_id, "chunk": chunk})


async def emit_subtask_update(
    task_id: str, subtask_id: str, status: str, previous_status: str | None = None
):
    """Emit a subtask status change event for granular real-time updates.

    This event is emitted when an individual subtask's status changes, allowing
    the frontend to update subtask checkmarks in real-time without waiting for
    the full task update cycle.

    Args:
        task_id: The task/spec identifier
        subtask_id: The subtask identifier (e.g., "1.1", "2.3")
        status: The new status ("pending", "in_progress", "completed", "failed")
        previous_status: The previous status (optional, for logging/debugging)
    """

    logger = logging.getLogger(__name__)
    if previous_status:
        logger.info(
            "[WebSocket] Emitting task:subtask-update - taskId: %s, "
            "subtaskId: %s, status: %s -> %s",
            sanitize_log(task_id),
            sanitize_log(subtask_id),
            sanitize_log(previous_status),
            sanitize_log(status),
        )
    else:
        logger.info(
            f"[WebSocket] Emitting task:subtask-update - taskId: {sanitize_log(task_id)}, "
            f"subtaskId: {sanitize_log(subtask_id)}, status: {sanitize_log(status)}"
        )
    await broadcast_event(
        "task:subtask-update",
        {
            "taskId": task_id,
            "subtaskId": subtask_id,
            "status": status,
            "previousStatus": previous_status,
        },
    )
