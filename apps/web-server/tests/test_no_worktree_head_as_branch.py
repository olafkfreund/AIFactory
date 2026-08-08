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
- the two guarded shapes described under "the resolver exemption" below;
- ``services/task_branch.py`` itself (allowlisted below): the resolver's own
  base-branch-guarded read IS the implementation.

The resolver exemption
----------------------

Merely mentioning ``resolve_task_branch``/``resolve_work_ref`` somewhere in
the enclosing function used to buy a free pass. That is the hole this whole
bug class kept slipping through (#1089): a function could call the resolver,
drop the answer on the floor, and read the worktree HEAD anyway. Proven by
mutation against ``routes/pr.py`` -- not flagged.

Three conditions now, all lexical (no dataflow engine, and measured against
every legitimate site in the tree before landing):

1. a resolver is actually CALLED -- a mention is not a call;
2. its answer is CONSUMED -- at least one name it binds is read again
   somewhere in the function. Assigning the result and never looking at it is
   the "calls-then-ignores" shape, and it now flags;
3. the HEAD read is POSITIONED so the resolver can still have the last word.
   Either shape counts, and the repo contains both:

   - resolve-then-fall-back: the read sits inside an ``if``/``elif`` whose
     test names a resolver-bound value, so it is only reachable when the
     resolver found nothing (``routes/worktree_merge.py``, four sites);
   - read-then-validate: the read is lexically BEFORE any resolver call, so
     the resolver runs afterwards and overrides a base-branch answer
     (``services/pr_endgame.gather_pr_context``).

Known ceilings, all accepted because precision (never flagging a legitimate
read, so the check is never suppressed into decoration) is deliberately
preferred over coverage:

- detection keys on the ``worktree`` naming convention: a read through a cwd
  named, say, ``task_dir`` slips past. The repo has no such site today.
- condition 3 checks POSITION, not dataflow. A read placed before the
  resolver whose value is then never overridden still passes, and so does a
  read guarded by an ``if`` on a resolver-bound name that the guard does not
  actually depend on. Proving the resolver's answer wins needs dataflow
  analysis; what is enforced is that the answer was asked for, looked at, and
  given a position from which it can win.
- the exemption is still function-scoped: splitting the resolver call and the
  HEAD read into two functions defeats all three conditions. No site in the
  repo has that shape.

``test_the_exemption_does_not_cover_a_discarded_answer`` pins every one of
these decisions. It is the reason the next person does not have to
re-discover the ceiling by hand.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
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


def _resolver_calls(func: ast.AST) -> list[ast.Call]:
    """Calls to a canonical resolver inside *func*. A mention is not a call."""
    return [
        n
        for n in ast.walk(func)
        if isinstance(n, ast.Call) and _RESOLVERS & _identifiers(n.func)
    ]


def _resolver_result_names(func: ast.AST) -> set[str]:
    """Names bound straight off a resolver call: ``a, b = resolve_work_ref(...)``."""
    names: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        if not (
            isinstance(node.value, ast.Call)
            and _RESOLVERS & _identifiers(node.value.func)
        ):
            continue
        for target in node.targets:
            names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
    return names


def _is_read_anywhere(func: ast.AST, names: set[str]) -> bool:
    """Is any of *names* LOADED somewhere in *func*?

    Store-only means the resolver's answer was assigned and never looked at --
    overwriting it with a worktree HEAD read is exactly the shape #1089 proved
    was uncaught.
    """
    return any(
        isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in names
        for n in ast.walk(func)
    )


def _inside_guard_on(func: ast.AST, call: ast.Call, names: set[str]) -> bool:
    """Does *call* sit in an ``if``/``else`` branch whose test names a resolver result?"""
    for node in ast.walk(func):
        if not isinstance(node, ast.If) or not (names & _identifiers(node.test)):
            continue
        if any(
            n is call for stmt in (*node.body, *node.orelse) for n in ast.walk(stmt)
        ):
            return True
    return False


def _resolver_exempts(func: ast.AST, call: ast.Call) -> bool:
    """Is *call*'s worktree HEAD read the sanctioned guarded shape? (module docstring)"""
    resolver_calls = _resolver_calls(func)
    if not resolver_calls:
        return False
    names = _resolver_result_names(func)
    if not _is_read_anywhere(func, names):
        return False
    if call.lineno < min(c.lineno for c in resolver_calls):
        return True  # read-then-validate: the resolver still gets the last word
    return _inside_guard_on(func, call, names)


def _scan(path: Path, matches: Callable[[ast.Call], bool]) -> list[int]:
    """Lines of every worktree read in *path* that *matches* and is not exempt."""
    tree = ast.parse(path.read_text(), filename=str(path))
    lines: list[int] = []

    def visit(node: ast.AST, func_stack: list[ast.AST]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_stack = [*func_stack, node]
        if (
            isinstance(node, ast.Call)
            and matches(node)
            and _reads_worktree(node)
            and not any(_resolver_exempts(f, node) for f in func_stack)
        ):
            lines.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, func_stack)

    visit(tree, [])
    return lines


def _violations_in(path: Path) -> list[int]:
    return _scan(path, _is_branch_head_read)


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


# The four `routes/worktree_merge.py` fallbacks all have this shape:
# resolve-then-fall-back, the read reachable only when the resolver came up empty.
_GUARDED_FALLBACK = (
    "import subprocess\n"
    "def merge_preview(worktree_path, project_path, task_id, base_branch):\n"
    "    branch, work_ref, reason = resolve_work_ref(\n"
    "        worktree_path=worktree_path, project_path=project_path\n"
    "    )\n"
    "    if not work_ref:\n"
    "        result = subprocess.run(\n"
    '            ["git", "rev-parse", "--abbrev-ref", "HEAD"],\n'
    "            cwd=worktree_path,\n"
    "            capture_output=True,\n"
    "            text=True,\n"
    "            check=True,\n"
    "        )\n"
    "        work_ref = result.stdout.strip()\n"
    "    return work_ref\n"
)

# `services/pr_endgame.gather_pr_context`: the HEAD read comes FIRST and the
# resolver overrides it when it turns out to be a base branch. Lexically the
# mirror image of the shape above, and equally legitimate.
_READ_THEN_VALIDATE = (
    "def gather_pr_context(runner, worktree, project_path, spec_id, base):\n"
    '    head = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"], str(worktree))\n'
    "    branch = head.out.strip() if head.ok else ''\n"
    '    if not branch or branch in {"HEAD", "main", "master", base}:\n'
    "        resolved, _reason = resolve_task_branch(\n"
    "            worktree_path=worktree, project_path=project_path\n"
    "        )\n"
    '        branch = resolved or f"aifactory/{spec_id}"\n'
    "    return branch\n"
)


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

    # ...and the guarded fallback, reconstructed from
    # worktree_merge.get_worktree_merge_preview: the resolver runs, its answer is
    # consulted, and the HEAD read is reachable only when that answer was empty.
    tmp.write_text(_GUARDED_FALLBACK)
    assert _violations_in(tmp) == []


def test_the_exemption_does_not_cover_a_discarded_answer(tmp_path: Path) -> None:
    """Pin the resolver exemption's real boundary, in both directions (#1089).

    Before this, the exemption was function-scoped and unconditional: any
    function that so much as MENTIONED ``resolve_task_branch``/
    ``resolve_work_ref`` was excused, whatever it then did with the answer.
    Injecting the pre-#1074 read into ``routes/pr.py`` in a function that calls
    the resolver and drops the result produced ``[]``.

    The two legitimate shapes in the tree must keep passing; the two abuse
    shapes must fail. If the abuse halves stop failing, the exemption has gone
    back to being a keyword that buys silence.
    """
    site = tmp_path / "exemption.py"

    # PASS: the two shapes that exist on main.
    site.write_text(_GUARDED_FALLBACK)
    assert _violations_in(site) == []
    site.write_text(_READ_THEN_VALIDATE)
    assert _violations_in(site) == []

    # FAIL: calls the resolver, never looks at the answer, uses HEAD instead.
    # This is the mutation that proved the hole.
    site.write_text(
        "import subprocess\n"
        "def create_pr(worktree_path, project_path):\n"
        "    branch, reason = resolve_work_ref(\n"
        "        worktree_path=worktree_path, project_path=project_path\n"
        "    )\n"
        "    result = subprocess.run(\n"
        '        ["git", "rev-parse", "--abbrev-ref", "HEAD"],\n'
        "        cwd=worktree_path,\n"
        "        check=True,\n"
        "    )\n"
        "    return result.stdout.strip()\n"
    )
    assert _violations_in(site) == [6]

    # FAIL: the answer IS consulted -- there is even a guard on it -- but the
    # HEAD read sits outside that guard and overwrites the answer regardless.
    # Consumption alone would excuse this; position is what catches it.
    site.write_text(
        "import subprocess\n"
        "def merge_preview(worktree_path, project_path):\n"
        "    branch, work_ref, reason = resolve_work_ref(\n"
        "        worktree_path=worktree_path, project_path=project_path\n"
        "    )\n"
        "    if not work_ref:\n"
        '        log("nothing pushed yet")\n'
        "    result = subprocess.run(\n"
        '        ["git", "rev-parse", "--abbrev-ref", "HEAD"],\n'
        "        cwd=worktree_path,\n"
        "        check=True,\n"
        "    )\n"
        "    work_ref = result.stdout.strip()\n"
        "    return work_ref\n"
    )
    assert _violations_in(site) == [8]

    # And a MENTION is not a call: naming the resolver in an annotation or a
    # dead reference must not excuse anything.
    site.write_text(
        "import subprocess\n"
        "def create_pr(worktree_path, project_path):\n"
        "    _resolver = resolve_work_ref\n"
        "    result = subprocess.run(\n"
        '        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree_path\n'
        "    )\n"
        "    return result.stdout.strip()\n"
    )
    assert _violations_in(site) == [4]


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
        "_get_worktree_stats only, and only because get_worktree_info -- its sole "
        "caller -- returns early unless the worktree's .git is a FILE. A kubejob "
        "build directory is a standalone CLONE (.git is a directory), so the "
        "control plane never reaches these two reads; the subprocess backend that "
        "does reach them has the work in the worktree. get_changed_files in the "
        "same file was NOT covered by that guard and is fixed, not allowlisted "
        "(#1089): it feeds run.py --review/--merge/--discard from the control plane"
    ),
    "merge/timeline_git.py": (
        "get_branch_point and _detect_target_branch: both are BASE-branch "
        "discovery, probing which of main/master/develop has a merge-base with "
        "the worktree, and the worktree does sit on the base branch. Reached from "
        "initialize_from_worktree (core/workspace/setup.py, merge/tracker_cli.py) "
        "at worktree-creation time. get_changed_files_in_worktree is NOT baseline "
        "capture -- core/workspace.py:_try_smart_merge_inner reaches it from "
        "run.py --merge on the control plane -- and takes a work_ref instead "
        "(#1089)"
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
    # Same walker, same exemption rules -- the two detectors differ only in which
    # argv shape they call a worktree read. One hole, patched in one place.
    return _scan(path, _is_head_range_read)


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
