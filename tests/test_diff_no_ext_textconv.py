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

_DIFF_CMDS = {"diff", "diff-tree", "diff-index", "diff-files", "range-diff", "blame"}
_PATCH_FLAGS = {"-p", "--patch"}
_NO_PATCH_FLAGS = {"-s", "--no-patch"}
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


def _is_blob(e: ast.expr) -> bool:
    if isinstance(e, ast.Constant) and isinstance(e.value, str):
        return ":" in e.value
    if isinstance(e, ast.JoinedStr):
        return any(
            isinstance(p, ast.Constant) and isinstance(p.value, str) and ":" in p.value
            for p in e.values
        )
    return False


def _is_diff_family(
    sub: str, consts: set[str], vals: list[str | None], elts: list[ast.expr]
) -> bool:
    if sub in _DIFF_CMDS or (sub == "log" and bool(consts & _PATCH_FLAGS)):
        return True
    if sub == "stash":
        return "show" in consts and bool(consts & _PATCH_FLAGS)
    if sub != "show":
        return False
    if consts & _PATCH_FLAGS or not (consts & _NO_PATCH_FLAGS):
        idx = vals.index("show")
        pos = [
            e
            for e, v in zip(elts[idx + 1 :], vals[idx + 1 :], strict=False)
            if v is None or not v.startswith("-") or v == "--"
        ]
        return bool(consts & _PATCH_FLAGS) or not (
            pos and all(_is_blob(e) for e in pos)
        )
    return False


def _calls_in(tree: ast.AST, rel: str) -> Iterator[tuple[str, int, list[str | None]]]:
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
            (b for a, b in itertools.pairwise(vals) if a == "git" and b is not None),
            None,
        )
        if sub is None and "git" in callee:
            sub = next((v for v in vals if v is not None), None)
        if sub is not None and _is_diff_family(sub, consts, vals, elts):
            yield rel, node.lineno, vals


def _git_diff_calls() -> Iterator[tuple[str, int, list[str | None]]]:
    """Yield (path, line, argv constants) for every git diff-family argv.

    Known limit: only literal argv is seen. A call whose subcommand is built at
    runtime (``cmd.append("diff")``, shell strings) is invisible to this scan.
    """
    for path, rel in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        yield from _calls_in(tree, rel)


def _flags_before_pathspec(vals: list[str | None]) -> bool:
    # After "--" git reads the flags as pathspecs, so they must come first.
    head = set(vals[: vals.index("--")] if "--" in vals else vals)
    req = {"--no-textconv"} if "blame" in head else _REQUIRED
    return req.issubset(head)


def test_flags_after_double_dash_do_not_count() -> None:
    assert not _flags_before_pathspec(
        ["git", "diff", "a", "--", "p", "--no-ext-diff", "--no-textconv"]
    )
    assert _flags_before_pathspec(
        ["git", "diff", "--no-ext-diff", "--no-textconv", "a", "--", "p"]
    )


def test_every_git_diff_call_disables_drivers_and_textconv() -> None:
    calls = list(_git_diff_calls())
    offenders = [f"{p}:{n}" for p, n, v in calls if not _flags_before_pathspec(v)]
    assert not offenders, (
        "git diff-family calls missing --no-ext-diff --no-textconv (#1718):\n"
        + "\n".join(offenders)
    )


def test_scan_flags_planted_calls() -> None:
    for src in (
        'subprocess.run(["git", "show", "HEAD"])',
        '["git", "blame", "f"]',
        '["git", "range-diff", "a...b"]',
        '["git", "stash", "show", "-p"]',
    ):
        calls = list(_calls_in(ast.parse(src), "x.py"))
        assert len(calls) == 1 and not _flags_before_pathspec(calls[0][2]), src
    for src in (
        '["git", "show", f"{r}:{p}"]',
        '["git", "show", "--no-patch", "HEAD"]',
    ):
        assert list(_calls_in(ast.parse(src), "x.py")) == [], src
    assert _flags_before_pathspec(["git", "blame", "--no-textconv", "f"])


def _has_merge_pins(elts: list[ast.expr], pin_names: set[str]) -> bool:
    for e in elts:
        if not isinstance(e, ast.Starred):
            continue
        if isinstance(e.value, ast.Call) and _callee(e.value) == "merge_driver_pins":
            return True
        if isinstance(e.value, ast.Name) and e.value.id in pin_names:
            return True
    return False


def _unpinned_merges_in(tree: ast.AST, rel: str) -> Iterator[str]:
    pin_names = {
        t.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and _callee(n.value) == "merge_driver_pins"
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        vals = _strs(node.elts)
        consts = [v for v in vals if v is not None]
        if not consts or consts[0] != "git":
            continue
        cset = set(consts)
        is_merge = "merge-tree" in cset or (
            bool(cset & {"merge", "rebase"}) and "--abort" not in cset
        )
        if is_merge and not _has_merge_pins(node.elts, pin_names):
            yield f"{rel}:{node.lineno}"


def test_every_server_merge_pins_merge_drivers() -> None:
    """Every literal git merge/rebase/merge-tree argv must unpack merge_driver_pins (#1761).

    Known limit: argv without a literal ``"git"`` (``apps/backend/core/worktree.py``
    ``merge_args``, run through ``_run_git``) is not seen by this AST scan;
    no runtime test covers ``_do_merge_locked`` directly either, so that site
    must be kept in sync with ``merge_driver_pins`` by review.
    """
    offenders = [
        hit
        for path, rel in _sources()
        for hit in _unpinned_merges_in(
            ast.parse(path.read_text(encoding="utf-8"), filename=rel), rel
        )
    ]
    assert not offenders, (
        "server merge/rebase/merge-tree calls missing *merge_driver_pins (#1761):\n"
        + "\n".join(offenders)
    )
    assert list(_unpinned_merges_in(ast.parse('["git", "merge", b]'), "x.py")) == [
        "x.py:1"
    ]
    pinned = 'pins = merge_driver_pins(c)\n["git", *pins, "merge", b]\n'
    assert list(_unpinned_merges_in(ast.parse(pinned), "x.py")) == []


def test_scan_still_sees_the_full_patch_sites() -> None:
    full_patch = {p for p, _, v in _git_diff_calls() if not set(v) & _SUMMARY}
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
