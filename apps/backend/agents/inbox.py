"""
Agent Inbox Reader — Between-Turn Message Consumption (#264)
===========================================================

The web-server writes durable, directed messages to a per-recipient inbox file
(see ``apps/web-server/server/services/inbox_service.py``). This module is the
**consumption side**: it lets a running backend agent drain its inbox between
turns and fold the user's message into the next prompt.

The on-disk schema/location is an AIFactory-OWNED contract shared with the
web-server (it does NOT depend on any agent SDK internal format):

    <spec_dir>/inbox/<recipient>.json   →  JSON array of message objects

This module deliberately re-implements only the read/drain path with the same
atomic + locked semantics, rather than importing the web-server package — the
backend runs as a separate subprocess and must not depend on web-server code.
The shared contract is the file format, not the Python module.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

DEFAULT_RECIPIENT = "agent"
_LOCK_TIMEOUT_SECONDS = 5.0


def _inbox_path(spec_dir: Path, recipient: str) -> Path:
    name = Path(recipient or DEFAULT_RECIPIENT).name
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_")) or DEFAULT_RECIPIENT
    return Path(spec_dir) / "inbox" / f"{safe}.json"


class _Lock:
    """Exclusive cross-process lock matching the web-server inbox lock."""

    def __init__(self, target: Path, timeout: float = _LOCK_TIMEOUT_SECONDS):
        self._lock_path = target.parent / f"{target.name}.lock"
        self._timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> _Lock:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
        if fcntl is None:  # pragma: no cover - non-POSIX
            return self
        start = time.time()
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                if time.time() - start >= self._timeout:
                    os.close(self._fd)
                    self._fd = None
                    raise TimeoutError(f"inbox lock timeout: {self._lock_path}")
                time.sleep(0.01)

    def __exit__(self, *_exc: object) -> bool:
        if self._fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        # The .lock sentinel is intentionally left in place (never unlinked):
        # deleting a flock target races concurrent holders. See the matching
        # note in the web-server inbox_service.
        return False


def _read(inbox_path: Path) -> list[dict[str, Any]]:
    if not inbox_path.exists():
        return []
    raw = inbox_path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    data = json.loads(raw)
    return data if isinstance(data, list) else []


def _write_atomic(inbox_path: Path, messages: list[dict[str, Any]]) -> None:
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=inbox_path.parent, prefix=f".{inbox_path.name}.tmp.", suffix=""
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(messages, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, inbox_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def has_pending(spec_dir: Path, recipient: str = DEFAULT_RECIPIENT) -> bool:
    """Cheap check for any unread message (no mutation)."""
    try:
        return any(not m.get("read", False) for m in _read(_inbox_path(spec_dir, recipient)))
    except (OSError, json.JSONDecodeError):
        return False


def drain_unread(
    spec_dir: Path, recipient: str = DEFAULT_RECIPIENT
) -> list[dict[str, Any]]:
    """Atomically fetch unread messages and mark them read (exactly-once).

    Returns the messages transitioned from unread → read. Safe to call every
    turn; returns ``[]`` when the inbox is empty or unreadable.
    """
    inbox_path = _inbox_path(spec_dir, recipient)
    if not inbox_path.exists():
        return []
    try:
        with _Lock(inbox_path):
            messages = _read(inbox_path)
            unread = [m for m in messages if not m.get("read", False)]
            if unread:
                for m in unread:
                    m["read"] = True
                _write_atomic(inbox_path, messages)
            return unread
    except (OSError, json.JSONDecodeError, TimeoutError):
        # Inbox problems must never crash a build; skip this turn.
        return []


def format_for_prompt(messages: list[dict[str, Any]]) -> str:
    """Render drained messages as a prompt directive block for the agent."""
    if not messages:
        return ""
    lines = [
        "## Incoming user messages",
        "",
        "The user sent the following directed message(s) while you were working. "
        "Treat them as high-priority instructions for the current task:",
        "",
    ]
    for m in messages:
        sender = m.get("from", "user")
        text = (m.get("text") or "").strip()
        lines.append(f"- **From {sender}:** {text}")
    return "\n".join(lines)


def post_message(
    spec_dir: Path,
    text: str,
    *,
    sender: str = "user",
    recipient: str = DEFAULT_RECIPIENT,
    source: str | None = None,
) -> dict[str, Any]:
    """Append an unread message to a spec's inbox (the async-feedback channel).

    Public writer counterpart to ``drain_unread`` — used by the artifact async
    review (#397 Phase 6) and any caller that needs to hand the agent a
    high-priority message it will pick up between turns. Atomic + locked.
    """
    message: dict[str, Any] = {"from": sender, "text": text, "read": False}
    if source:
        message["source"] = source
    inbox_path = _inbox_path(spec_dir, recipient)
    with _Lock(inbox_path):
        messages = _read(inbox_path)
        messages.append(message)
        _write_atomic(inbox_path, messages)
    return message
