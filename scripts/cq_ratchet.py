#!/usr/bin/env python3
"""Diff-scoped code-quality ratchet for AIFactory.

Runs a strict baseline (``standards/ruff.toml`` for ruff, ``standards/mypy.ini``
for mypy) on every Python file a PR changes and fails if a changed file's
violation count went UP versus the PR's base branch (Factory coding standards
section 0: "gates run on the PR diff and may not regress a changed file";
section 4.6: "legacy hotspots are allowed until touched").

A per-file *count* ratchet (rather than line-scoped) is used deliberately: it is
robust to ``ruff format`` reflowing legacy lines (which would make a line-scoped
gate misattribute pre-existing violations to the reformat) while still making it
impossible to ADD a violation to a touched file. Cleaning legacy violations only
ever lowers the count, which is always allowed.

The ruff count is kept PER RULE CODE, not as one number per file (#1189). A
single total nets an added violation off against a removed one, so the gate
reported "improved" on a diff that introduced a new finding — measured on this
very file during Factory#597, where an injected F401 hid behind the 18 findings
the same PR removed (base 18, head 1, exit 0). The three sibling forks
(``scripts/ratchet_lint.py`` in PFactory, TFactory and CFactory) have always
compared a ``Counter`` keyed by rule code; this fork was the odd one out and the
weaker. Netting is precisely what a ratchet must not do: the case it exists for
is the PR that pays down debt and adds a new violation in the same diff.

Two tools are supported:

* ``--tool ruff``  — strict ruff findings per changed file, counted per rule
  code; a file regresses if ANY code's count went up, whatever the total did.
* ``--tool mypy``  — counts mypy ``--strict`` errors attributed to each changed
  file. mypy cannot be line-scoped and the legacy tree is only partially
  annotated, so a whole-tree (or even whole-touched-file at error severity)
  --strict run would turn a formatting-only PR instantly red. Counting per file
  base-vs-head lets a touched legacy file keep its existing mypy debt while
  forbidding NET-NEW type errors — the same no-regression contract ruff uses.
  This half stays a single scalar, as all four forks agree: mypy's blocking
  errors are not rule-keyed the way ruff's are, so it is carried in a one-key
  ``Counter`` purely so both tools share one comparison.

Usage:
    cq_ratchet.py --tool ruff --base <ref> --ruff <bin> --config <ruff.toml> [--paths GLOB ...]
    cq_ratchet.py --tool mypy --base <ref> --mypy <bin> --config <mypy.ini> [--paths GLOB ...]
    cq_ratchet.py --tool ruff --staged --ruff <bin> --config <ruff.toml> [--paths GLOB ...]

``--staged`` gates the files staged in the git index against HEAD instead of a
committed range — the pre-commit hook mode (#1084). File selection comes from
the index; the head side is measured on the working tree, because both sides
must be measured inside a real package tree (#1058) and for a normal commit
the working tree IS the staged content. Under partial staging (git add -p)
unstaged hunks are judged too — CI re-judges the committed range either way.

Exit code 1 if any changed file's violation count increased; else 0.
"""

# T201 (no print) targets SERVICE code, where stdout is not an output channel.
# This is a CI command-line tool whose entire product is what it prints: the
# per-file verdict, the REGRESSION lines and the findings behind them all go to
# the CI log, and the workflow has no other way to show them. Scoped to the one
# rule: the code-less blanket form is what PGH004 forbids, and it would hide the
# next real finding in this file. Same carve-out PFactory's sibling fork carries.
# ruff: noqa: T201

from __future__ import annotations

import argparse
import atexit
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from functools import lru_cache
from pathlib import Path

# Canonical shared ratchet rules, vendored byte-exact from the Factory hub
# and byte-exact drift-gated (Factory#403). scripts/ is sys.path[0] when this
# runs as a script, so the sibling import resolves without packaging.
from ratchet_helpers import (
    MYPY_TEST_RELAX,
    is_test_file,
    require_tool_ran,
    ruff_findings,
    ruff_stdin_argv,
)


# S603 applies to every subprocess call in this file and is suppressed per call,
# never file-wide: the argv are assembled here from repo-relative config paths,
# `git` literals and the CI-supplied ruff/mypy binaries, with no shell and no
# caller string ever becoming a command word. Kept per-line so a genuinely new
# subprocess site still has to argue its own case. This is a CI developer tool,
# not a request-handling surface.
def _run(cmd: list[str], check: bool = True) -> str:
    return subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, check=check
    ).stdout


