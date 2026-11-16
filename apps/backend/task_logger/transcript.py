"""Best-effort, redacted JSONL transcript for one process run."""

import json
import math
import os
import re
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from factory_common.secrets import redact

MAX_BYTES = 64 * 1024 * 1024
MIN_SECRET_LENGTH = 8
_SECRET_NAMES = ("TOKEN", "KEY", "SECRET", "PASSWORD")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<type>(?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY)-----"
    r".*?-----END (?P=type)-----",
    re.DOTALL,
)
_LOCK = threading.RLock()


class _State:
    path: Path | None = None
    stopped = False
    tools: ClassVar[dict[str, str]] = {}


def _clean(value: object, secrets: list[str]) -> object:
    if isinstance(value, str):
        value = _PRIVATE_KEY.sub("***REDACTED***", value)
        for secret in secrets:
            value = value.replace(secret, "***REDACTED***")
        return redact(value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {_clean(str(k), secrets): _clean(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(item, secrets) for item in value]
    return _clean(str(value), secrets)


def start(spec_dir: Path) -> None:
    """Fix this run's path without creating a file."""
    with suppress(Exception), _LOCK:
        name = f"{datetime.now(UTC):%Y%m%dT%H%M%S_%fZ}-{os.getpid():07d}.jsonl"
        _State.path = Path(spec_dir) / "transcripts" / name
        _State.stopped = False
        _State.tools.clear()


def record(kind: str, tool: str | None, payload: object, **meta: object) -> None:
    """Append one redacted event, stopping at the size cap."""
    with suppress(Exception), _LOCK:
        if _State.path is None or _State.stopped:
            return
        secrets = sorted(
            {
                value
                for name, value in os.environ.items()
                if any(word in name.upper() for word in _SECRET_NAMES)
                and len(value) >= MIN_SECRET_LENGTH
            },
            key=len,
            reverse=True,
        )
        now = datetime.now(UTC).isoformat()
        entry = {
            "ts": now,
            "kind": kind,
            "tool": tool,
            "tool_use_id": meta.get("tool_use_id"),
            "phase": meta.get("phase"),
            "subtask_id": meta.get("subtask_id"),
            "session": meta.get("session"),
            "success": meta.get("success"),
            "payload": payload,
        }
        clean = _clean(entry, secrets)
        line = (json.dumps(clean, ensure_ascii=False) + "\n").encode("utf-8")
        marker_entry = _clean({**entry, "kind": "truncated", "payload": None}, secrets)
        marker = (json.dumps(marker_entry, ensure_ascii=False) + "\n").encode("utf-8")
        size = _State.path.stat().st_size if _State.path.exists() else 0
        _State.path.parent.mkdir(parents=True, exist_ok=True)
        with _State.path.open("ab") as output:
            if size + len(line) + len(marker) > MAX_BYTES:
                if size + len(marker) <= MAX_BYTES:
                    output.write(marker)
                _State.stopped = True
            else:
                output.write(line)


def tool_start(name: str, payload: object, tool_use_id: str, **meta: object) -> None:
    """Remember the tool name for its result."""
    with suppress(Exception), _LOCK:
        if _State.path is None or _State.stopped:
            return
        _State.tools[tool_use_id] = name
        record("tool_start", name, payload, tool_use_id=tool_use_id, **meta)


def tool_end(tool_use_id: str, payload: object, is_error: bool, **meta: object) -> None:
    """Record the result with the matching tool name."""
    with suppress(Exception), _LOCK:
        if _State.path is None or _State.stopped:
            return
        name = _State.tools.pop(tool_use_id, None)
        record(
            "tool_end",
            name,
            payload,
            tool_use_id=tool_use_id,
            success=not is_error,
            **meta,
        )


def written_files() -> list[Path]:
    """Return the transcript only after at least one line was written."""
    with suppress(Exception), _LOCK:
        path = _State.path
        return [path] if path is not None and path.is_file() else []
    return []


def reset() -> None:
    """Clear process state for tests."""
    with suppress(Exception), _LOCK:
        _State.path = None
        _State.stopped = False
        _State.tools.clear()
