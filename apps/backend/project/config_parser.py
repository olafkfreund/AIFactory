"""
Config File Parser
==================

Utilities for reading and parsing project configuration files
(package.json, pyproject.toml, composer.json, etc.).
"""

import fnmatch
import hashlib
import json
import re
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

    def _git(self, *args: str) -> bytes | None:
        """Run a git command in project_dir and return raw stdout, or None."""
        try:
            proc = subprocess.run(  # noqa: S603
                ["git", *args],  # noqa: S607 - git from PATH, as elsewhere
                cwd=self.project_dir,
                capture_output=True,
                # The agent can write refs/replace/*; never let it swap the base.
                env={**child_env(), "GIT_NO_REPLACE_OBJECTS": "1"},
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    def git_output(self, *args: str) -> str | None:
        """Run a git command in project_dir and return stdout, or None on failure."""
        out = self._git(*args)
        return None if out is None else out.decode("utf-8", "replace")

    def _object(self, kind: str, oid: str) -> bytes | None:
        """Raw object content, or None unless its bytes hash to ``oid`` (#2028).

        Git does not verify a loose object's hash on read, and the agent can
        write into the git directory, so every object on the way is rehashed.
        """
        raw = self._git("cat-file", kind, oid)
        algo = {40: "sha1", 64: "sha256"}.get(len(oid))
        if raw is None or algo is None:
            return None
        digest = hashlib.new(algo, f"{kind} {len(raw)}\0".encode() + raw)
        return raw if digest.hexdigest() == oid else None

    def _tree(self, oid: str) -> dict[str, tuple[str, str]] | None:
        """Entries of a verified tree as ``{name: (mode, oid)}``."""
        raw = self._object("tree", oid)
        if raw is None:
            return None
        width = len(oid) // 2
        entries: dict[str, tuple[str, str]] = {}
        i = 0
        while i < len(raw):
            sp, nul = raw.find(b" ", i), raw.find(b"\0", i)
            if sp < 0 or nul < sp or nul + 1 + width > len(raw):
                return None
            name = raw[sp + 1 : nul].decode("utf-8", "replace")
            entries[name] = (raw[i:sp].decode(), raw[nul + 1 : nul + 1 + width].hex())
            i = nul + 1 + width
        return entries

    def _walk(self, parts: list[str]) -> tuple[str, str] | None:
        """``(mode, oid)`` of the verified entry at ``parts`` under the base, in cwd."""
        assert self.ref  # noqa: S101 - callers check
        # A hex ref is the commit itself; a tag or branch named like it must not
        # stand in. Anything else (HEAD, no recorded base) is resolved once.
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.ref):
            oid: str | None = self.ref
        else:
            out = self.git_output("rev-parse", "--verify", f"{self.ref}^{{commit}}")
            oid = out.strip() if out else None
        commit = self._object("commit", oid) if oid else None
        prefix = self.git_output("rev-parse", "--show-prefix")
        first = commit.split(b"\n", 1)[0] if commit else b""
        if prefix is None or not first.startswith(b"tree "):
            return None
        entry = ("40000", first[5:].decode())
        for name in [p for p in prefix.strip().split("/") if p] + parts:
            entries = self._tree(entry[1]) if entry[0] == "40000" else None
            if entries is None or name not in entries:
                return None
            entry = entries[name]
        return entry

    def _base_names(self) -> list[str]:
        """Names of the verified base entries in cwd."""
        entry = self._walk([])
        entries = self._tree(entry[1]) if entry else None
        # A 120000 entry is a symlink, not a file the profile may rely on.
        return [n for n, (mode, _) in (entries or {}).items() if mode != "120000"]

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
            parts = filename.split("/")
            if filename.startswith("/") or ".." in parts:
                return None
            entry = self._walk([p for p in parts if p not in ("", ".")])
            # Regular files only: a 120000 entry is a symlink, not file content.
            raw = self._object("blob", entry[1]) if entry else None
            if raw is None or entry is None or entry[0] not in ("100644", "100755"):
                return None
            return raw.decode("utf-8", "replace")
        try:
            with open(self.project_dir / filename) as f:
                return f.read()
        except (OSError, FileNotFoundError):
            return None

    def file_exists(self, *paths: str) -> bool:
        """Check if any of the given files/patterns exist."""
        if self.ref:
            names = self._base_names()
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
            return [
                self.project_dir / name
                for name in self._base_names()
                if fnmatch.fnmatch(name, pattern)
            ]
        return list(self.project_dir.glob(pattern))
