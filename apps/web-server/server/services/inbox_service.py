"""
Inbox Service — Durable File-Based Agent Messaging (#264)
========================================================

A durable, file-based inbox that lets the UI/user deliver a directed message
to an agent that is ALREADY RUNNING. The agent picks the message up between
turns (see the consumption hook in apps/backend/agents/inbox.py).

This is an **AIFactory-OWNED** schema and location — it does NOT depend on any
undocumented internal inbox format of an underlying agent SDK.

On-disk location
----------------
    <spec_dir>/inbox/<recipient>.json

where ``<spec_dir>`` is ``.aifactory/specs/<spec_id>/`` and ``<recipient>`` is
the logical agent name (e.g. ``coder``, ``planner``, or ``agent`` for the
default running session).

On-disk format
--------------
A single JSON **array** of message objects. Each message:

    {
        "from":      str,   # sender identity (e.g. "user", "ui")
        "text":      str,   # full message body delivered to the agent
        "summary":   str,   # short one-line summary (for logs/UI)
        "timestamp": str,   # ISO-8601 UTC creation time
        "messageId": str,   # unique id (uuid4 hex) — used for delivery proof
        "read":      bool,  # True once an agent has consumed it
    }

Why a JSON array (not newline-delimited)?
    Messages are short and low-volume (human directives between agent turns),
    and bounded per spec. A whole-file array rewrite pairs naturally with the
    atomic tmp+os.replace pattern and makes mark-as-read a simple in-place
    mutation. NDJSON optimises for high-throughput append-only logs we do not
    have here, and complicates the read-modify-write needed for mark-as-read.

Reliability properties
-----------------------
1. **Atomic write**: every mutation writes to a temp file in the same
   directory, then ``os.replace()`` swaps it in (atomic on POSIX and
   same-volume Windows). A reader never sees a half-written file.
2. **Delivery proof**: after enqueue, the file is re-read from disk and the
   new ``messageId`` is asserted present. ``enqueue`` returns this verified
   flag so the caller can surface real delivery status.
3. **Concurrency safety**: all read-modify-write cycles run under an exclusive
   cross-process ``FileLock`` so concurrent writers cannot corrupt the array
   or lose messages.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Cross-process locking (best-effort, POSIX via fcntl / Windows via msvcrt).
#
# We keep this self-contained rather than importing the backend's runners
# helper, because the web-server does not import the backend package at module
# load time (the backend runs as a subprocess). The on-disk schema is the
# shared contract, not Python code.
# ---------------------------------------------------------------------------

_IS_WINDOWS = os.name == "nt"
_WINDOWS_LOCK_SIZE = 1024 * 1024

try:  # pragma: no cover - platform dependent
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:  # pragma: no cover - platform dependent
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None


# Default recipient name for the single running agent session.
DEFAULT_RECIPIENT = "agent"

# Lock acquisition timeout for inbox mutations.
_LOCK_TIMEOUT_SECONDS = 5.0


class InboxError(Exception):
    """Base error for inbox operations."""


class DeliveryVerificationError(InboxError):
    """Raised when an enqueued message cannot be verified on disk."""


class _InboxLock:
    """Minimal exclusive cross-process file lock (separate .lock file)."""

    def __init__(self, target: Path, timeout: float = _LOCK_TIMEOUT_SECONDS):
        self._lock_path = target.parent / f"{target.name}.lock"
        self._timeout = timeout
        self._fd: int | None = None

    def _acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
        start = time.time()
        while True:
            try:
                if _IS_WINDOWS:
                    if msvcrt is None:  # pragma: no cover
                        return  # No locking primitive available; proceed.
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, _WINDOWS_LOCK_SIZE)
                else:
                    if fcntl is None:  # pragma: no cover
                        return
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except (BlockingIOError, OSError):
                if time.time() - start >= self._timeout:
                    os.close(self._fd)
                    self._fd = None
                    raise InboxError(
                        f"Timed out acquiring inbox lock on {self._lock_path}"
                    )
                time.sleep(0.01)

    def _release(self) -> None:
        if self._fd is not None:
            try:
                if _IS_WINDOWS:
                    if msvcrt is not None:  # pragma: no cover
                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, _WINDOWS_LOCK_SIZE)
                elif fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError as exc:  # pragma: no cover - best effort
                warnings.warn(f"Failed to release inbox lock: {exc}", stacklevel=2)
            finally:
                os.close(self._fd)
                self._fd = None
        # NOTE: the .lock file is intentionally NOT unlinked. Deleting a
        # flock target while other processes hold/await it creates a race
        # where two holders can lock different inodes at the same path and
        # both proceed. The lock file is a tiny, stable sentinel left in
        # place for the lifetime of the inbox.

    def __enter__(self) -> _InboxLock:
        self._acquire()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self._release()
        return False


# ---------------------------------------------------------------------------
# Path / IO helpers
# ---------------------------------------------------------------------------


def _sanitize_recipient(recipient: str) -> str:
    """Reduce a recipient to a safe single path component.

    Prevents path traversal / nested-dir escapes from a recipient value that
    may originate from an API caller.
    """
    name = (recipient or "").strip() or DEFAULT_RECIPIENT
    # Keep only the final component and strip anything non-trivial.
    name = Path(name).name
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_"))
    return safe or DEFAULT_RECIPIENT


def get_inbox_path(spec_dir: Path, recipient: str = DEFAULT_RECIPIENT) -> Path:
    """Return the inbox file path for a recipient within a spec directory."""
    return Path(spec_dir) / "inbox" / f"{_sanitize_recipient(recipient)}.json"


def _read_messages(inbox_path: Path) -> list[dict[str, Any]]:
    """Read and parse the message array. Returns [] if absent or empty."""
    if not inbox_path.exists():
        return []
    try:
        raw = inbox_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise InboxError(f"Failed to read inbox {inbox_path}: {exc}") from exc
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InboxError(f"Inbox file {inbox_path} is corrupt: {exc}") from exc
    if not isinstance(data, list):
        raise InboxError(f"Inbox file {inbox_path} is not a JSON array")
    return data


def _write_messages_atomic(inbox_path: Path, messages: list[dict[str, Any]]) -> None:
    """Atomically write the full message array (tmp file + os.replace)."""
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_message(
    text: str,
    sender: str = "user",
    summary: str | None = None,
) -> dict[str, Any]:
    """Construct a well-formed inbox message with a fresh messageId."""
    body = (text or "").strip()
    if not body:
        raise InboxError("Message text must not be empty")
    derived_summary = (summary or "").strip()
    if not derived_summary:
        # First line, trimmed to a sensible length.
        lines = body.splitlines()
        first_line = lines[0] if lines else body
        derived_summary = first_line[:120]
    return {
        "from": sender or "user",
        "text": body,
        "summary": derived_summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "messageId": uuid.uuid4().hex,
        "read": False,
    }


def enqueue(
    spec_dir: Path,
    text: str,
    recipient: str = DEFAULT_RECIPIENT,
    sender: str = "user",
    summary: str | None = None,
) -> dict[str, Any]:
    """Enqueue a message to a recipient's inbox with delivery verification.

    Performs an atomic, locked read-modify-write, then re-reads the file from
    disk and confirms the new messageId is present (delivery proof).

    Returns a dict::

        {
            "messageId": str,
            "delivered": bool,    # True iff read-back verification passed
            "recipient": str,
            "message": {...},     # the stored message object
        }
    """
    inbox_path = get_inbox_path(spec_dir, recipient)
    message = build_message(text=text, sender=sender, summary=summary)

    with _InboxLock(inbox_path):
        messages = _read_messages(inbox_path)
        messages.append(message)
        _write_messages_atomic(inbox_path, messages)

        # Delivery proof: re-read from disk and confirm our id landed.
        persisted = _read_messages(inbox_path)
        delivered = any(m.get("messageId") == message["messageId"] for m in persisted)

    if not delivered:
        raise DeliveryVerificationError(
            f"Message {message['messageId']} not found after write to {inbox_path}"
        )

    return {
        "messageId": message["messageId"],
        "delivered": delivered,
        "recipient": _sanitize_recipient(recipient),
        "message": message,
    }


def list_messages(
    spec_dir: Path,
    recipient: str = DEFAULT_RECIPIENT,
    unread_only: bool = False,
) -> list[dict[str, Any]]:
    """List messages for a recipient, optionally only unread ones."""
    inbox_path = get_inbox_path(spec_dir, recipient)
    messages = _read_messages(inbox_path)
    if unread_only:
        return [m for m in messages if not m.get("read", False)]
    return messages


def read_pending(
    spec_dir: Path,
    recipient: str = DEFAULT_RECIPIENT,
) -> list[dict[str, Any]]:
    """Return all unread messages for a recipient (does not mark them read)."""
    return list_messages(spec_dir, recipient, unread_only=True)


def mark_read(
    spec_dir: Path,
    message_ids: list[str],
    recipient: str = DEFAULT_RECIPIENT,
) -> int:
    """Mark the given message ids as read. Returns the number updated."""
    if not message_ids:
        return 0
    target_ids = set(message_ids)
    inbox_path = get_inbox_path(spec_dir, recipient)

    with _InboxLock(inbox_path):
        messages = _read_messages(inbox_path)
        updated = 0
        for m in messages:
            if m.get("messageId") in target_ids and not m.get("read", False):
                m["read"] = True
                updated += 1
        if updated:
            _write_messages_atomic(inbox_path, messages)
    return updated


def drain_unread(
    spec_dir: Path,
    recipient: str = DEFAULT_RECIPIENT,
) -> list[dict[str, Any]]:
    """Atomically fetch all unread messages AND mark them read in one cycle.

    This is the consumption-hook primitive: an agent calls this between turns
    to pick up everything pending exactly once. Returns the messages that were
    transitioned from unread to read (so they are delivered exactly once).
    """
    inbox_path = get_inbox_path(spec_dir, recipient)
    with _InboxLock(inbox_path):
        messages = _read_messages(inbox_path)
        unread = [m for m in messages if not m.get("read", False)]
        if unread:
            for m in unread:
                m["read"] = True
            _write_messages_atomic(inbox_path, messages)
    return unread
