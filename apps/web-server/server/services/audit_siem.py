"""Optional, best-effort audit delivery to OpenObserve."""

import asyncio
import base64
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)
_tasks: set[asyncio.Task[None]] = set()


def siem_config() -> tuple[str, str] | None:
    """Read the endpoint and Basic credentials at call time."""
    url = os.environ.get("AIFACTORY_AUDIT_SIEM_URL", "")
    user = os.environ.get("AIFACTORY_AUDIT_SIEM_USER", "")
    password = os.environ.get("AIFACTORY_AUDIT_SIEM_PASSWORD", "")
    if not url or not user or not password:
        return None
    org = os.environ.get("AIFACTORY_AUDIT_SIEM_ORG", "default")
    stream = os.environ.get("AIFACTORY_AUDIT_SIEM_STREAM", "audit_chain")
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"{url.rstrip('/')}/api/{org}/{stream}/_json", f"Basic {credentials}"


def _client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=3)


async def emit(action: str, **fields) -> None:
    """Send one record without allowing delivery failures to affect auth."""
    try:
        config = siem_config()
        if config is None:
            return
        endpoint, auth = config
        record = {
            "_timestamp": int(time.time() * 1_000_000),
            "action": action,
            "source": "aifactory",
            **fields,
        }
        async with _client_factory() as client:
            response = await client.post(
                endpoint, headers={"Authorization": auth}, json=[record]
            )
            response.raise_for_status()
    except Exception as e:
        logger.warning("audit SIEM push failed: %s", type(e).__name__)


def schedule(action: str, **fields) -> None:
    """Keep background delivery alive until it finishes."""
    if siem_config() is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(emit(action, **fields))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
