"""Copilot cloud-agent dispatch service.

Assigns a GitHub issue to ``copilot-swe-agent[bot]`` and polls for the
resulting PR.  Opt-in via ``AIFACTORY_COPILOT_DISPATCH_ENABLED=true``.

Lifecycle:
    1. ``dispatch()``  — PATCH issue assignees → ``copilot_running``
    2. ``find_copilot_pr()`` — background poller (every 60 s, up to 59 min)
       → ``copilot_pr_opened`` on match, ``failed`` on timeout

Integrates with ``delegation_tracker.COPILOT_PR_AUTHORS`` so the existing
auto-fix scan also picks up PRs opened via this service.

If dispatch is disabled or the gh CLI call fails, the caller should fall
back to the normal AIFactory coder pipeline and log a warning.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# GitHub's hard limit on a Copilot cloud-agent session is 59 minutes.
# Polling beyond that is pointless.
_POLL_INTERVAL_SECONDS = 60
_MAX_POLL_MINUTES = 59

# The exact login string used by the Copilot cloud agent.  Note: GitHub's
# REST API returns `user.login == "copilot-swe-agent"` (no [bot] suffix) with
# `user.type == "Bot"`.  The [bot] suffix only appears in the display name.
AGENT_HANDLE = "copilot-swe-agent[bot]"
DISPATCH_LABEL = "copilot:delegate"


def is_dispatch_enabled() -> bool:
    """Return True when Copilot dispatch is opted in via env var."""
    return os.environ.get("AIFACTORY_COPILOT_DISPATCH_ENABLED", "").lower() in (
        "1",
        "true",
        "yes",
    )


class CopilotDispatchService:
    """Service to delegate tasks to the GitHub Copilot cloud agent."""

    def dispatch(self, repo_full_name: str, issue_number: int) -> dict:
        """Assign issue to the Copilot cloud agent via gh CLI.

        Returns a metadata dict to be stored under ``copilot_dispatch`` in
        ``task_metadata.json``.  Raises ``RuntimeError`` on gh CLI failure so
        the caller can fall back to the local pipeline.
        """
        result = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "PATCH",
                f"/repos/{repo_full_name}/issues/{issue_number}",
                "-f",
                f"assignees[]={AGENT_HANDLE}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"[copilot-dispatch] gh api PATCH failed: {result.stderr.strip()}"
            )
        dispatched_at = datetime.now(UTC).isoformat()
        logger.info(
            "[copilot-dispatch] assigned issue #%d in %s to %s",
            issue_number,
            repo_full_name,
            AGENT_HANDLE,
        )
        return {
            "enabled": True,
            "dispatched_at": dispatched_at,
            "issue_number": issue_number,
            "pr_number": None,
            "pr_url": None,
            "agent_handle": AGENT_HANDLE,
            "reviewed": False,
        }

    def find_copilot_pr(self, repo_full_name: str, issue_number: int) -> int | None:
        """Return the PR number opened by the Copilot agent for this issue, or None.

        Uses a jq filter against the GitHub REST API.  The filter matches PRs
        where ``user.type == "Bot"`` and the login starts with
        ``"copilot-swe-agent"`` (the actual REST API value; see module note)
        and the body contains the issue reference.
        """
        jq_filter = (
            f'[.[] | select(.user.type == "Bot" and '
            f'(.user.login | startswith("copilot-swe-agent")) and '
            f'(.body // "" | contains("#{issue_number}")))] '
            f"| first | .number"
        )
        result = subprocess.run(
            [
                "gh",
                "api",
                f"/repos/{repo_full_name}/pulls",
                "--jq",
                jq_filter,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "[copilot-dispatch] find_copilot_pr failed repo=%s issue=%d err=%s",
                repo_full_name,
                issue_number,
                result.stderr.strip(),
            )
            return None
        raw = result.stdout.strip()
        if raw and raw != "null":
            try:
                return int(raw)
            except ValueError:
                pass
        return None

    def get_pr_url(self, repo_full_name: str, pr_number: int) -> str | None:
        """Fetch the HTML URL for a PR number."""
        result = subprocess.run(
            [
                "gh",
                "api",
                f"/repos/{repo_full_name}/pulls/{pr_number}",
                "--jq",
                ".html_url",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw = result.stdout.strip()
        return raw if raw and raw != "null" else None


async def watch_for_copilot_pr(
    task_id: str,
    repo_full_name: str,
    issue_number: int,
    task_metadata_path: Path,
    on_pr_opened: Callable[[int, str | None], None] | None = None,
    on_timeout: Callable[[], None] | None = None,
) -> None:
    """Background coroutine: poll for the Copilot PR until found or timeout.

    Runs for at most ``_MAX_POLL_MINUTES`` minutes, polling every
    ``_POLL_INTERVAL_SECONDS`` seconds.  Calls ``on_pr_opened(pr_number, pr_url)``
    when the PR appears, or ``on_timeout()`` after the deadline passes.

    This is designed to be launched with ``asyncio.create_task()`` and does
    not raise — all errors are logged.
    """
    service = CopilotDispatchService()
    deadline_polls = (_MAX_POLL_MINUTES * 60) // _POLL_INTERVAL_SECONDS

    for _ in range(deadline_polls):
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        try:
            pr_number = await asyncio.to_thread(
                service.find_copilot_pr, repo_full_name, issue_number
            )
        except Exception as exc:
            logger.warning("[copilot-dispatch] watch error task=%s: %s", task_id, exc)
            continue

        if pr_number is not None:
            pr_url = None
            try:
                pr_url = await asyncio.to_thread(
                    service.get_pr_url, repo_full_name, pr_number
                )
            except Exception:
                pass

            logger.info(
                "[copilot-dispatch] PR #%d found for task %s issue #%d",
                pr_number,
                task_id,
                issue_number,
            )
            _update_task_metadata(
                task_metadata_path,
                {"pr_number": pr_number, "pr_url": pr_url},
            )
            if on_pr_opened is not None:
                try:
                    on_pr_opened(pr_number, pr_url)
                except Exception as exc:
                    logger.warning(
                        "[copilot-dispatch] on_pr_opened callback error: %s", exc
                    )
            return

    # Deadline reached — no PR found.
    logger.warning(
        "[copilot-dispatch] no Copilot PR after %d minutes for task=%s issue=#%d — marking failed",
        _MAX_POLL_MINUTES,
        task_id,
        issue_number,
    )
    if on_timeout is not None:
        try:
            on_timeout()
        except Exception as exc:
            logger.warning("[copilot-dispatch] on_timeout callback error: %s", exc)


def _update_task_metadata(path: Path, updates: dict) -> None:
    """Merge ``updates`` into the ``copilot_dispatch`` block of ``task_metadata.json``."""
    if not path.exists():
        return
    try:
        import json

        meta = json.loads(path.read_text())
        dispatch = meta.setdefault("copilot_dispatch", {})
        dispatch.update(updates)
        path.write_text(json.dumps(meta, indent=2))
    except Exception as exc:
        logger.warning(
            "[copilot-dispatch] metadata update failed path=%s err=%s", path, exc
        )
