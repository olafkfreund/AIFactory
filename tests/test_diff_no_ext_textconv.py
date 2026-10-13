"""Server diffs must not run a diff driver or textconv from agent config (#1718).

An agent can write ``.gitattributes`` and ``.git/config`` in its worktree. A
``diff.<x>.textconv``, ``diff.<x>.command`` or ``diff.external`` planted there
runs a program when the server diffs that worktree. Every diff-family git call
under ``apps/`` must therefore pass ``--no-ext-diff --no-textconv``.
"""

from __future__ import annotations

import ast
import itertools
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_APPS = _ROOT / "apps"

_DIFF_CMDS = {"diff", "diff-tree", "diff-index", "diff-files"}
_PATCH_CMDS = {"log", "show"}  # only when they also carry -p / --patch
_PATCH_FLAGS = {"-p", "--patch"}
_REQUIRED = {"--no-ext-diff", "--no-textconv"}
_SUMMARY = {
    "--name-only",
    "--name-status",
    "--stat",
    "--numstat",
    "--shortstat",
    "--quiet",
}
_SKIP_DIRS = {"tests", "test", "node_modules", "__pycache__", "site-packages", "venv"}

# The full-patch sites the issue names. The matcher must keep seeing them.
_FULL_PATCH_FILES = {
    "apps/web-server/server/routes/worktree_merge.py",
    "apps/backend/core/worktree.py",
    "apps/backend/analysis/insight_extractor.py",
    "apps/backend/runners/github/context_gatherer.py",
    "apps/backend/merge/file_evolution/modification_tracker.py",
}


def _sources() -> Iterator[tuple[Path, str]]:
    for path in sorted(_APPS.rglob("*.py")):
        rel = path.relative_to(_ROOT)
        if any(p in _SKIP_DIRS or p.startswith(".") for p in rel.parts[:-1]):
            continue
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        yield path, rel.as_posix()


def _callee(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id.lower()
    if isinstance(f, ast.Attribute):
        return f.attr.lower()
    return ""


def _strs(elts: list[ast.expr]) -> list[str | None]:
    return [
        e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None
        for e in elts
    ]


def _is_diff_family(sub: str, consts: set[str]) -> bool:
    return sub in _DIFF_CMDS or (sub in _PATCH_CMDS and bool(consts & _PATCH_FLAGS))


def _git_diff_calls() -> Iterator[tuple[str, int, set[str]]]:
    """Yield (path, line, string constants) for every git diff-family argv."""
    for path, rel in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        arg_of: dict[int, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for a in node.args:
                    arg_of[id(a)] = _callee(node)
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple)):
                elts, callee = node.elts, arg_of.get(id(node), "")
            elif isinstance(node, ast.Call):
                elts, callee = node.args, _callee(node)
            else:
                continue
            vals = _strs(elts)
            consts = {v for v in vals if v is not None}
            sub = next(
                (
                    b
                    for a, b in itertools.pairwise(vals)
                    if a == "git" and b is not None
                ),
                None,
            )
            if sub is None and "git" in callee:
                sub = next((v for v in vals if v is not None), None)
            if sub is not None and _is_diff_family(sub, consts):
                yield rel, node.lineno, consts


def test_every_git_diff_call_disables_drivers_and_textconv() -> None:
    calls = list(_git_diff_calls())
    offenders = [f"{p}:{n}" for p, n, c in calls if not _REQUIRED.issubset(c)]
    assert not offenders, (
        "git diff-family calls missing --no-ext-diff --no-textconv (#1718):\n"
        + "\n".join(offenders)
    )


def test_scan_still_sees_the_full_patch_sites() -> None:
    full_patch = {p for p, _, c in _git_diff_calls() if not c & _SUMMARY}
    missing = _FULL_PATCH_FILES - full_patch
    assert not missing, f"matcher no longer finds the full-patch diff in: {missing}"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.parametrize("key", ["diff.x.textconv", "diff.x.command", "diff.external"])
def test_session_diff_does_not_run_a_planted_program(tmp_path: Path, key: str) -> None:
    from analysis.insight_extractor import get_session_diff  # noqa: PLC0415

    repo = tmp_path / "repo"
    repo.mkdir()
    marker = tmp_path / "ran"
    plant = tmp_path / "plant.sh"
    plant.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 0\n')
    plant.chmod(0o755)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitattributes").write_text("f diff=x\n")
    (repo / "f").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "one")
    c1 = _git(repo, "rev-parse", "HEAD")
    (repo / "f").write_text("a\nadded-line\n")
    _git(repo, "commit", "-qam", "two")
    c2 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "config", key, str(plant))

    # Control: plain git diff must fire the plant, or the check below is vacuous.
    subprocess.run(  # noqa: S603
        ["git", "diff", c1, c2],  # noqa: S607
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if not marker.exists():
        pytest.skip(f"this git ignores a planted {key}")
    marker.unlink()

    out = get_session_diff(repo, c1, c2)
    assert not marker.exists(), f"get_session_diff ran the planted {key}"
    assert "+added-line" in out
