"""Project access for per-task WebSocket streams."""

import time

from fastapi import WebSocket

from ..auth import (
    WebSocketAuthError,
    _ws_acw_principal,
    _ws_extract_token,
    authenticate_websocket,
)
from ..database import engine
from ..routes.project_authz import authorize_project_for_user

_KEY_RECHECK_SECONDS = 300


class TaskSocketAccess:
    def __init__(self, websocket: WebSocket, task_id: str):
        self.websocket = websocket
        self.task_id = task_id
        self.project_id = task_id.split(":", 1)[0] if ":" in task_id else None
        self.user: dict | None = None
        self.key_token: str | None = None
        self.last_check = time.monotonic()

    async def connect(self) -> bool:
        try:
            self.user = await authenticate_websocket(self.websocket)
        except WebSocketAuthError:
            return False
        except Exception:
            await self.websocket.close(code=4003)
            return False
        if self.user is None:
            return True
        if not self.project_id or not self.task_id.split(":", 1)[1]:
            await self.websocket.close(code=4003)
            return False
        if not await self._authorized(self.user, 4003):
            return False
        token = _ws_extract_token(self.websocket)
        if token and token.startswith("acw_") and self.user.get("api_key_id"):
            self.key_token = token
        self.last_check = time.monotonic()
        return True

    async def recheck(self) -> bool:
        if (
            not self.key_token
            or time.monotonic() - self.last_check < _KEY_RECHECK_SECONDS
        ):
            return True
        try:
            principal = await _ws_acw_principal(self.key_token)
        except Exception:
            principal = None
        if principal is None:
            await self.websocket.close(code=4001)
            return False
        if not await self._authorized(principal, 4001):
            return False
        self.user = principal
        self.last_check = time.monotonic()
        return True

    async def _authorized(self, user: dict, close_code: int) -> bool:
        try:
            async with engine.async_session_factory() as db:
                await authorize_project_for_user(user, self.project_id, db)
        except Exception:
            await self.websocket.close(code=close_code)
            return False
        return True
