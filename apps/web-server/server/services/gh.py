"""Run the GitHub CLI. A subprocess wrapper, nothing route-specific.

Lived in routes/github.py, which made any SERVICE needing gh import a ROUTE --
a services -> routes cycle that CodeQL flagged the moment services/approval.py
needed it. The next service to shell out to gh would have recreated it.

routes/github re-exports this name, so existing importers are unchanged.
"""

from __future__ import annotations

import subprocess
from typing import Any


def run_gh_command(args: list[str], cwd: str | None = None) -> dict[str, Any]:
    """Run a gh CLI command and return ``{"success": bool, ...}``.

    Never raises: a failure is data, so every caller handles one shape. The
    behaviour is unchanged from the version that lived in routes/github.py --
    only the lint suppressions carry their reasons now.
    """
    try:
        # S603/S607: the executable is the literal "gh" and there is no shell.
        # PLW1510: check=False is the point -- a non-zero exit is returned as
        # data, not raised, so callers get one shape for every outcome.
        result = subprocess.run(  # noqa: S603, PLW1510
            ["gh", *args],  # noqa: S607 - gh is resolved from PATH by design
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=30,
        )
        if result.returncode != 0:
            return {"success": False, "error": result.stderr.strip()}
        return {"success": True, "output": result.stdout.strip()}
    except FileNotFoundError:
        return {"success": False, "error": "GitHub CLI (gh) not installed"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Command timed out"}
    except OSError as exc:
        # Narrowed from a bare `except Exception`. Anything subprocess raises
        # that is not already handled above is an OSError; a broader catch here
        # would swallow programming errors as if the CLI had failed.
        return {"success": False, "error": str(exc)}
