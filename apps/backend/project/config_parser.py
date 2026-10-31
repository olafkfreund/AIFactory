"""
Config File Parser
==================

Utilities for reading and parsing project configuration files
(package.json, pyproject.toml, composer.json, etc.).
"""

import fnmatch
import json
import subprocess
import tomllib
from pathlib import Path

from core.child_env import child_env

# The `sys.version_info >= (3, 11)` guard and its `tomli` fallback that used to
# sit here are gone (#1211): `tomllib` has been stdlib since 3.11, the repo now
# declares `target-version = "py311"` in ruff.toml, and the runtime image is
# 3.12+. UP036's fix, applied by hand because ruff classifies it unsafe -- it
# deletes a branch, and a branch deletion should be read by a person.


class ConfigParser:
    """Parses project configuration files."""

    def __init__(self, project_dir: Path, ref: str | None = None):
        """
        Initialize config parser.

        Args:
            project_dir: Root directory of the project
            ref: Optional git ref to read tracked files from
        """
        self.project_dir = Path(project_dir).resolve()
        self.ref = ref

    def git_output(self, *args: str) -> str | None:
        """Run a git command in project_dir and return stdout, or None on failure."""
        try:
            proc = subprocess.run(  # noqa: S603
                ["git", *args],  # noqa: S607 - git from PATH, as elsewhere
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                # The agent can write refs/replace/*; never let it swap the base.
                env={**child_env(), "GIT_NO_REPLACE_OBJECTS": "1"},
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    def read_json(self, filename: str) -> dict | None:
        """Read a JSON file from project root (or from ``ref``)."""
        text = self.read_text(filename)
        if text is None:
            return None
        try:
            data: dict = json.loads(text)
        except json.JSONDecodeError:
            return None
        return data

    def read_toml(self, filename: str) -> dict | None:
        """Read a TOML file from project root."""
        try:
            if self.ref:
                text = self.read_text(filename)
                if text is None:
                    return None
                return tomllib.loads(text)
            with open(self.project_dir / filename, "rb") as f:
                return tomllib.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:
            # Handle both tomllib.TOMLDecodeError and tomli.TOMLDecodeError
            if "TOMLDecodeError" in type(e).__name__:
                return None
            raise

    def read_text(self, filename: str) -> str | None:
        """Read a text file from project root."""
        if self.ref:
            return self.git_output("show", f"{self.ref}:./{filename}")
        try:
            with open(self.project_dir / filename) as f:
                return f.read()
        except (OSError, FileNotFoundError):
            return None

    def file_exists(self, *paths: str) -> bool:
        """Check if any of the given files/patterns exist."""
        if self.ref:
            out = self.git_output("ls-tree", "--name-only", self.ref)
            if not out:
                return False
            names = out.splitlines()
            return any(fnmatch.fnmatch(n, p) for p in paths for n in names)
        for p in paths:
            # Handle glob patterns
            if "*" in p:
                if list(self.project_dir.glob(p)):
                    return True
            else:
                if (self.project_dir / p).exists():
                    return True
        return False

    def glob_files(self, pattern: str) -> list[Path]:
        """Find files matching a pattern."""
        if self.ref:
            out = self.git_output("ls-tree", "--name-only", self.ref)
            if not out:
                return []
            return [
                self.project_dir / name
                for name in out.splitlines()
                if fnmatch.fnmatch(name, pattern)
            ]
        return list(self.project_dir.glob(pattern))
