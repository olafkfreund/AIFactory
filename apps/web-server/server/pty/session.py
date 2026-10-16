"""
PTY Session - Wraps a pseudo-terminal for WebSocket communication.

Each session represents one terminal instance that can be connected
to via WebSocket for bidirectional I/O.
"""

import asyncio
import contextlib
import os
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from server.utils.subprocess_env import pin_git_config

_HELPER_KEY = r"^credential\.(.+\.)?helper$"


def _user_credential_helpers(env: dict[str, str]) -> list[tuple[str, str]]:
    """The user's own system and global credential helpers, in git's order.

    Never reads repo config, so a helper an agent planted there stays reset.
    """
    out: list[tuple[str, str]] = []
    for scope in ("--system", "--global"):
        try:
            r = subprocess.run(  # noqa: S603
                ["git", "config", scope, "--get-regexp", _HELPER_KEY],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=5,
                env=env,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                key, _, value = line.partition(" ")
                out.append((key, value))
    return out


# Import ptyprocess for PTY handling (cross-platform)
try:
    from ptyprocess import PtyProcess, PtyProcessUnicode
except ImportError:
    PtyProcess = None
    PtyProcessUnicode = None


@dataclass
class PTYSession:
    """A single PTY (pseudo-terminal) session."""

    id: str = field(default_factory=lambda: str(uuid4()))
    cwd: str = field(default_factory=lambda: str(Path.home()))
    shell: str = field(default_factory=lambda: os.environ.get("SHELL", "/bin/bash"))
    cols: int = 80
    rows: int = 24
    env: dict[str, str] | None = None
    created_at: datetime = field(default_factory=datetime.now)

    # Internal state
    _pty: Optional["PtyProcessUnicode"] = field(default=None, repr=False)
    _output_buffer: str = field(default="", repr=False)
    _closed: bool = field(default=False, repr=False)

    def __post_init__(self):
        if PtyProcess is None:
            raise RuntimeError(
                "ptyprocess package is required. Install with: pip install ptyprocess"
            )

    def start(self) -> None:
        """Start the PTY process."""
        if self._pty is not None:
            raise RuntimeError("PTY already started")

        # Prepare environment
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        # Remove CLAUDECODE to allow launching claude CLI from the terminal
        env.pop("CLAUDECODE", None)
        if self.env:
            env.update(self.env)
        # Agent worktrees share .git/config: pin git's exec keys, but keep the
        # user's editor, pager and own credential helpers (#1745).
        pin_git_config(
            env,
            editor=env.get("VISUAL") or env.get("EDITOR") or "vi",
            pager=env.get("PAGER") or "less",
        )
        n = int(env["GIT_CONFIG_COUNT"])
        for i, (key, value) in enumerate(_user_credential_helpers(env)):
            env[f"GIT_CONFIG_KEY_{n + i}"] = key
            env[f"GIT_CONFIG_VALUE_{n + i}"] = value
            env["GIT_CONFIG_COUNT"] = str(n + i + 1)

        # Start the PTY process
        self._pty = PtyProcessUnicode.spawn(
            [self.shell, "-l"],  # Login shell
            dimensions=(self.rows, self.cols),
            cwd=self.cwd,
            env=env,
        )
        self._closed = False

    def write(self, data: str) -> None:
        """Write data to the PTY (user input)."""
        if self._pty is None or self._closed:
            raise RuntimeError("PTY not running")
        self._pty.write(data)

    def read(self, size: int = 4096) -> str:
        """Read data from the PTY (terminal output).

        This is a blocking call - use read_async for async contexts.
        Returns empty string if no data available.
        """
        if self._pty is None or self._closed:
            return ""

        try:
            # Non-blocking read
            import select

            if select.select([self._pty.fd], [], [], 0)[0]:
                return self._pty.read(size)
            return ""
        except EOFError:
            self._closed = True
            return ""
        except Exception:
            return ""

    async def read_async(self, size: int = 4096) -> str:
        """Async version of read - runs blocking read in executor."""
        if self._pty is None or self._closed:
            return ""

        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._pty.read, size)
        except EOFError:
            self._closed = True
            return ""
        except Exception:
            return ""

    def resize(self, cols: int, rows: int) -> None:
        """Resize the terminal."""
        if self._pty is None or self._closed:
            return

        self.cols = cols
        self.rows = rows
        self._pty.setwinsize(rows, cols)

    def is_alive(self) -> bool:
        """Check if the PTY process is still running."""
        if self._pty is None:
            return False
        return self._pty.isalive()

    def close(self) -> None:
        """Close the PTY session."""
        if self._pty is None:
            return

        self._closed = True

        # Best-effort: the process may already have exited between the
        # isalive() check and terminate().
        with contextlib.suppress(Exception):
            if self._pty.isalive():
                self._pty.terminate(force=True)

        self._pty = None

    def send_signal(self, sig: int) -> None:
        """Send a signal to the PTY process."""
        if self._pty is None or self._closed:
            return

        # Best-effort: the process may already have exited.
        with contextlib.suppress(Exception):
            self._pty.kill(sig)

    def interrupt(self) -> None:
        """Send Ctrl+C (SIGINT) to the PTY."""
        self.send_signal(signal.SIGINT)

    def to_dict(self) -> dict:
        """Serialize session to dictionary."""
        return {
            "id": self.id,
            "cwd": self.cwd,
            "shell": self.shell,
            "cols": self.cols,
            "rows": self.rows,
            "created_at": self.created_at.isoformat(),
            "is_alive": self.is_alive(),
        }


async def create_pty_reader(
    session: PTYSession,
    on_output: Callable[[str], None],
    check_interval: float = 0.01,
) -> None:
    """Continuously read from PTY and call callback with output.

    This runs until the session is closed or the PTY exits.
    """
    while session.is_alive() and not session._closed:
        try:
            output = await session.read_async(4096)
            if output:
                if asyncio.iscoroutinefunction(on_output):
                    await on_output(output)
                else:
                    on_output(output)
            else:
                # No data, wait a bit before trying again
                await asyncio.sleep(check_interval)
        except Exception:
            break
