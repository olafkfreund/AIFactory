"""Private HOME for an agent CLI, with guarded login refresh write-back."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import subprocess
import tempfile
import weakref
from pathlib import Path

from core.child_env import child_env

logger = logging.getLogger(__name__)

_BASES = {
    "CLAUDE_CONFIG_DIR": ".claude",
    "CODEX_HOME": ".codex",
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_CACHE_HOME": ".cache",
    "XDG_STATE_HOME": ".local/state",
}
_LOGINS = {
    "claude": (
        ("CLAUDE_CONFIG_DIR", ".credentials.json", True),
        ("CLAUDE_CONFIG_DIR", "settings.json", False),
    ),
    "codex": (("CODEX_HOME", "auth.json", True),),
    "copilot": (
        ("XDG_CONFIG_HOME", "github-copilot/apps.json", True),
        ("XDG_CONFIG_HOME", "github-copilot/hosts.json", True),
        ("", ".copilot/config.json", True),
    ),
    "gemini": (
        ("", ".gemini/oauth_creds.json", True),
        ("", ".gemini/google_accounts.json", True),
        ("", ".gemini/settings.json", False),
    ),
    "opencode": (("XDG_DATA_HOME", "opencode/auth.json", True),),
    "bash": (),
}


def _git_identity(home: Path) -> None:
    try:
        result = subprocess.run(
            [  # noqa: S607
                "git",
                "config",
                "--global",
                "--null",
                "--get-regexp",
                r"^(user\.(name|email)|safe\.directory)$",
            ],
            env=child_env(),
            timeout=10,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return
    if result.returncode:
        return
    lines = []
    for record in result.stdout.split(b"\0"):
        if b"\n" not in record:
            continue
        key, value = record.split(b"\n", 1)
        if b"\n" in value:
            continue
        section, name = key.decode().rsplit(".", 1)
        escaped = value.decode().replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'[{section}]\n\t{name} = "{escaped}"\n')
    if lines:
        path = home / ".gitconfig"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.writelines(lines)


def _cleanup(home: Path, entries: list[tuple[Path, Path, bytes, bool]]) -> None:
    try:
        for private, real, original, writeback in entries:
            try:
                if not writeback or not private.is_file():
                    continue
                changed = private.read_bytes()
                if changed == original:
                    continue
                json.loads(changed)
                fd = os.open(real.parent, os.O_RDONLY)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                    if real.read_bytes() != original:
                        logger.warning("%s changed meanwhile, kept", real)
                        continue
                    temp_fd, temp_name = tempfile.mkstemp(
                        dir=real.parent, prefix=".aif-"
                    )
                    try:
                        with os.fdopen(temp_fd, "wb") as file:
                            os.fchmod(file.fileno(), 0o600)
                            file.write(changed)
                        Path(temp_name).replace(real)
                    finally:
                        Path(temp_name).unlink(missing_ok=True)
                finally:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
            except Exception:  # noqa: BLE001 - a finalizer must never raise
                logger.warning(
                    "Could not write back agent login %s", real, exc_info=True
                )
    finally:
        shutil.rmtree(home, ignore_errors=True)


class AgentHome:
    def __init__(self, cli: str) -> None:
        if cli not in _LOGINS:
            raise ValueError(f"Unknown agent CLI: {cli}")
        home = Path(tempfile.mkdtemp(prefix="aif-home-"))
        entries: list[tuple[Path, Path, bytes, bool]] = []
        try:
            (home / "tmp").mkdir(mode=0o700)
            for base, rel, writeback in _LOGINS[cli]:
                real = (
                    Path(os.environ.get(base) or Path.home() / _BASES[base]) / rel
                    if base
                    else Path.home() / rel
                )
                if not real.is_file():
                    continue
                private = home / _BASES[base] / rel if base else home / rel
                original = real.read_bytes()
                private.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as file:
                    file.write(original)
                entries.append((private, real, original, writeback))
            _git_identity(home)
        except Exception:
            shutil.rmtree(home, ignore_errors=True)
            raise
        self.env = {
            "HOME": str(home),
            "TMPDIR": str(home / "tmp"),
            **{var: str(home / rel) for var, rel in _BASES.items()},
        }
        self._finalizer = weakref.finalize(self, _cleanup, home, entries)

    def close(self) -> None:
        self._finalizer()

    def __enter__(self) -> AgentHome:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
