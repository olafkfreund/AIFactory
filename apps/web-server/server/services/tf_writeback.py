"""Recognize a TFactory test write-back directly on a verified build head."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.utils.subprocess_env import child_env, project_github_env

_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?\Z"
)
_TEST_FILE = re.compile(r"(?:test_.*\.py|.*_test\.(?:py|go)|.*\.(?:test|spec)\.(?:js|jsx|ts|tsx))\Z")


def _is_test_path(path: str) -> bool:
    parts = path.split("/")
    return bool(
        path
        and ".github" not in parts
        and (
            any(part in {"tests", "test", "__tests__", "spec"} for part in parts[:-1])
            or _TEST_FILE.fullmatch(parts[-1])
        )
    )


def _github_repo(project_dir: Path) -> tuple[str, str] | None:
    result = subprocess.run(  # noqa: S603 - fixed git command and trusted project path
        ["git", "-C", str(project_dir), "remote", "get-url", "origin"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    match = _REMOTE.fullmatch(result.stdout.strip()) if result.returncode == 0 else None
    return (match.group(1), match.group(2)) if match else None


def _gh_commit(
    owner: str, repo: str, sha: str, cwd: Path
) -> tuple[list[str], list[str]] | None:
    with project_github_env(cwd, child_env()) as env:
        result = subprocess.run(  # noqa: S603 - validated repo and SHA, fixed gh API path
            ["gh", "api", f"repos/{owner}/{repo}/commits/{sha}"],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
    if result.returncode != 0:
        return None
    doc = json.loads(result.stdout)
    if not isinstance(doc, dict) or not isinstance(doc.get("parents"), list) or not isinstance(doc.get("files"), list):
        return None
    if not all(isinstance(p, dict) for p in doc["parents"]) or not all(
        isinstance(item, dict) and isinstance(item.get("filename"), str)
        for item in doc["files"]
    ):
        return None
    parents = [p.get("sha") for p in doc["parents"]]
    files = [
        name
        for item in doc["files"]
        for name in (item["filename"], item.get("previous_filename"))
        if name is not None
    ]
    if not all(isinstance(value, str) for value in [*parents, *files]):
        return None
    return parents, files


def writeback_base(
    spec_dir: Path,
    head: str,
    *,
    getter: Callable[[str, dict[str, str]], tuple[int, object]] | None = None,
    reader: Callable[[str], tuple[list[str], list[str]] | None] | None = None,
) -> str | None:
    """Return the verified parent only for TFactory's own test-only commit."""
    try:
        if not isinstance(head, str) or not _SHA.fullmatch(head):
            return None
        from pfactory.tfactory_client import (  # noqa: PLC0415
            _project_dir,
            tfactory_task,
        )

        spec_dir = Path(spec_dir)
        task: dict[str, Any] | None = tfactory_task(spec_dir, spec_dir.name, getter=getter)
        if not isinstance(task, dict):
            return None
        status = task.get("status_json")
        if not isinstance(status, dict) or status.get("status") != "triaged":
            return None
        source = task.get("source_sha")
        writer = status.get("git_writer")
        if not (
            isinstance(source, str)
            and _SHA.fullmatch(source)
            and source != head
            and isinstance(writer, dict)
            and writer.get("ok") is True
            and writer.get("dry_run") is False
            and writer.get("commit_sha") == head
        ):
            return None
        committed = writer.get("committed_paths")
        if not isinstance(committed, list) or not committed or not all(isinstance(p, str) for p in committed):
            return None
        if reader is None:
            project_dir = _project_dir(spec_dir)
            repo = _github_repo(project_dir)
            if repo is None:
                return None
            commit = _gh_commit(*repo, head, project_dir)
        else:
            commit = reader(head)
        if commit is None:
            return None
        parents, files = commit
        if parents != [source] or not files or len(files) >= 300:
            return None
        if all(isinstance(p, str) and p in committed and _is_test_path(p) for p in files):
            return source
    except Exception:  # noqa: BLE001 - uncertain provenance must fail closed
        return None
    return None
