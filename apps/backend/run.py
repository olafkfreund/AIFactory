#!/usr/bin/env python3
"""
Magestic AI Framework
=====================

A multi-session autonomous coding framework for building features and applications.
Uses subtask-based implementation plans with phase dependencies.

Key Features:
- Safe workspace isolation (builds in separate workspace by default)
- Parallel execution with Git worktrees
- Smart recovery from interruptions
- GitHub integration

Usage:
    python aifactory/run.py --spec 001-initial-app
    python aifactory/run.py --spec 001
    python aifactory/run.py --list

    # Workspace management
    python aifactory/run.py --spec 001 --merge     # Add completed build to project
    python aifactory/run.py --spec 001 --review    # See what was built
    python aifactory/run.py --spec 001 --discard   # Delete build (requires confirmation)

Prerequisites:
    - CLAUDE_CODE_OAUTH_TOKEN environment variable set (run: claude setup-token)
    - Spec created via: claude /spec
    - Claude Code CLI installed
"""

import sys

# Python version check - must be before any imports using 3.10+ syntax
if sys.version_info < (3, 10):  # noqa: UP036
    sys.exit(
        f"Error: Magestic AI requires Python 3.10 or higher.\n"
        f"You are running Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n"
        f"\n"
        f"Please upgrade Python: https://www.python.org/downloads/"
    )

import contextlib
import io

# Configure safe encoding on Windows BEFORE any imports that might print
# This handles both TTY and piped output (e.g., from Electron)
if sys.platform == "win32":
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name)
        # Method 1: Try reconfigure (works for TTY)
        _reconfigured = False
        if hasattr(_stream, "reconfigure"):
            with contextlib.suppress(AttributeError, io.UnsupportedOperation, OSError):
                _stream.reconfigure(encoding="utf-8", errors="replace")
                _reconfigured = True
        if _reconfigured:
            continue
        # Method 2: Wrap with TextIOWrapper for piped output. Best-effort —
        # if this also fails, the stream just keeps its default encoding;
        # logging isn't configured yet this early in bootstrap.
        with contextlib.suppress(AttributeError, io.UnsupportedOperation, OSError):
            if hasattr(_stream, "buffer"):
                _new_stream = io.TextIOWrapper(
                    _stream.buffer,
                    encoding="utf-8",
                    errors="replace",
                    line_buffering=True,
                )
                setattr(sys, _stream_name, _new_stream)
    # Clean up temporary variables
    del _stream_name, _stream
    if "_new_stream" in dir():
        del _new_stream

from cli import main

if __name__ == "__main__":
    main()
