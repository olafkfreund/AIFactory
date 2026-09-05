"""Centralized path helpers for AIFactory data directory."""

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

AI_FACTORY_DIR = Path.home() / ".aifactory"


def migrate_legacy_data():
    """Safely migrate legacy AIFactory data folder to AIFactory."""
    legacy_dir = Path.home() / ".aifactory"
    if legacy_dir.exists() and not AI_FACTORY_DIR.exists():
        try:
            shutil.copytree(legacy_dir, AI_FACTORY_DIR, dirs_exist_ok=True)
            print(
                f"AIFactory - Successfully migrated legacy data from {legacy_dir} to {AI_FACTORY_DIR}"
            )
        except Exception as e:
            print(f"AIFactory - Warning: failed to migrate legacy data: {e}")


# Run migration automatically on module load
migrate_legacy_data()


def get_data_dir() -> Path:
    """Return the AIFactory data directory, creating it if needed."""
    AI_FACTORY_DIR.mkdir(parents=True, exist_ok=True)
    return AI_FACTORY_DIR


def get_data_file(filename: str) -> Path:
    """Get a file path in the AIFactory data directory."""
    return get_data_dir() / filename


def write_secret_file(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as a 0600 file, atomically and with no
    readable window.

    ``Path.write_text`` creates the file at the umask default (usually 0644) and
    only a *subsequent* ``chmod`` narrows it — so a secret written that way is
    world-readable for the duration of the write. ``tempfile.mkstemp`` opens the
    temp file 0600 from creation regardless of umask.

    The write goes to a temp file in the same directory and is published with
    ``os.replace`` so concurrent readers always see either the old or the new
    complete content, never a truncated or interleaved file. This matters more
    than it looks: ``write_text`` truncates in place, so a reader that lands
    mid-write gets invalid JSON, ``load_profiles`` swallows the
    ``JSONDecodeError`` and returns ``{"profiles": []}``, and the next save
    writes that belief back — destroying every profile and its token.
    ``os.replace`` also swaps the inode, so a file previously left at 0644 (by
    an older build, or restored from a backup) comes out 0600.

    Ported from the TFactory/PFactory implementations (Factory fork drift):
    AIFactory had neither helper and still used write_text + chmod.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        try:
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        Path(tmp).replace(path)  # atomic within a filesystem
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_secret_json(path: Path, data: Any) -> None:
    """Serialise ``data`` as JSON and write it via :func:`write_secret_file`.

    NOTE: this makes each write atomic; it does NOT serialise read-modify-write.
    Two concurrent handlers can still lose an update (last writer wins) — but the
    file is always valid, so a lost update costs one field, not every token.
    """
    # Serialise BEFORE touching the filesystem: an unserialisable payload must
    # not leave a half-written target or a .tmp dropping behind.
    text = json.dumps(data, indent=2)
    write_secret_file(path, text)
