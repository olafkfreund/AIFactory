"""Enforce branch-as-truth: never read a task worktree's HEAD as the task branch.

The invariant (docs/docs/architecture/build-output-propagation.md):

    A build Job's filesystem is write-once-and-discard. Code escapes via
    ``git push``. Everything else escapes only if something explicitly
    pushes it.

Under ``AIFACTORY_BUILD_BACKEND=kubejob`` the control plane's task worktree
sits on the BASE branch; the work lives on the branch the build Job pushed.
Running ``git rev-parse --abbrev-ref HEAD`` in the worktree therefore yields
``main``, and treating that as the task branch produced six separate Approve
defects (#1070-#1076) after four earlier bugs of the same class (#190, #218,
#852, #1038). The doc stated the rule; nothing enforced it. This test is the
enforcement (#1082).

What is flagged: a call whose argv asks git for the current branch name
(``rev-parse --abbrev-ref HEAD``, or its ``branch --show-current``
equivalent) and whose remaining arguments (``cwd=`` or a runner-style
positional) mention a worktree -- any identifier containing ``worktree``.

What is NOT flagged (the sanctioned patterns on main):

- reading the PROJECT repo's HEAD (``cwd=project_path``) to discover the base
  branch -- correct and common;
- a worktree HEAD read in a function that also calls
  ``resolve_task_branch``/``resolve_work_ref``: the guarded-fallback shape,
  where the resolver runs first and the worktree HEAD is only the honest
  "nothing pushed yet" fallback;
- ``services/task_branch.py`` itself (allowlisted below): the resolver's own
  base-branch-guarded read IS the implementation.

Known ceilings, both accepted because precision (never flagging a legitimate
read, so the check is never suppressed into decoration) is deliberately
preferred over coverage:

- detection keys on the ``worktree`` naming convention: a read through a cwd
  named, say, ``task_dir`` slips past. The repo has no such site today.
- the resolver exemption is function-scoped and unconditional: a function
  that calls ``resolve_task_branch``/``resolve_work_ref``, ignores the
  result, and then uses the worktree HEAD anyway is NOT flagged. Verifying
  the HEAD read actually sits in the resolver-failed fallback position needs
  dataflow analysis, which would misfire on legitimate shapes.
"""

from __future__ import annotations

import ast
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"

# Files where a worktree HEAD read is the sanctioned implementation, not a
# consumer. Every entry needs a reason. Prefer zero entries.
ALLOWLIST: dict[str, str] = {
    "services/task_branch.py": (
        "the canonical resolver: its HEAD read is guarded (a result equal to "
        "the base branch is rejected) and is the implementation every other "
        "site is being pointed at"
    ),
}

_RESOLVERS = {"resolve_task_branch", "resolve_work_ref"}
# Two argv shapes read "the current branch": the one the bug class used, and
# its `git branch --show-current` equivalent (covered so the check cannot be
# dodged by switching the command).
_ARGV_MARKER_SETS = (
    {"rev-parse", "--abbrev-ref", "HEAD"},
    {"branch", "--show-current"},
)


