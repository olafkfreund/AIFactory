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

Two tools are supported:

* ``--tool ruff``  — counts strict ruff findings per changed file.
* ``--tool mypy``  — counts mypy ``--strict`` errors attributed to each changed
  file. mypy cannot be line-scoped and the legacy tree is only partially
  annotated, so a whole-tree (or even whole-touched-file at error severity)
  --strict run would turn a formatting-only PR instantly red. Counting per file
  base-vs-head lets a touched legacy file keep its existing mypy debt while
  forbidding NET-NEW type errors — the same no-regression contract ruff uses.

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

from __future__ import annotations

import argparse
import atexit
import fnmatch
import json
import os
import re
import subprocess
import sys
import shutil
import tempfile
from collections import Counter
from functools import lru_cache
from pathlib import Path

import tomllib

# Canonical shared ratchet rules, vendored byte-exact from the Factory hub
# and byte-exact drift-gated (Factory#403). scripts/ is sys.path[0] when this
# runs as a script, so the sibling import resolves without packaging.
from ratchet_helpers import MYPY_TEST_RELAX, is_test_file


def _run(cmd: list[str], check: bool = True) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=check).stdout


def changed_python_files(base: str, paths: list[str], staged: bool = False) -> list[str]:
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

    The ratchet writes each changed file to a temp path before checking, so
    ruff's own path-based ``extend-exclude`` never matches. VENDORED MIRRORS —
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


def _ruff_count(ruff: str, config: str, file_on_disk: str) -> int:
    res = subprocess.run(
        [
            ruff,
            "check",
            "--no-fix",
            "--config",
            config,
            "--output-format",
            "json",
            file_on_disk,
        ],
        capture_output=True,
        text=True,
    )
    return len(json.loads(res.stdout)) if res.stdout.strip() else 0


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


def _mypy_count(mypy: str, config: str, file_on_disk: str) -> int:
    """Count mypy --strict errors attributed to ``file_on_disk``.

    ``--follow-imports=silent`` keeps mypy from reporting errors in imported
    legacy modules the changed file merely references, and
    ``--ignore-missing-imports`` stops third-party stub gaps from inflating the
    count — the strict bar still applies to the file's own annotations. Only
    error lines whose path matches the file we asked about are counted, so an
    error surfaced in a followed dependency never lands on this file's ledger.
    """
    res = subprocess.run(
        [
            mypy,
            "--config-file",
            config,
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
    res = subprocess.run(
        ["git", "worktree", "add", "--detach", "-q", tmp, base],
        capture_output=True,
        text=True,
        env=_worktree_env(),
    )
    if res.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    atexit.register(_remove_worktree, tmp)
    return tmp


def _remove_worktree(path: str) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", path],
        capture_output=True,
        text=True,
        env=_worktree_env(),
    )
    shutil.rmtree(path, ignore_errors=True)


def base_count(base: str, counter, config: str, path: str) -> int:
    """Violation count for ``path`` as it exists on ``base`` (0 if new).

    ``counter`` is a callable ``(config, file_on_disk) -> int``.
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
        return 0  # file did not exist on base -> new file, base count is 0
    return counter(config, str(candidate))


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

        def counter(config: str, f: str) -> int:
            return _ruff_count(args.ruff, config, f)

        label = "strict ruff"
    else:
        if not args.mypy:
            ap.error("--mypy is required for --tool mypy")

        def counter(config: str, f: str) -> int:
            return _mypy_count(args.mypy, config, f)

        label = "mypy --strict"

    files = changed_python_files(args.base, args.paths, staged=args.staged)
    if not files:
        print(f"cq-ratchet ({args.tool}): no changed Python files in scope")
        return 0

    regressions: list[tuple[str, int, int]] = []
    summary: Counter[str] = Counter()
    for path in files:
        before = base_count(args.base, counter, args.config, path)
        after = counter(args.config, path)
        if after > before:
            regressions.append((path, before, after))
        elif after < before:
            summary["improved"] += 1
        else:
            summary["unchanged"] += 1

    print(
        f"cq-ratchet ({args.tool}): {len(files)} changed file(s) checked against "
        f"the {label} baseline ({summary['improved']} improved, "
        f"{summary['unchanged']} unchanged, {len(regressions)} regressed)"
    )
    for path, before, after in regressions:
        print(f"REGRESSION {path}: {label} violations {before} -> {after}")
        # Show the actual findings to make the failure actionable.
        if args.tool == "ruff":
            subprocess.run(
                [args.ruff, "check", "--no-fix", "--config", args.config, path],
                check=False,
            )
        else:
            subprocess.run(
                [
                    args.mypy,
                    "--config-file",
                    args.config,
                    "--ignore-missing-imports",
                    "--follow-imports=silent",
                    "--no-error-summary",
                    *(MYPY_TEST_RELAX if is_test_file(path) else []),
                    path,
                ],
                check=False,
            )
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
