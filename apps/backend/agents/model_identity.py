"""Durable model identities for sessions that can change task code."""

import fcntl
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def _path(source_spec_dir: Path) -> Path:
    return Path(source_spec_dir) / "model_identities.json"


def read_identities(source_spec_dir: Path) -> list[dict[str, Any]]:
    """Read recorded sessions; a missing file means no sessions yet."""
    path = _path(source_spec_dir)
    if not path.exists():
        return []
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"Invalid model identities: {path}")
    return entries


def record_identity(  # noqa: PLR0913 - persisted identity needs all session coordinates
    source_spec_dir: Path,
    *,
    phase: str,
    subtask_id: str | None,
    model: str,
    provider: str,
    served_model: str | None = None,
) -> None:
    """Append one session identity without losing concurrent workers."""
    path = _path(source_spec_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, (path.parent / "model_identities.json.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = read_identities(source_spec_dir)
        entries.append(
            {
                "phase": phase,
                "subtask_id": subtask_id,
                "model": model,
                "provider": provider,
                "served_model": served_model,
            }
        )
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(entries, stream)
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(path)
        finally:
            Path(temporary).unlink(missing_ok=True)
