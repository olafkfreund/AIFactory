"""
Worktree external-tool routes (open in IDE / terminal, detect installed tools).

Extracted verbatim from ``routes/tasks.py`` (issue #556) as a behavior-preserving
sub-router. These endpoints are mounted onto the tasks router via
``include_router`` so the public paths and request/response shapes are unchanged:

    POST /worktree/open-in-ide
    POST /worktree/open-in-terminal
    POST /worktree/detect-tools

The handlers depend only on the standard library (``subprocess``, ``pathlib``,
``platform``, ``shutil``) and ``pydantic`` -- no database, app state, or private
helpers from ``tasks.py`` -- which is why this cluster is the lowest-risk slice
of the god-file to lift out first.
"""

import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from server.error_ref import client_error
from server.services.argv_safety import assert_not_option

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================
# Worktree Open in IDE/Terminal Routes
# ============================================


CUSTOM_PATH_DISABLED = (
    "customPath is no longer accepted: it let the request body choose the "
    "program the server executes, outside the agent sandbox, for any token "
    "holder. Use the `ide`/`terminal` name instead. See issue #1267 for the "
    "operator-side allowlist that would replace it."
)


class OpenInIDERequest(BaseModel):
    """Request body for opening a path in IDE."""

    worktreePath: str
    ide: str
    # Kept in the model ONLY so a client that still sends it gets a 400 that
    # says why. Dropping the field instead would make pydantic discard it and
    # the server would launch a different program than the caller asked for,
    # reporting success -- a silent substitution is a worse bug than the
    # refusal (#1267).
    customPath: str | None = Field(default=None, description=CUSTOM_PATH_DISABLED)


class OpenInTerminalRequest(BaseModel):
    """Request body for opening a path in terminal."""

    worktreePath: str
    terminal: str
    customPath: str | None = Field(default=None, description=CUSTOM_PATH_DISABLED)


def reject_custom_path(custom_path: str | None) -> None:
    """Raise 400 if the caller tried to name its own launcher binary."""
    if custom_path:
        raise HTTPException(status_code=400, detail=CUSTOM_PATH_DISABLED)


def resolve_launch_dir(worktree_path: str) -> tuple[str, dict[str, object] | None]:
    """Return (canonical directory, error) for a launch target.

    Resolving first means the string that reaches argv is an absolute path, so
    it cannot be read as an option, and the assertion states that rather than
    leaving it implied. Requiring a *directory* (not merely an existing path)
    is what makes ``cwd=`` a valid substitute for the ``cd <path>`` fragments
    this module used to build.
    """
    try:
        resolved = Path(worktree_path).resolve()
        if not resolved.is_dir():
            raise ValueError(f"Path does not exist: {worktree_path}")
        return assert_not_option(str(resolved), "worktreePath"), None
    except ValueError as exc:
        return "", {
            "success": False,
            "error": client_error(logger, "resolve launch dir failed", exc),
        }


