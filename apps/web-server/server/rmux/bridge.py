"""WebSocket bridge between rmux pane FIFOs and browser xterm.js (Epic #44).

Two endpoints, both gated by ``AIFACTORY_RMUX_ENABLED``:

  GET  /api/tasks/{task_id}/agent-console/ws       (WebSocket)
       Streams pane bytes FIFO→browser.  In attach mode, also accepts
       browser keystrokes and forwards via ``rmux send-keys``.

  POST /api/tasks/{task_id}/agent-console/attach   (JSON)
       Body: ``{"connection_id": "..."}``.  Flips the named WS
       connection into bidirectional mode AND writes an
       ``audit.action=console.attach`` row.  At most one attached
       connection per session — concurrent POSTs lose to 409 Conflict.

The race-safe attach contract (design §3.1):

  1. WS server generates ``connection_id`` (UUID v4) on accept, sends
     it as the first ``{"type":"connected","connection_id":...}`` frame
  2. ``POST /attach`` acquires the per-session ``asyncio.Lock``
  3. If ``state.attached_connection_id is None``: set it to the
     request body's connection_id, write audit row, release lock,
     return 200
  4. Else: release lock, return 409 Conflict
  5. WS receive loop polls ``state.attached_connection_id == self.cid``
     to decide whether to forward inbound bytes
  6. On WS disconnect or ``POST /detach``: clear under the same lock,
     write ``console.detach`` audit row

Browser→pane byte encoding: xterm.js sends raw key bytes (e.g. ``\\x1b[A``
for up-arrow, ``\\x03`` for Ctrl-C, plain UTF-8 for printable text).
We forward via ``send_keys`` (no ``-l``) so rmux interprets control
sequences correctly.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import WebSocketAuthError, authenticate_websocket
from ..database.engine import async_session_factory, get_db
from ..routes.project_authz import (
    _auth_disabled,
    authorize_project_for_user,
    is_service_principal,
)
from ..services.audit_service import log_audit_event
from .session import SessionState, get_registry
from .wrapper import RmuxError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["rmux Live Console"])

# A service principal connecting over WebSocket authenticates via the legacy
# token, for which ``authenticate_websocket`` returns ``None``. Normalize that
# to an explicit service-principal user so the shared authz/audit path treats
# it consistently with the REST middleware (#322).
_WS_SERVICE_PRINCIPAL = {
    "id": "default",
    "email": None,
    "role": "admin",
    "is_service": True,
}


async def _authorize_console(
    user: dict | None,
    state: SessionState,
    db: AsyncSession,
    *,
    minimum_role: str = "member",
) -> str | None:
    """Authorize ``user`` for ``state``'s console. Raises ``HTTPException`` on
    denial; returns the owning ``org_id`` (or None) for audit attribution.

    Authorization is keyed on the session's **own** ``project_id`` (captured at
    creation), never a client-supplied path prefix — so ``a_project:b_spec``
    can't borrow A's access to reach B's session (#322 loose-addressing fix).
    A legacy session with no known ``project_id`` is service-principal-only.
    """
    if state.project_id is None:
        if not (_auth_disabled() or is_service_principal(user)):
            raise HTTPException(
                status_code=403,
                detail="Console access is not authorized for this session",
            )
        return None
    return await authorize_project_for_user(user, state.project_id, db, minimum_role)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _read_fifo_chunks(fifo_path: Path, chunk: int = 4096):
    """Async generator yielding bytes from ``fifo_path`` until close/EOF.

    Wraps the blocking read in ``asyncio.to_thread`` so the WS event
    loop never stalls on a slow pane.  Opening in binary mode preserves
    ANSI escape bytes intact for xterm.js.
    """

    def _open_blocking():
        return open(fifo_path, "rb", buffering=0)

    fh = await asyncio.to_thread(_open_blocking)
    try:
        while True:
            data = await asyncio.to_thread(fh.read, chunk)
            if not data:
                # FIFO writer closed.  In practice rmux's pipe-pane keeps
                # the writer open for the session's lifetime, so EOF
                # means the session was killed — bail.
                return
            yield data
    finally:
        try:
            fh.close()
        except OSError:
            pass


def _resolve_state_or_404(spec_id: str) -> SessionState:
    """Look up a session in the registry; raise 404 if missing.

    ``spec_id`` may arrive as a composite ``project_id:spec_id`` from
    older frontend routes.  We split on the first colon if present so
    the user can paste either form.
    """
    registry = get_registry()
    state = registry.get_state(spec_id)
    if state is None and ":" in spec_id:
        # Try the suffix half — some frontend routes pass the
        # ``project_id:spec_id`` form
        state = registry.get_state(spec_id.split(":", 1)[1])
    if state is None:
        raise HTTPException(
            status_code=404,
            detail=f"no rmux session registered for {spec_id}",
        )
    return state


def _local_state(spec_id: str) -> SessionState | None:
    """Return the pod-local session for ``spec_id`` (with composite fallback)."""
    registry = get_registry()
    state = registry.get_state(spec_id)
    if state is None and ":" in spec_id:
        state = registry.get_state(spec_id.split(":", 1)[1])
    return state


async def _resolve_remote_pane(spec_id: str) -> dict | None:
    """Resolve a session this pod doesn't host from the shared Redis index (#681).

    Returns the index entry (spec_id / session_name / project_id / passive) when
    a OTHER replica registered it, else None. No-op → None when Redis is off, so
    single-replica behaviour is unchanged.
    """
    from . import redis_transport

    if not redis_transport.redis_enabled():
        return None
    entry = await redis_transport.get_pane(spec_id)
    if entry is None and ":" in spec_id:
        entry = await redis_transport.get_pane(spec_id.split(":", 1)[1])
    return entry


def _remote_authz_state(spec_id: str, entry: dict | None) -> SessionState:
    """Build a minimal ``SessionState`` for a remote pane, for authz only (#681).

    Carries the ``project_id`` from the shared index so ``_authorize_console``
    keys on the session's real project exactly as for a local session. The
    ``fifo_path`` is unused on the remote path (bytes come from Redis pub/sub).
    """
    entry = entry or {}
    return SessionState(
        spec_id=str(entry.get("spec_id") or spec_id),
        session_name=str(entry.get("session_name") or f"aifactory-task-{spec_id}"),
        fifo_path=Path("/nonexistent"),
        project_id=entry.get("project_id"),
        passive=True,
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AttachRequest(BaseModel):
    """Body for ``POST /attach``.

    ``connection_id`` MUST match the value the server sent on the WS
    handshake's first frame — that's how we bind the audit row + the
    attach right to a specific browser tab.
    """

    connection_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="UUID v4 from the WS handshake's `connected` frame",
    )


# ---------------------------------------------------------------------------
# REST: POST /attach   POST /detach
# ---------------------------------------------------------------------------


@router.post("/{spec_id}/agent-console/attach")
async def attach(
    spec_id: str,
    body: AttachRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Flip the named connection into bidirectional input mode.

    On success: 200, writes ``audit.action=console.attach`` row.
    On race lost: 409, no audit row.
    On unknown spec: 404.

    The audit row binds (user_id, org_id, ip, connection_id, spec_id)
    so an investigator can answer "who attached when?" for any session.
    """
    state = _resolve_state_or_404(spec_id)
    cid = body.connection_id

    # Identity from middleware-set ``request.state.user`` (a dict) — NOT
    # ``user_id``/``org_id`` attributes, which the middleware never sets, so
    # audit rows previously recorded null (#322).
    user = getattr(request.state, "user", None)
    # Attach is interactive (write) → require at least 'member' on the task's
    # org, keyed on the session's real project (#322).
    org_id = await _authorize_console(user, state, db, minimum_role="member")
    user_id = user.get("id") if isinstance(user, dict) else None
    client_ip = request.client.host if request.client else None

    async with state.lock:
        if state.attached_connection_id is not None:
            # Someone already holds attach.  Return 409 with enough
            # context that the UI can render "another session has
            # control" without leaking the other user's identity.
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "session_already_attached",
                    "attached_connection_id": state.attached_connection_id,
                },
            )
        state.attached_connection_id = cid

    # Audit row OUTSIDE the lock — DB I/O is slow and we don't want to
    # serialise other tasks' lock acquisitions behind it.  Worst case
    # if the DB write fails, the attach is already in effect (the
    # in-memory flag flipped); the warning log is the operator's
    # signal something went wrong.  The audit_service helper already
    # wraps writes in try/except for exactly this reason.
    if db is not None:
        await log_audit_event(
            db,
            user_id=user_id,
            org_id=org_id,
            action="console.attach",
            resource_type="task",
            resource_id=state.spec_id,
            details={"connection_id": cid, "session_name": state.session_name},
            ip=client_ip,
        )

    return {"status": "attached", "connection_id": cid}


