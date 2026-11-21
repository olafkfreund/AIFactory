"""Project access for per-task WebSocket streams."""

import time
from typing import Any

from fastapi import WebSocket

from server.auth import (
    WebSocketAuthError,
    _ws_acw_principal,
    _ws_extract_token,
    authenticate_websocket,
)
from server.config import get_settings
from server.database import engine
from server.routes.project_authz import authorize_project_for_user, is_service_principal

_KEY_RECHECK_SECONDS = 300


async def _project_authorized(user: dict[str, Any], project_id: str | None) -> bool:
    if (
        user.get("is_legacy_shared_token")
        and get_settings().LEGACY_SHARED_TOKEN_FULL_ACCESS
    ):
        return True
    try:
        async with engine.async_session_factory() as db:
            await authorize_project_for_user(user, project_id, db)
    except Exception:  # noqa: BLE001 - denial or lookup failure fails closed
        return False
    return True


class TaskSocketAccess:
    def __init__(self, websocket: WebSocket, task_id: str):
        self.websocket = websocket
        self.task_id = task_id
        self.project_id = task_id.split(":", 1)[0] if ":" in task_id else None
        self.user: dict[str, Any] | None = None
        self.key_token: str | None = None
        self.last_check = time.monotonic()

    async def connect(self) -> bool:
        try:
            self.user = await authenticate_websocket(self.websocket)
        except WebSocketAuthError:
            return False
        except Exception:  # noqa: BLE001 - unreadable auth state fails closed
            await self.websocket.close(code=4403)
            return False
        if self.user is None and get_settings().DISABLE_AUTH:
            return True
        if self.user is None:
            await self.websocket.close(code=4401)
            return False
        if not self.project_id or not self.task_id.split(":", 1)[1]:
            await self.websocket.close(code=4403)
            return False
        if not await self._authorized(self.user, 4403):
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
        except Exception:  # noqa: BLE001 - a failing validator counts as revoked
            principal = None
        if principal is None:
            await self.websocket.close(code=4401)
            return False
        if not await self._authorized(principal, 4401):
            return False
        self.user = principal
        self.last_check = time.monotonic()
        return True

    async def _authorized(self, user: dict[str, Any], close_code: int) -> bool:
        if not await _project_authorized(user, self.project_id):
            await self.websocket.close(code=close_code)
            return False
        return True


class AllTaskSocketAccess:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.user: dict[str, Any] | None = None
        self.key_token: str | None = None
        self.last_check = time.monotonic()

    async def connect(self) -> bool:
        try:
            self.user = await authenticate_websocket(self.websocket)
        except WebSocketAuthError:
            return False
        except Exception:  # noqa: BLE001 - unreadable auth state fails closed
            await self.websocket.close(code=4403)
            return False
        token = _ws_extract_token(self.websocket)
        if (
            token
            and token.startswith("acw_")
            and self.user
            and self.user.get("api_key_id")
        ):
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
        except Exception:  # noqa: BLE001 - a failing validator counts as revoked
            principal = None
        if principal is None:
            await self.websocket.close(code=4401)
            return False
        self.user = principal
        self.last_check = time.monotonic()
        return True

    async def readable(self, task_ids: list[str]) -> set[str]:
        if self.user is None:
            return set(task_ids) if get_settings().DISABLE_AUTH else set()
        if is_service_principal(self.user) or (
            self.user.get("is_legacy_shared_token")
            and get_settings().LEGACY_SHARED_TOKEN_FULL_ACCESS
        ):
            return set(task_ids)
        projects: dict[str, bool] = {}
        readable = set()
        for task_id in task_ids:
            project_id, separator, spec_id = task_id.partition(":")
            if not separator or not project_id or not spec_id:
                continue
            if project_id not in projects:
                projects[project_id] = await _project_authorized(self.user, project_id)
            if projects[project_id]:
                readable.add(task_id)
        return readable