def get_ide_command(ide: str, path: str) -> list[str]:
    """Get the command to open a path in the specified IDE.

    The program is always chosen from the table below, never from the request.
    A caller-supplied launcher path (the old ``customPath``) is remote code
    execution: the server runs whatever binary the body names, outside the
    agent sandbox, for any token holder. Validating that string cannot fix it,
    because the caller still picks the program (#1267).
    """
    import platform

    system = platform.system()

    # IDE command mappings
    ide_commands = {
        # VS Code family
        "vscode": ["code", path],
        "cursor": ["cursor", path],
        "vscodium": ["codium", path],
        "vscode-insiders": ["code-insiders", path],
        # JetBrains IDEs
        "webstorm": ["webstorm", path]
        if system != "Darwin"
        else ["open", "-a", "WebStorm", path],
        "intellij": ["idea", path]
        if system != "Darwin"
        else ["open", "-a", "IntelliJ IDEA", path],
        "pycharm": ["pycharm", path]
        if system != "Darwin"
        else ["open", "-a", "PyCharm", path],
        "phpstorm": ["phpstorm", path]
        if system != "Darwin"
        else ["open", "-a", "PhpStorm", path],
        "goland": ["goland", path]
        if system != "Darwin"
        else ["open", "-a", "GoLand", path],
        "rider": ["rider", path]
        if system != "Darwin"
        else ["open", "-a", "Rider", path],
        "clion": ["clion", path]
        if system != "Darwin"
        else ["open", "-a", "CLion", path],
        "rubymine": ["rubymine", path]
        if system != "Darwin"
        else ["open", "-a", "RubyMine", path],
        "datagrip": ["datagrip", path]
        if system != "Darwin"
        else ["open", "-a", "DataGrip", path],
        # Sublime Text
        "sublime": ["subl", path]
        if system != "Darwin"
        else ["open", "-a", "Sublime Text", path],
        # Atom / Pulsar
        "atom": ["atom", path],
        "pulsar": ["pulsar", path],
        # Vim/Neovim (terminal-based)
        "vim": ["vim", path],
        "neovim": ["nvim", path],
        "nvim": ["nvim", path],
        # Emacs
        "emacs": ["emacs", path],
        # Zed
        "zed": ["zed", path] if system != "Darwin" else ["open", "-a", "Zed", path],
        # Nova (macOS)
        "nova": ["open", "-a", "Nova", path],
        # BBEdit (macOS)
        "bbedit": ["open", "-a", "BBEdit", path],
        # TextMate (macOS)
        "textmate": ["open", "-a", "TextMate", path],
        # Notepad++ (Windows)
        "notepadpp": ["notepad++", path],
        # Visual Studio (Windows)
        "visualstudio": ["devenv", path],
        # Fleet
        "fleet": ["fleet", path],
        # Lapce
        "lapce": ["lapce", path],
        # Helix
        "helix": ["hx", path],
        # Kate (Linux/KDE)
        "kate": ["kate", path],
        # Geany (Linux)
        "geany": ["geany", path],
    }

    return ide_commands.get(ide, ["code", path])  # Default to VS Code


def get_terminal_command(terminal: str, path: str) -> list[str]:
    """Get the command to open a terminal at the specified path.

    As with ``get_ide_command``, the program comes from the table, never from
    the request body (#1267). Terminals that used to be handed a ``cd <path>``
    shell fragment are now launched with no path argument at all -- the caller
    passes ``cwd=path`` to ``Popen``, which starts the shell in that directory
    without any string ever being interpreted by a shell.
    """
    import platform

    system = platform.system()

    # Terminal command mappings by platform
    if system == "Darwin":  # macOS
        terminal_commands = {
            "system": ["open", "-a", "Terminal", path],
            "terminal": ["open", "-a", "Terminal", path],
            "iterm2": ["open", "-a", "iTerm", path],
            "iterm": ["open", "-a", "iTerm", path],
            "warp": ["open", "-a", "Warp", path],
            "hyper": ["open", "-a", "Hyper", path],
            "kitty": ["kitty", "--directory", path],
            "alacritty": ["alacritty", "--working-directory", path],
            "wezterm": ["wezterm", "start", "--cwd", path],
            "tabby": ["open", "-a", "Tabby", path],
        }
    elif system == "Windows":
        terminal_commands = {
            # cwd= handles the directory; no `cd <path>` fragment to quote.
            "system": ["cmd", "/c", "start", "cmd"],
            "wt": ["wt", "-d", path],
            "windows-terminal": ["wt", "-d", path],
            "cmd": ["cmd", "/c", "start", "cmd"],
            "powershell": ["powershell", "-NoExit"],
            "pwsh": ["pwsh", "-NoExit"],
            "hyper": ["hyper", path],
            "alacritty": ["alacritty", "--working-directory", path],
            "wezterm": ["wezterm", "start", "--cwd", path],
            "kitty": ["kitty", "--directory", path],
            "cmder": ["cmder", "/START", path],
            "conemu": ["conemu", "-Dir", path],
        }
    else:  # Linux and others
        terminal_commands = {
            # `-e "cd <path> && $SHELL"` was a shell string built from request
            # data; cwd= does the same job with nothing to inject into.
            "system": ["x-terminal-emulator"],
            "gnome-terminal": ["gnome-terminal", f"--working-directory={path}"],
            "konsole": ["konsole", f"--workdir={path}"],
            "xfce4-terminal": ["xfce4-terminal", f"--working-directory={path}"],
            "terminator": ["terminator", f"--working-directory={path}"],
            "tilix": ["tilix", f"--working-directory={path}"],
            "kitty": ["kitty", "--directory", path],
            "alacritty": ["alacritty", "--working-directory", path],
            "wezterm": ["wezterm", "start", "--cwd", path],
            "hyper": ["hyper", path],
            "xterm": ["xterm"],
            "urxvt": ["urxvt", "-cd", path],
            "st": ["st", "-d", path],
            "foot": ["foot", f"--working-directory={path}"],
            "sakura": ["sakura", f"--working-directory={path}"],
            "tabby": ["tabby", path],
        }

    return terminal_commands.get(terminal, terminal_commands.get("system", ["xterm"]))


