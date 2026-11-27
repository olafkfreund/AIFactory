"""Best-effort Matrix notices containing only a configured portal link."""

import logging
import os
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)


def portal_base_url() -> str:
    return os.environ.get("AIFACTORY_PORTAL_URL", "").strip().rstrip("/")


def notify_question_waiting() -> None:
    """Send one notice from a worker thread when called by async code.

    The 3-second HTTPX timeouts can add up to roughly 12 seconds across phases,
    and repeated reads can extend that further. Never call on the event loop thread.
    """
    homeserver = os.environ.get("AIFACTORY_BUS_HOMESERVER", "").strip().rstrip("/")
    room = os.environ.get("AIFACTORY_BUS_ROOM", "").strip()
    token_file = os.environ.get("AIFACTORY_BUS_TOKEN_FILE", "").strip()
    portal = portal_base_url()
    if not all((homeserver, room, token_file, portal)):
        return
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
        if not token:
            return
        parsed = urlsplit(homeserver)
        url = httpx.URL(homeserver)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or url.host != parsed.hostname
            or parsed.username is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid Matrix homeserver")
        endpoint = (
            f"{homeserver}/_matrix/client/v3/rooms/{quote(room, safe='')}"
            f"/send/m.room.message/{uuid4().hex}"
        )
        response = httpx.put(
            endpoint,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "msgtype": "m.text",
                "body": (
                    "[factory] a question is waiting for you — "
                    f"run aifactory.questions_pending or open {portal}/needs-input"
                ),
            },
            timeout=3.0,
            follow_redirects=False,
            trust_env=False,
        )
        response.raise_for_status()
        response.json()
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001 - notices must never interrupt a pause
        logger.warning("Question bus notice failed (%s)", type(exc).__name__)