def changed_python_files(
    base: str, paths: list[str], staged: bool = False
) -> list[str]:
    if staged:
        # Index vs HEAD: what `git commit` is about to record.
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    else:
        cmd = ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD"]
    out = _run([*cmd, "--", *paths])
    excludes = _ruff_excludes()
    return [
        f
        for f in out.split()
        if f.endswith(".py") and Path(f).is_file() and not _is_excluded(f, excludes)
    ]


def _ruff_excludes() -> list[str]:
    """Exclude globs from the repo ruff config (root ``ruff.toml`` + ``extend``).

    Ruff lints whatever it is handed explicitly and applies ``extend-exclude``
    only when it walks the tree itself — so the excludes never match here, and
    that stays true now the base side is judged by its real repo path
    (Factory#510 fixed the per-file-IGNORES, not this). VENDORED MIRRORS —
    the factory-github layer and factory_common, whose fidelity is enforced by
    their own drift gates, not by the local linter — are excluded there; honour
    that here so the ratchet does not gate files ruff is configured to skip.

    Without this, a re-vendor is unmergeable: the canonical carries whatever
    violation count it carries, the ratchet reads that as a regression, and the
    only way to satisfy it is to edit a byte-exact mirror — which is exactly
    what the drift gate exists to prevent (#1028).

    Ported from PFactory's ratchet_lint.py so the two behave the same.
    """
    patterns: list[str] = []
    seen: set[str] = set()
    stack = [Path("ruff.toml")]
    while stack:
        cfg = stack.pop()
        if not cfg.is_file() or str(cfg) in seen:
            continue
        seen.add(str(cfg))
        try:
            data = tomllib.loads(cfg.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for key in ("exclude", "extend-exclude"):
            val = data.get(key)
            if isinstance(val, list):
                patterns.extend(str(x) for x in val)
        extend = data.get("extend")
        if isinstance(extend, str):
            stack.append(cfg.parent / extend)
    return patterns


def _is_excluded(path: str, patterns: list[str]) -> bool:
    """True if ruff is configured to skip *path*.

    Handles a DIRECTORY entry ("apps/backend/runners/github/") as well as an
    exact file, so excluding a vendored tree does not mean listing every file in
    it — a list that silently rots the moment the canonical gains a file.
    """
    for pat in patterns:
        clean = pat.rstrip("/")
        if path == clean or path.startswith(clean + "/"):
            return True
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, f"*/{pat}"):
            return True
    return False


# --------------------------------------------------------------------------- #
# ruff                                                                         #
# --------------------------------------------------------------------------- #


def _ruff_count(
    ruff: str, config: str, file_on_disk: str, repo_path: str
) -> Counter[str]:
    """Violations in *file_on_disk*, judged as though it were at *repo_path*.

    Returns a count PER RULE CODE, not a total (#1189). The JSON already carries
    ``item["code"]``, and keeping the breakdown is the only thing that stops a
    net-new violation being absorbed by an unrelated one the same diff removed.

    The two arguments differ for the BASE side, and that difference is the whole
    point (Factory#510). The base version lives in a worktree under /tmp, and
    ruff relativises a path against the project root before matching
    per-file-ignores — a path outside that root falls back to matching the
    BASENAME only. So ``**/tests/**`` matched the HEAD file and could never match
    its base counterpart, and the two sides of a no-regression comparison were
    measured under DIFFERENT RULES.

    That is not the too-strict failure the other services saw; it is the gate
    going blind. The base count comes back inflated by every S101/PLR2004 the
    carve-out would have exempted, so a file can absorb exactly that many
    genuine NEW violations and still compare clean. Measured on
    apps/web-server/tests/verify_file_based_endpoints.py: head 56, base 60 —
    four free violations.

    ``--stdin-filename`` fixes it by decoupling the two: the CONTENT still comes
    from the worktree (which is why the worktree exists — see _base_worktree),
    while the PATH ruff judges by is the real repo-relative one, identical on
    both sides. mypy needs no equivalent: its carve-out is decided by
    :func:`is_test_file`, which matches ``/tests/`` anywhere in the path and so
    was already symmetric.
    """
    # The canonical argv names a bare `ruff`; this service always passes an
    # explicit binary (the pinned venv), so argv[0] is replaced. --no-fix is kept
    # from the previous invocation and matters MORE on stdin than it did on a
    # path: with `fix = true` ever set in the config, ruff writes the fixed
    # source to stdout and `ruff_findings` below would read code as findings.
    argv = ruff_stdin_argv(config, repo_path)
    res = subprocess.run(  # noqa: S603
        [ruff, argv[1], "--no-fix", *argv[2:]],
        capture_output=True,
        text=True,
        check=False,
        input=Path(file_on_disk).read_text(),
    )
    # The shared "is this run a measurement" rule, both halves. #1174 fixed the
    # second half HERE because the canonical is byte-exact and cannot be edited
    # from a service repo; Factory#648 lifted it into that canonical, so the
    # local guard is deleted and this fork now asks one question once:
    #
    #   * did ruff exit for its own reasons (Factory#590), and
    #   * is what ruff WROTE a finding list (#1174, Factory#648) -- empty stdout
    #     is not a clean run, the pinned ruff prints `[]`; unparseable stdout is
    #     the `fix = true` case --no-fix above exists for.
    return ruff_findings(res)