def _string_constants(node: ast.AST) -> set[str]:
    return {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _identifiers(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
    return names


def _is_branch_head_read(call: ast.Call) -> bool:
    """Does this call's argv ask git for the current branch name?"""
    return any(
        isinstance(arg, (ast.List, ast.Tuple))
        and any(_string_constants(arg) >= markers for markers in _ARGV_MARKER_SETS)
        for arg in call.args
    )


def _reads_worktree(call: ast.Call) -> bool:
    """Does any non-argv argument (cwd= or positional) mention a worktree?"""
    others: list[ast.AST] = [
        a for a in call.args if not isinstance(a, (ast.List, ast.Tuple))
    ]
    others.extend(kw.value for kw in call.keywords)
    return any(
        "worktree" in name.lower() for node in others for name in _identifiers(node)
    )


def _references_resolver(func: ast.AST) -> bool:
    return bool(_RESOLVERS & _identifiers(func))


def _violations_in(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    lines: list[int] = []

    def visit(node: ast.AST, func_stack: list[ast.AST]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_stack = [*func_stack, node]
        if (
            isinstance(node, ast.Call)
            and _is_branch_head_read(node)
            and _reads_worktree(node)
            and not any(_references_resolver(f) for f in func_stack)
        ):
            lines.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, func_stack)

    visit(tree, [])
    return lines


def test_no_worktree_head_read_as_task_branch() -> None:
    violations: list[str] = []
    for path in sorted(SERVER_DIR.rglob("*.py")):
        rel = path.relative_to(SERVER_DIR).as_posix()
        if rel in ALLOWLIST:
            continue
        violations.extend(f"server/{rel}:{line}" for line in _violations_in(path))

    assert not violations, (
        "worktree HEAD read used as a task branch:\n  "
        + "\n  ".join(violations)
        + "\n\nUnder the kubejob build backend the task worktree sits on the "
        "BASE branch -- its HEAD is not the task branch, and reading it "
        "diffs/merges base against base (#1082, and #190/#218/#852/#1038 "
        "before it).\n"
        "Fix: resolve the branch the build pushed via "
        "server/services/task_branch.resolve_task_branch (branch name) or "
        "resolve_work_ref (branch plus a ref readable in the project repo), "
        "and read the work as {base}...{ref}. A worktree HEAD read is "
        "acceptable only as the fallback after the resolver found nothing, "
        "in the same function.\n"
        "If a file legitimately must read a worktree HEAD, add it to "
        "ALLOWLIST in this test with a reason."
    )


def test_allowlist_entries_still_exist() -> None:
    """A stale allowlist entry is a hole; drop entries whose file is gone."""
    for rel in ALLOWLIST:
        assert (SERVER_DIR / rel).is_file(), f"stale ALLOWLIST entry: {rel}"


def test_check_catches_the_prefix_bug(tmp_path: Path) -> None:
    """Self-test: the exact pre-#1074 create-pr pattern must be flagged.

    Reconstructed from ``git show a0c05f45~1:.../routes/pr.py`` -- the site
    that asked GitHub to open main -> main. If this stops failing the checker
    has been weakened into decoration.
    """
    prefix_site = (
        "import subprocess\n"
        "def create_pr(worktree_path, project_path):\n"
        "    result = subprocess.run(\n"
        '        ["git", "rev-parse", "--abbrev-ref", "HEAD"],\n'
        "        cwd=worktree_path,\n"
        "        capture_output=True,\n"
        "        text=True,\n"
        "        check=True,\n"
        "    )\n"
        "    worktree_branch = result.stdout.strip()\n"
        "    return worktree_branch\n"
    )
    tmp = tmp_path / "prefix_reconstruction.py"
    tmp.write_text(prefix_site)
    assert _violations_in(tmp) == [3]

    # The runner-style variant (pre-#1082 pr_endgame shape, positional cwd).
    tmp.write_text(
        "def gather(runner, worktree):\n"
        '    head = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"], str(worktree))\n'
        "    return head.out.strip()\n"
    )
    assert _violations_in(tmp) == [2]

    # The `git branch --show-current` equivalent of the same read.
    tmp.write_text(
        prefix_site.replace(
            '"rev-parse", "--abbrev-ref", "HEAD"', '"branch", "--show-current"'
        )
    )
    assert _violations_in(tmp) == [3]

    # And the two sanctioned shapes are NOT flagged: the project-repo read...
    tmp.write_text(prefix_site.replace("cwd=worktree_path", "cwd=project_path"))
    assert _violations_in(tmp) == []

    # ...and the guarded fallback (resolver called in the same function).
    tmp.write_text(
        prefix_site.replace(
            "def create_pr(worktree_path, project_path):",
            "def create_pr(worktree_path, project_path):\n"
            "    branch, reason = resolve_work_ref(worktree_path=worktree_path)",
        )
    )
    assert _violations_in(tmp) == []


# --------------------------------------------------------------------------- #
# The second detector (#1089).
#
# The check above matches an EXPLICIT branch-name read (`rev-parse --abbrev-ref
# HEAD`, `branch --show-current`). It missed an entire unfixed region, because
# `git diff {base}...HEAD` with a worktree cwd is the same lie without ever
# naming HEAD as a branch: under kubejob the worktree IS the base branch, so the
# range is base...base and the answer is an empty change set. Found by hand
# audit, not by this file, which is the argument for adding it.
#
# Scope is widened to apps/backend as well, because that is where the 11 sites
# lived and the original SERVER_DIR-only scan could not see any of them.
# --------------------------------------------------------------------------- #

_BACKEND_DIR = SERVER_DIR.parents[1] / "backend"

# Range subcommands: a two-endpoint read where one endpoint is HEAD.
_RANGE_CMDS = {"diff", "log", "rev-list", "merge-base", "cherry"}

# Sites where reading the worktree's own HEAD is the CORRECT thing, because the
# code runs where the worktree genuinely holds the work. Every entry states why.
_RANGE_ALLOWLIST: dict[str, str] = {
    "core/worktree.py": (
        "WorktreeManager runs agent-side: constructed by agents/parallel_integration "
        "and core/workspace/{setup,finalization}. There the worktree HEAD IS the "
        "task branch, so {base}..HEAD is the right read and a ref-based rewrite "
        "would break the majority caller"
    ),
    "merge/timeline_git.py": (
        "baseline capture, not a review surface: reached only from "
        "core/workspace/setup.py and merge/tracker_cli.py, at worktree-creation "
        "time when the worktree legitimately holds no work yet. The web server "
        "has no reference to FileTimelineTracker at all"
    ),
}


def _is_head_range_read(call: ast.Call) -> bool:
    """Does this call ask git for a range ending at the worktree's own HEAD?"""
    for arg in call.args:
        if not isinstance(arg, (ast.List, ast.Tuple)):
            continue
        if not (_string_constants(arg) & _RANGE_CMDS):
            continue
        for node in ast.walk(arg):
            # "{base}...HEAD" as a literal, or an f-string ending in HEAD.
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.endswith("HEAD")
                and ".." in node.value
            ):
                return True
            if isinstance(node, ast.JoinedStr) and node.values:
                tail = node.values[-1]
                if (
                    isinstance(tail, ast.Constant)
                    and isinstance(tail.value, str)
                    and tail.value.endswith("HEAD")
                ):
                    return True
        # `["git", "merge-base", target, "HEAD"]` -- HEAD as a bare endpoint.
        if "HEAD" in _string_constants(arg):
            return True
    return False


def _range_violations_in(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    lines: list[int] = []

    def visit(node: ast.AST, func_stack: list[ast.AST]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_stack = [*func_stack, node]
        if (
            isinstance(node, ast.Call)
            and _is_head_range_read(node)
            and _reads_worktree(node)
            and not any(_references_resolver(f) for f in func_stack)
        ):
            lines.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, func_stack)

    visit(tree, [])
    return lines


# Directories that are not this repository's source. apps/backend carries a
# .venv in CI, and ast.parse dies on third-party files -- msal ships one with a
# BOM. Scanning them made this check fail on the environment rather than on the
# code, which is its own kind of dishonest gate: it was red for a reason that has
# nothing to do with the invariant.
_NOT_OUR_CODE = frozenset(
    {
        ".venv",
        "venv",
        "site-packages",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        ".mypy_cache",
        ".ruff_cache",
    }
)


def test_no_worktree_range_read_as_the_task_work() -> None:
    violations: list[str] = []
    for root, label in ((SERVER_DIR, "server"), (_BACKEND_DIR, "backend")):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if _NOT_OUR_CODE & set(path.parts):
                continue
            rel = path.relative_to(root).as_posix()
            if rel in ALLOWLIST or rel in _RANGE_ALLOWLIST or "test" in path.name:
                continue
            violations.extend(
                f"{label}/{rel}:{line}" for line in _range_violations_in(path)
            )

    assert not violations, (
        "a git range ending at a worktree's HEAD is being read as the task's work:"
        "\n  " + "\n  ".join(violations) + "\n\n"
        "Under the kubejob backend the worktree sits on the BASE branch, so this "
        "range is base...base and yields an empty change set -- the shape that "
        "made merge preview report zero semantic conflicts for every task "
        "(#1089).\n"
        "Fix: read the ref the build pushed, as {base}...{ref} in the PROJECT "
        "repo. The control plane gets that ref from "
        "server/services/task_branch.resolve_work_ref.\n"
        "If the code legitimately runs where the worktree DOES hold the work "
        "(agent-side, or baseline capture at worktree creation), add it to "
        "_RANGE_ALLOWLIST with that reason."
    )


def test_the_range_detector_catches_the_semantic_half_bug(tmp_path: Path) -> None:
    """Self-test: the exact pre-#1089 shape must be flagged.

    Reconstructed from modification_tracker.refresh_from_git. If this stops
    failing, the detector has been weakened into decoration.
    """
    site = tmp_path / "reconstruction.py"
    site.write_text(
        "import subprocess\n"
        "def refresh_from_git(worktree_path, target_branch):\n"
        "    return subprocess.run(\n"
        '        ["git", "diff", "--name-only", f"{target_branch}...HEAD"],\n'
        "        cwd=worktree_path,\n"
        "    )\n"
    )
    assert _range_violations_in(site) == [3]

    # merge-base with HEAD as a bare endpoint (the timeline_git shape).
    site.write_text(
        "import subprocess\n"
        "def get_branch_point(worktree_path, target_branch):\n"
        "    return subprocess.run(\n"
        '        ["git", "merge-base", target_branch, "HEAD"], cwd=worktree_path\n'
        "    )\n"
    )
    assert _range_violations_in(site) == [3]

    # A project-repo read of the same shape is NOT the bug and must not flag.
    site.write_text(
        "import subprocess\n"
        "def base_diff(project_path, base):\n"
        "    return subprocess.run(\n"
        '        ["git", "diff", "--name-only", f"{base}...HEAD"], cwd=project_path\n'
        "    )\n"
    )
    assert _range_violations_in(site) == []