@router.post("/worktree/open-in-ide")
async def open_worktree_in_ide(request: OpenInIDERequest):
    """
    Open a worktree path in the specified IDE.
    Used by the web UI to launch external IDE applications.
    """
    reject_custom_path(request.customPath)
    ide = request.ide

    worktree_path, error = resolve_launch_dir(request.worktreePath)
    if error:
        return error

    try:
        cmd = get_ide_command(ide, worktree_path)

        # Launch the IDE (don't wait for it to finish)
        subprocess.Popen(
            cmd,
            cwd=worktree_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        return {"success": True, "data": {"opened": True}}
    except FileNotFoundError:
        return {
            "success": False,
            "error": f"IDE command not found. Make sure '{ide}' is installed and in your PATH.",
        }
    except Exception as e:
        return {
            "success": False,
            "error": client_error(logger, "Failed to open IDE", e),
        }


@router.post("/worktree/open-in-terminal")
async def open_worktree_in_terminal(request: OpenInTerminalRequest):
    """
    Open a worktree path in the specified terminal emulator.
    Used by the web UI to launch external terminal applications.
    """
    reject_custom_path(request.customPath)
    terminal = request.terminal

    worktree_path, error = resolve_launch_dir(request.worktreePath)
    if error:
        return error

    try:
        cmd = get_terminal_command(terminal, worktree_path)

        # Launch the terminal (don't wait for it to finish)
        subprocess.Popen(
            cmd,
            cwd=worktree_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        return {"success": True, "data": {"opened": True}}
    except FileNotFoundError:
        return {
            "success": False,
            "error": f"Terminal command not found. Make sure '{terminal}' is installed and in your PATH.",
        }
    except Exception as e:
        return {
            "success": False,
            "error": client_error(logger, "Failed to open terminal", e),
        }


@router.post("/worktree/detect-tools")
async def detect_worktree_tools():
    """
    Detect installed IDEs and terminal emulators on the system.
    Returns lists of available tools with their installation status.
    """
    import platform
    import shutil

    system = platform.system()

    # IDE detection
    ide_definitions = [
        {"id": "vscode", "name": "Visual Studio Code", "command": "code"},
        {"id": "cursor", "name": "Cursor", "command": "cursor"},
        {"id": "vscodium", "name": "VSCodium", "command": "codium"},
        {
            "id": "vscode-insiders",
            "name": "VS Code Insiders",
            "command": "code-insiders",
        },
        {"id": "sublime", "name": "Sublime Text", "command": "subl"},
        {
            "id": "webstorm",
            "name": "WebStorm",
            "command": "webstorm" if system != "Darwin" else None,
        },
        {
            "id": "intellij",
            "name": "IntelliJ IDEA",
            "command": "idea" if system != "Darwin" else None,
        },
        {
            "id": "pycharm",
            "name": "PyCharm",
            "command": "pycharm" if system != "Darwin" else None,
        },
        {"id": "zed", "name": "Zed", "command": "zed"},
        {"id": "atom", "name": "Atom", "command": "atom"},
        {"id": "pulsar", "name": "Pulsar", "command": "pulsar"},
        {"id": "vim", "name": "Vim", "command": "vim"},
        {"id": "neovim", "name": "Neovim", "command": "nvim"},
        {"id": "emacs", "name": "Emacs", "command": "emacs"},
        {"id": "helix", "name": "Helix", "command": "hx"},
        {"id": "fleet", "name": "Fleet", "command": "fleet"},
        {"id": "lapce", "name": "Lapce", "command": "lapce"},
    ]

    if system == "Windows":
        ide_definitions.extend(
            [
                {"id": "notepadpp", "name": "Notepad++", "command": "notepad++"},
                {"id": "visualstudio", "name": "Visual Studio", "command": "devenv"},
            ]
        )
    elif system == "Linux":
        ide_definitions.extend(
            [
                {"id": "kate", "name": "Kate", "command": "kate"},
                {"id": "geany", "name": "Geany", "command": "geany"},
            ]
        )

    # Terminal detection
    terminal_definitions = []
    if system == "Darwin":
        terminal_definitions = [
            {"id": "terminal", "name": "Terminal", "command": None, "app": "Terminal"},
            {"id": "iterm2", "name": "iTerm2", "command": None, "app": "iTerm"},
            {"id": "warp", "name": "Warp", "command": None, "app": "Warp"},
            {"id": "hyper", "name": "Hyper", "command": None, "app": "Hyper"},
            {"id": "kitty", "name": "Kitty", "command": "kitty"},
            {"id": "alacritty", "name": "Alacritty", "command": "alacritty"},
            {"id": "wezterm", "name": "WezTerm", "command": "wezterm"},
        ]
    elif system == "Windows":
        terminal_definitions = [
            {"id": "wt", "name": "Windows Terminal", "command": "wt"},
            {"id": "cmd", "name": "Command Prompt", "command": "cmd"},
            {"id": "powershell", "name": "PowerShell", "command": "powershell"},
            {"id": "pwsh", "name": "PowerShell Core", "command": "pwsh"},
            {"id": "hyper", "name": "Hyper", "command": "hyper"},
            {"id": "alacritty", "name": "Alacritty", "command": "alacritty"},
            {"id": "wezterm", "name": "WezTerm", "command": "wezterm"},
            {"id": "kitty", "name": "Kitty", "command": "kitty"},
        ]
    else:  # Linux
        terminal_definitions = [
            {
                "id": "gnome-terminal",
                "name": "GNOME Terminal",
                "command": "gnome-terminal",
            },
            {"id": "konsole", "name": "Konsole", "command": "konsole"},
            {
                "id": "xfce4-terminal",
                "name": "Xfce Terminal",
                "command": "xfce4-terminal",
            },
            {"id": "terminator", "name": "Terminator", "command": "terminator"},
            {"id": "tilix", "name": "Tilix", "command": "tilix"},
            {"id": "kitty", "name": "Kitty", "command": "kitty"},
            {"id": "alacritty", "name": "Alacritty", "command": "alacritty"},
            {"id": "wezterm", "name": "WezTerm", "command": "wezterm"},
            {"id": "hyper", "name": "Hyper", "command": "hyper"},
            {"id": "xterm", "name": "XTerm", "command": "xterm"},
            {"id": "foot", "name": "Foot", "command": "foot"},
        ]

    # Check which tools are installed
    ides = []
    for ide_def in ide_definitions:
        installed = False
        path = ""
        if ide_def.get("command"):
            found = shutil.which(ide_def["command"])
            if found:
                installed = True
                path = found
        ides.append(
            {
                "id": ide_def["id"],
                "name": ide_def["name"],
                "path": path,
                "installed": installed,
            }
        )

    terminals = []
    for term_def in terminal_definitions:
        installed = False
        path = ""
        if term_def.get("command"):
            found = shutil.which(term_def["command"])
            if found:
                installed = True
                path = found
        elif term_def.get("app") and system == "Darwin":
            # Check macOS applications
            app_path = f"/Applications/{term_def['app']}.app"
            if Path(app_path).exists():
                installed = True
                path = app_path
        terminals.append(
            {
                "id": term_def["id"],
                "name": term_def["name"],
                "path": path,
                "installed": installed,
            }
        )

    return {"success": True, "data": {"ides": ides, "terminals": terminals}}