# --------------------------------------------------------------------------- #
# mypy                                                                         #
# --------------------------------------------------------------------------- #

# mypy text output lines look like:  path/to/file.py:12: error: <msg>  [code]
_MYPY_ERROR_RE = re.compile(r"^(?P<path>.+?):\d+: error:")


def _cache_dir_for(file_on_disk: str) -> str:
    """A mypy cache directory unique to the tree *file_on_disk* lives in."""
    root = Path(file_on_disk).resolve()
    for parent in root.parents:
        if (parent / ".git").exists():
            return str(parent / ".mypy_cache")
    return str(Path(tempfile.gettempdir()) / "cq-ratchet-mypy-cache")


def interpreter_target() -> str:
    """The ``--python-version`` this gate must target: the venv it checks against.

    The shared ``standards/mypy.ini`` declares ``python_version = 3.11``. That is
    correct for the hub baseline — it is the fleet FLOOR (coding-standards.md
    section 1, "Python (3.11+)"), the hub's own ratchet still builds 3.11, and
    the fleet really is mixed (Factory 3.11, PFactory/AIFactory/TFactory 3.12,
    CFactory 3.13), so raising it centrally would raise the floor for everyone.
    Nothing in ``standards/`` should move.

    It is wrong as THIS gate's target. cq-ratchet.yml builds the venv on 3.12,
    ``apps/backend/requirements.txt`` pulls ``graphiti-core`` (hence numpy) on
    3.12, and numpy's stubs there use PEP 695 ``type`` statements. Told to target
    3.11, mypy refuses to parse them::

        .venv/lib/python3.12/site-packages/numpy/__init__.pyi:737: error: Type
        statement is only supported in Python 3.12 and greater  [syntax]

    and exits 2 having checked nothing. Every file that reaches numpy
    transitively is then either hard-failed by ``require_tool_ran`` or — worse —
    silently under-counted, when the file emits one unrelated error of its own
    and so satisfies that guard's ``measured`` arm (#1188; PFactory#467 was the
    same defect, and its silent half was the damaging one).

    Derived from the running interpreter rather than written as ``3.12``, because
    a literal is exactly how ``3.11`` went stale: the venv moves and the target
    does not. The workflow runs this script with ``apps/backend/.venv/bin/python``
    and passes ``--mypy apps/backend/.venv/bin/mypy``, so the interpreter under
    this process IS the one whose site-packages mypy reads.

    Not a loosening under the tighten-only rule: every strict flag in the shared
    baseline still applies, unchanged. Only the syntax/stdlib level moves, and it
    moves to the one actually in use — which is what mypy would default to on its
    own were the baseline not naming a version.
    """
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _mypy_count(mypy: str, config: str, file_on_disk: str) -> int:
    """Count mypy --strict errors attributed to ``file_on_disk``.

    ``--follow-imports=silent`` keeps mypy from reporting errors in imported
    legacy modules the changed file merely references, and
    ``--ignore-missing-imports`` stops third-party stub gaps from inflating the
    count — the strict bar still applies to the file's own annotations. Only
    error lines whose path matches the file we asked about are counted, so an
    error surfaced in a followed dependency never lands on this file's ledger.
    """
    res = subprocess.run(  # noqa: S603
        [
            mypy,
            "--config-file",
            config,
            # The venv's Python, not the baseline's fleet floor — see
            # interpreter_target (#1188).
            "--python-version",
            interpreter_target(),
            "--ignore-missing-imports",
            "--follow-imports=silent",
            # Separate cache per tree. The base and head copies of a file
            # share a module name, so ONE shared incremental cache lets one
            # side's result stand in for the other's -- locally that produced
            # base counts of 9 and then 0 for the same command on the same
            # tree (#1057). Keying the cache to the tree being measured keeps
            # the counts deterministic without paying for --no-incremental,
            # which was ~5x slower on this repo.
            "--cache-dir",
            _cache_dir_for(file_on_disk),
            "--no-error-summary",
            "--no-color-output",
            "--hide-error-context",
            *(MYPY_TEST_RELAX if is_test_file(file_on_disk) else []),
            file_on_disk,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    target = str(Path(file_on_disk).resolve())
    count = 0
    for line in (res.stdout + res.stderr).splitlines():
        m = _MYPY_ERROR_RE.match(line)
        if not m:
            continue
        # mypy prints the path it was handed; compare resolved forms so the
        # temp-file path (base) and the on-disk path (head) both match.
        try:
            if str(Path(m.group("path")).resolve()) == target:
                count += 1
        except OSError:
            # base-version errors point at a temp file that is the only path
            # mypy was given, so any error line is this file's.
            count += 1
    # Same shared rule as the ruff counter, with `measured` passed: mypy's exit 2
    # also covers a BLOCKING error, which still names a file and so belongs in the
    # count rather than aborting the run.
    require_tool_ran("mypy", res, measured=count)
    return count


# --------------------------------------------------------------------------- #
# shared base-version counting                                                 #
# --------------------------------------------------------------------------- #


def _worktree_env() -> dict[str, str]:
    """Environment for `git worktree add/remove`, minus git's hook exports.

    Inside a pre-commit hook git exports GIT_INDEX_FILE (and often GIT_DIR),
    and a worktree add that inherits them populates the CALLER'S index with
    the base tree -- the staged changes silently vanish and the commit being
    gated becomes empty. Only these two subprocesses get the stripped env;
    the index-reading commands (`git diff --cached`) must keep the exports so
    they see exactly what is being committed.
    """
    env = os.environ.copy()
    for key in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_PREFIX"):
        env.pop(key, None)
    return env


@lru_cache(maxsize=1)
def _base_worktree(base: str) -> str | None:
    """A detached git worktree of *base*, created once per run.

    The base version MUST be measured inside a real package tree. The previous
    implementation copied the file alone into a bare temp directory, where its
    ``from ..config import ...`` / ``from .projects import ...`` no longer
    resolved, so the checker saw a fraction of the real type surface. That made
    the comparison out-of-package-base versus in-package-head, and any edit to
    a file with relative imports reported a large phantom regression -- 4 vs 88
    on projects.py, for a change that adds none (#1057).

    One worktree per invocation, not per file, and the working tree is never
    mutated.
    """
    tmp = tempfile.mkdtemp(prefix="cq-ratchet-base-")
    # S607: `git` stays a bare name so PATH decides, exactly as every other git
    # call in this repo's tooling does; pinning an absolute path here would make
    # the ratchet unrunnable on any host that installs git elsewhere.
    res = subprocess.run(  # noqa: S603
        ["git", "worktree", "add", "--detach", "-q", tmp, base],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env=_worktree_env(),
    )
    if res.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    atexit.register(_remove_worktree, tmp)
    return tmp


def _remove_worktree(path: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "worktree", "remove", "--force", path],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env=_worktree_env(),
    )
    shutil.rmtree(path, ignore_errors=True)


def base_count(base: str, counter, config: str, path: str) -> Counter[str]:
    """Violation counts for ``path`` as it exists on ``base`` (empty if new).

    ``counter`` is a callable ``(config, file_on_disk, repo_path) ->
    Counter[str]``. The two paths are the same thing on the head side and
    deliberately different here: content from the worktree, identity from the
    repo. A counter that judged the file by ``file_on_disk`` would measure the
    two sides of the comparison under different rules (Factory#510).
    """
    worktree = _base_worktree(base)
    if worktree is None:
        # No worktree (shallow clone, detached oddity). Treating the base as 0
        # would report every pre-existing violation as net-new, so refuse to
        # guess: a gate that cannot measure its baseline must say so.
        raise RuntimeError(
            f"cannot create a base worktree at {base!r}; "
            "the ratchet cannot measure a baseline without one"
        )
    candidate = Path(worktree) / path
    if not candidate.is_file():
        # File did not exist on base -> new file, every code's base count is 0.
        return Counter()
    return counter(config, str(candidate), path)


def regressed_codes(before: Counter[str], after: Counter[str]) -> list[str]:
    """Rule codes whose count went UP, one formatted line each (empty if none).

    THE gate rule (#1189). Comparing per code rather than on the total is what
    stops a net-new violation being paid for with an unrelated one the same diff
    removed — the case a ratchet exists for, and the one the previous
    ``after > before`` on two ints got wrong. Ported from the three sibling forks
    (``scripts/ratchet_lint.py::regressions``), deliberately not reinvented:
    this rule family has already cost five PRs through independent restatement
    (Factory#590).
    """
    return [
        f"{code} +{n - before[code]} (base {before[code]} -> head {n})"
        for code, n in sorted(after.items())
        if n > before[code]
    ]


def _report_regressions(
    args: argparse.Namespace,
    label: str,
    regressions: list[tuple[str, Counter[str], Counter[str]]],
) -> None:
    """Print each regression and re-run the tool so the findings are visible.

    Split out of ``main`` (Factory#597): the reporting tail is the only part of
    ``main`` that branches on ``--tool`` a second time, and lifting it takes the
    branch count back under the shared PLR0912 cap. It also puts the two raw
    subprocess sites in one place instead of buried at the end of the entry
    point.

    The totals stay in the headline because they are what a reader looks for
    first, but the per-code lines below them are the verdict: a file can regress
    while its TOTAL falls, and printing only the total would describe such a
    failure as an improvement.
    """
    for path, before, after in regressions:
        print(
            f"REGRESSION {path}: {label} violations "
            f"{sum(before.values())} -> {sum(after.values())}"
        )
        for line in regressed_codes(before, after):
            print(f"  {line}")
        # Show the actual findings to make the failure actionable.
        if args.tool == "ruff":
            subprocess.run(  # noqa: S603
                [args.ruff, "check", "--no-fix", "--config", args.config, path],
                check=False,
            )
        else:
            subprocess.run(  # noqa: S603
                [
                    args.mypy,
                    "--config-file",
                    args.config,
                    "--python-version",
                    interpreter_target(),
                    "--ignore-missing-imports",
                    "--follow-imports=silent",
                    "--no-error-summary",
                    *(MYPY_TEST_RELAX if is_test_file(path) else []),
                    path,
                ],
                check=False,
            )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", choices=["ruff", "mypy"], default="ruff")
    ap.add_argument("--base")
    ap.add_argument(
        "--staged",
        action="store_true",
        help="gate the staged index against HEAD (pre-commit hook mode)",
    )
    ap.add_argument("--ruff", help="ruff binary (required for --tool ruff)")
    ap.add_argument("--mypy", help="mypy binary (required for --tool mypy)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--paths", nargs="*", default=["apps/backend/*.py"])
    args = ap.parse_args()

    if args.staged:
        args.base = "HEAD"
    elif not args.base:
        ap.error("--base is required unless --staged is given")

    if args.tool == "ruff":
        if not args.ruff:
            ap.error("--ruff is required for --tool ruff")

        def counter(config: str, f: str, repo_path: str) -> Counter[str]:
            return _ruff_count(args.ruff, config, f, repo_path)

        label = "strict ruff"
    else:
        if not args.mypy:
            ap.error("--mypy is required for --tool mypy")

        def counter(config: str, f: str, repo_path: str) -> Counter[str]:  # noqa: ARG001
            # repo_path is unused: mypy's test carve-out is decided by
            # is_test_file, which matches "/tests/" anywhere in the path, so the
            # worktree copy and the real file were always classified alike.
            #
            # One bucket, not one per mypy error code: this half is a scalar in
            # all four sibling forks (#1189), and the Counter is only the shared
            # carrier so both tools go through regressed_codes unchanged.
            return Counter({"mypy": _mypy_count(args.mypy, config, f)})

        label = "mypy --strict"

    files = changed_python_files(args.base, args.paths, staged=args.staged)
    if not files:
        print(f"cq-ratchet ({args.tool}): no changed Python files in scope")
        return 0

    regressions: list[tuple[str, Counter[str], Counter[str]]] = []
    summary: Counter[str] = Counter()
    for path in files:
        before = base_count(args.base, counter, args.config, path)
        # Head side: the file IS at its repo path, so the two coincide.
        after = counter(args.config, path, path)
        # Any code going up is a regression, EVEN IF the total fell (#1189).
        # The improved/unchanged split below is only cosmetic and stays on the
        # totals; the verdict never is.
        if regressed_codes(before, after):
            regressions.append((path, before, after))
        elif sum(after.values()) < sum(before.values()):
            summary["improved"] += 1
        else:
            summary["unchanged"] += 1

    print(
        f"cq-ratchet ({args.tool}): {len(files)} changed file(s) checked against "
        f"the {label} baseline ({summary['improved']} improved, "
        f"{summary['unchanged']} unchanged, {len(regressions)} regressed)"
    )
    _report_regressions(args, label, regressions)
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
