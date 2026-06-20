#!/usr/bin/env python3
"""Diff-scoped code-quality ratchet for AIFactory.

Runs the fleet-wide strict ruff baseline (``standards/ruff.toml``) on every
Python file a PR changes and fails if a changed file's strict-violation count
went UP versus the PR's base branch (Factory coding standards section 0: "gates
run on the PR diff and may not regress a changed file"; section 4.6: "legacy
hotspots are allowed until touched").

A per-file *count* ratchet (rather than line-scoped) is used deliberately: it is
robust to ``ruff format`` reflowing legacy lines (which would make a line-scoped
gate misattribute pre-existing violations to the reformat) while still making it
impossible to ADD a strict violation to a touched file. Cleaning legacy
violations only ever lowers the count, which is always allowed.

Usage:
    cq_ratchet.py --base <ref> --ruff <ruff-bin> --config <ruff.toml> [--paths GLOB ...]

Exit code 1 if any changed file's strict-violation count increased; else 0.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path


def _run(cmd: list[str], check: bool = True) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=check).stdout


def changed_python_files(base: str, paths: list[str]) -> list[str]:
    out = _run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD", "--", *paths]
    )
    return [f for f in out.split() if f.endswith(".py") and Path(f).is_file()]


def _ruff_count(ruff: str, config: str, file_on_disk: str) -> int:
    res = subprocess.run(
        [ruff, "check", "--no-fix", "--config", config, "--output-format", "json", file_on_disk],
        capture_output=True,
        text=True,
    )
    return len(json.loads(res.stdout)) if res.stdout.strip() else 0


def base_count(base: str, ruff: str, config: str, path: str) -> int:
    """Strict-violation count for ``path`` as it exists on ``base`` (0 if new)."""
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
        return _ruff_count(ruff, config, tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--ruff", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--paths", nargs="*", default=["apps/backend/**/*.py"])
    args = ap.parse_args()

    files = changed_python_files(args.base, args.paths)
    if not files:
        print("cq-ratchet: no changed Python files in scope")
        return 0

    regressions: list[tuple[str, int, int]] = []
    summary: Counter[str] = Counter()
    for path in files:
        before = base_count(args.base, args.ruff, args.config, path)
        after = _ruff_count(args.ruff, args.config, path)
        if after > before:
            regressions.append((path, before, after))
        elif after < before:
            summary["improved"] += 1
        else:
            summary["unchanged"] += 1

    print(
        f"cq-ratchet: {len(files)} changed file(s) checked against the strict "
        f"baseline ({summary['improved']} improved, {summary['unchanged']} "
        f"unchanged, {len(regressions)} regressed)"
    )
    for path, before, after in regressions:
        print(f"REGRESSION {path}: strict violations {before} -> {after}")
        # Show the actual new findings to make the failure actionable.
        subprocess.run(
            [args.ruff, "check", "--no-fix", "--config", args.config, path], check=False
        )
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
