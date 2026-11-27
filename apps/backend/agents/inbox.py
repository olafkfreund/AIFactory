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

import contextlib
import json
import logging
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
logger = logging.getLogger(__name__)


def _inbox_path(spec_dir: Path, recipient: str) -> Path:
    name = Path(recipient or DEFAULT_RECIPIENT).name
    safe = (
        "".join(c for c in name if c.isalnum() or c in ("-", "_")) or DEFAULT_RECIPIENT
    )
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
    if not isinstance(data, list):
        return []
    if any(not isinstance(item, dict) for item in data):
        raise ValueError("invalid inbox messages")
    return data


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
        # Best-effort temp-file cleanup on the failure path; the real error
        # is re-raised regardless of whether the unlink succeeds.
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def has_pending(spec_dir: Path, recipient: str = DEFAULT_RECIPIENT) -> bool:
    """Cheap check for any unread message (no mutation)."""
    try:
        return any(
            not m.get("read", False) for m in _read(_inbox_path(spec_dir, recipient))
        )
    except (OSError, ValueError, RecursionError):
        return False


def drain_unread(
    spec_dir: Path,
    recipient: str = DEFAULT_RECIPIENT,
    source_spec_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Atomically fetch unread messages and mark them read (exactly-once).

    Returns the messages transitioned from unread → read. Safe to call every
    turn; returns ``[]`` when the inbox is empty or unreadable.
    """
    inbox_path = _inbox_path(spec_dir, recipient)
    try:
        if not inbox_path.exists():
            return []
        with _Lock(inbox_path), contextlib.ExitStack() as locks:
            messages = _read(inbox_path)
            unread = [m for m in messages if not m.get("read", False)]
            if not unread:
                return []
            source = None
            source_messages: list[dict[str, Any]] = []
            answer_ids = {
                m["answer_id"] for m in unread if isinstance(m.get("answer_id"), str)
            }
            if (
                answer_ids
                and source_spec_dir is not None
                and source_spec_dir not in (spec_dir, Path(os.devnull))
            ):
                source = _inbox_path(source_spec_dir, recipient)
                try:
                    if not source.exists():
                        logger.warning("Source inbox is missing for drained answers")
                        source = None
                    elif not source.samefile(inbox_path):
                        locks.enter_context(_Lock(source))
                        source_messages = _read(source)
                    else:
                        source = None
                except (OSError, ValueError, RecursionError):
                    logger.warning(
                        "Could not read source inbox for drained answers", exc_info=True
                    )
                    source = None
            already_read = {
                m["answer_id"]
                for m in source_messages
                if m.get("read", False) and isinstance(m.get("answer_id"), str)
            }
            for m in unread:
                m["read"] = True
            _write_atomic(inbox_path, messages)
            if source is not None:
                _mark_source_read(source, source_messages, answer_ids)
            return [
                m
                for m in unread
                if not isinstance(m.get("answer_id"), str)
                or m["answer_id"] not in already_read
            ]
    except (OSError, ValueError, RecursionError):
        # Inbox problems must never crash a build; skip this turn.
        return []


def _mark_source_read(
    source: Path, messages: list[dict[str, Any]], answer_ids: set[str]
) -> None:
    matched: set[str] = set()
    changed = False
    for message in messages:
        answer_id = message.get("answer_id")
        if isinstance(answer_id, str) and answer_id in answer_ids:
            matched.add(answer_id)
            if not message.get("read", False):
                message["read"] = True
                changed = True
    if answer_ids - matched:
        logger.warning("Drained answers were missing from the source inbox")
    if changed:
        try:
            _write_atomic(source, messages)
        except (OSError, ValueError, RecursionError):
            logger.warning(
                "Could not mark drained answers read in the source inbox", exc_info=True
            )


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