@router.post("/{spec_id}/agent-console/detach")
async def detach(
    spec_id: str,
    body: AttachRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Release attach mode held by ``connection_id``.

    Only the holder can detach (gates against a hostile client
    detaching someone else's session).  Writes ``console.detach``.

    Returns 200 even if the connection wasn't the holder, so the WS
    disconnect cleanup path can be fire-and-forget — that case is
    benign (race between client close and explicit detach).
    """
    state = _resolve_state_or_404(spec_id)
    cid = body.connection_id

    user = getattr(request.state, "user", None)
    org_id = await _authorize_console(user, state, db, minimum_role="viewer")
    user_id = user.get("id") if isinstance(user, dict) else None
    client_ip = request.client.host if request.client else None

    released = False
    async with state.lock:
        if state.attached_connection_id == cid:
            state.attached_connection_id = None
            released = True

    if released and db is not None:
        await log_audit_event(
            db,
            user_id=user_id,
            org_id=org_id,
            action="console.detach",
            resource_type="task",
            resource_id=state.spec_id,
            details={"connection_id": cid, "session_name": state.session_name},
            ip=client_ip,
        )

    return {"status": "detached" if released else "not_holder"}


# ---------------------------------------------------------------------------
# WebSocket: bidirectional pane bridge
# ---------------------------------------------------------------------------


@router.websocket("/{spec_id}/agent-console/ws")
async def agent_console_ws(websocket: WebSocket, spec_id: str):
    """Stream pane bytes FIFO→browser; accept browser keys when attached.

    Protocol:
      - First server frame: ``{"type":"connected","connection_id":"..."}``
        The client stores this UUID and includes it in any subsequent
        ``POST /attach`` call.
      - Subsequent server frames: raw binary pane bytes (ANSI intact).
      - Client→server frames: raw binary keystrokes; forwarded to
        ``rmux send-keys`` ONLY when this connection holds attach mode.
        Otherwise silently dropped (with a debug log) — read-only
        viewers MUST NOT be able to type by accident.

    Auth (#322): authenticate the token, then authorize the caller against the
    session's owning org (read-only 'viewer' to stream). The write path
    (keystrokes) additionally requires a successful ``POST /attach``, which
    demands 'member' — so a viewer can watch but never type.
    """
    try:
        ws_user = await authenticate_websocket(websocket)
    except WebSocketAuthError:
        return  # authenticate_websocket already closed the socket
    # Legacy-token callers authenticate as the service principal (None).
    user = ws_user if ws_user is not None else _WS_SERVICE_PRINCIPAL

    # Prefer the pod-local session (FIFO path, today's behaviour). RFC-0017 #681:
    # if it isn't local, fall back to the shared Redis index — the session may be
    # owned by another replica; we then stream its bytes over Redis pub/sub. Both
    # paths authorize against the session's real project (#322).
    state = _local_state(spec_id)
    remote: dict | None = None
    if state is None:
        remote = await _resolve_remote_pane(spec_id)
        if remote is None:
            await websocket.accept()
            await websocket.close(code=4004, reason="no rmux session for spec_id")
            return

    authz_state = state if state is not None else _remote_authz_state(spec_id, remote)
    async with async_session_factory() as db:
        try:
            await _authorize_console(user, authz_state, db, minimum_role="viewer")
        except HTTPException:
            await websocket.accept()
            await websocket.close(code=4003, reason="forbidden")
            return

    await websocket.accept()

    # Generate the connection_id and send it as the first frame so the
    # client can use it for /attach.
    cid = str(uuid.uuid4())
    await websocket.send_json({"type": "connected", "connection_id": cid})

    registry = get_registry()
    wrapper = registry.wrapper

    # Spawn two concurrent tasks:
    #  - reader: pump pane bytes → WS (always running). Local sessions read the
    #    FIFO; a remote session (RFC-0017 #681 — owned by another replica)
    #    streams bytes from the shared Redis pub/sub channel instead.
    #  - writer_listener: receive WS frames; if attach is held by us, forward to
    #    send-keys. Remote sessions are read-only (no local rmux pane to target),
    #    exactly like passive sessions.
    async def _reader():
        try:
            if state is not None:
                async for chunk in _read_fifo_chunks(state.fifo_path):
                    await websocket.send_bytes(chunk)
            else:
                from . import redis_transport

                async for chunk in redis_transport.subscribe_pane_bytes(spec_id):
                    await websocket.send_bytes(chunk)
        except WebSocketDisconnect:
            return
        except Exception:
            logger.warning(
                "agent-console reader crashed for %s",
                authz_state.spec_id,
                exc_info=True,
            )

    async def _writer_listener():
        # Remote (Redis-streamed) sessions have no local rmux pane to send keys
        # to — read-only, like passive sessions. Just drain inbound frames so the
        # socket close is observed.
        if state is None:
            try:
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        return
            except WebSocketDisconnect:
                return
            return
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                data = msg.get("bytes") or msg.get("text")
                if not data:
                    continue
                # Drop silently when not in attach mode.
                if state.attached_connection_id != cid:
                    logger.debug(
                        "dropping read-only WS input for %s (attached=%s, this=%s)",
                        state.spec_id,
                        state.attached_connection_id,
                        cid,
                    )
                    continue
                # Forward.  Convert bytes→str if necessary; rmux
                # send-keys accepts ESC sequences as raw text on stdin.
                payload = (
                    data.decode("utf-8", errors="replace")
                    if isinstance(data, bytes)
                    else data
                )
                try:
                    await wrapper.send_keys(state.session_name, payload)
                except RmuxError:
                    logger.warning(
                        "send-keys failed for %s (session gone?)",
                        state.spec_id,
                        exc_info=True,
                    )
        except WebSocketDisconnect:
            return

    reader_task = asyncio.create_task(_reader())
    writer_task = asyncio.create_task(_writer_listener())
    try:
        done, pending = await asyncio.wait(
            {reader_task, writer_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    finally:
        # Release attach mode if this connection held it — otherwise
        # the next attach POST would 409 forever. Only the local session carries
        # attach state; a remote (Redis-streamed) session has nothing to release.
        if state is not None:
            async with state.lock:
                if state.attached_connection_id == cid:
                    state.attached_connection_id = None
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - socket already gone; closing is best-effort
            logger.debug("WebSocket close failed during bridge teardown", exc_info=True)
