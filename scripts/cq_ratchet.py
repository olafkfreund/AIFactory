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

Exit code 1 if any changed file's violation count increased; else 0.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path


def _run(cmd: list[str], check: bool = True) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=check).stdout


def changed_python_files(base: str, paths: list[str]) -> list[str]:
    out = _run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            f"{base}...HEAD",
            "--",
            *paths,
        ]
    )
    return [f for f in out.split() if f.endswith(".py") and Path(f).is_file()]


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
            "--no-error-summary",
            "--no-color-output",
            "--hide-error-context",
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


def base_count(base: str, counter, config: str, path: str) -> int:
    """Violation count for ``path`` as it exists on ``base`` (0 if new).

    ``counter`` is a callable ``(config, file_on_disk) -> int``.
    """
    blob = subprocess.run(
        ["git", "show", f"{base}:{path}"], capture_output=True, text=True
    )
    if blob.returncode != 0:
        return 0  # file did not exist on base -> new file, base count is 0
    suffix = "".join(Path(path).suffixes) or ".py"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as tmp:
        tmp.write(blob.stdout)
        tmp_path = tmp.name
    try:
        return counter(config, tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", choices=["ruff", "mypy"], default="ruff")
    ap.add_argument("--base", required=True)
    ap.add_argument("--ruff", help="ruff binary (required for --tool ruff)")
    ap.add_argument("--mypy", help="mypy binary (required for --tool mypy)")
    ap.add_argument("--config", required=True)
    ap.add_argument("--paths", nargs="*", default=["apps/backend/**/*.py"])
    args = ap.parse_args()

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

    files = changed_python_files(args.base, args.paths)
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
                    path,
                ],
                check=False,
            )
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
