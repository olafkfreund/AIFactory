"""Assertion-pinning guard for the handback fix cycle (#467 / TFactory#283).

TFactory pins its verification assertions across handback rounds (TFactory#283)
so each round tests against the *same* bar. AIFactory must honour that on its
side: within a single fix cycle the coder/QA-fixer may add tests but must **not
weaken or delete** AIFactory's own test assertions to make a red suite go green.

This module is the diff-gate. It snapshots a per-file assertion count of the
project's test files before the fixer runs and compares after: a file that lost
assertions — or vanished — is flagged. It is intentionally a *heuristic* (a
robust, language-spanning assertion-token count), not a semantic prover: the
issue's bar is "additive-only, or flag dropped/loosened assertions for review",
and a count regression is exactly that signal. Stdlib-only; never raises.

Note the scope boundary: AIFactory's fixer only ever has its *own* project tree
checked out, never TFactory's verification suite — so "don't touch TFactory's
tests" holds by construction. This guard protects AIFactory's *internal* Act
loop (coder → qa_reviewer → qa_fixer).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

__all__ = [
    "count_assertions",
    "snapshot_test_assertions",
    "guard_assertion_manifest",
    "AssertionViolation",
    "GuardReport",
]

# Test files we account for, across the stacks AIFactory builds in.
_TEST_GLOBS = (
    "**/test_*.py",
    "**/*_test.py",
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.test.js",
    "**/*.test.jsx",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    "**/*.spec.js",
    "**/*.spec.jsx",
)

# Directories never worth walking — keeps the snapshot fast and noise-free.
_PRUNE_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".worktrees",
        "dist",
        "build",
        ".aifactory",
        ".mypy_cache",
        ".pytest_cache",
    }
)

# Assertion tokens across pytest/unittest (Python) and jest/vitest/chai (JS/TS).
_ASSERTION_PATTERNS = (
    r"(?<![\w.])assert(?![\w(.])",  # python bare `assert ...` (not assert(/assert.)
    r"\bself\.assert\w+\s*\(",  # unittest self.assertEqual(...)
    r"\bpytest\.raises\b",  # context-manager assertions
    r"\.assert_(?:called|awaited|not_called)\w*\s*\(",  # mock assertions
    r"\bexpect\s*\(",  # jest / vitest / chai expect(...)
    r"\bassert\s*\(",  # node:assert / chai assert(...)
    r"\bassert\.\w+\s*\(",  # assert.equal(...)
)
_ASSERTION_RE = re.compile("|".join(_ASSERTION_PATTERNS))


@dataclass(frozen=True)
class AssertionViolation:
    path: str
    kind: str  # "file_removed" | "assertions_reduced"
    before: int
    after: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GuardReport:
    ok: bool
    violations: list[AssertionViolation] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "violations": [v.to_dict() for v in self.violations]}


def count_assertions(source: str) -> int:
    """Heuristic count of assertion statements in a test-file source string."""
    if not source:
        return 0
    return len(_ASSERTION_RE.findall(source))


def snapshot_test_assertions(root: Path | str) -> dict[str, int]:
    """Map every test file under ``root`` to its assertion count.

    Keys are POSIX-style paths relative to ``root`` so the snapshot is stable
    across machines/worktrees. Unreadable files are skipped. Best-effort.
    """
    base = Path(root)
    if not base.is_dir():
        return {}
    seen: dict[str, int] = {}
    for pattern in _TEST_GLOBS:
        for fp in base.glob(pattern):
            if not fp.is_file():
                continue
            if _PRUNE_DIRS & set(fp.relative_to(base).parts):
                continue
            rel = fp.relative_to(base).as_posix()
            if rel in seen:
                continue
            try:
                seen[rel] = count_assertions(
                    fp.read_text(encoding="utf-8", errors="ignore")
                )
            except OSError:
                continue
    return seen


def guard_assertion_manifest(
    before: dict[str, int], after: dict[str, int]
) -> GuardReport:
    """Flag any previously-existing test file that lost assertions or vanished.

    Additive-only is the bar: new files and higher counts are fine; a dropped
    file or a reduced count is a violation to surface for review.
    """
    violations: list[AssertionViolation] = []
    for path, before_count in before.items():
        after_count = after.get(path)
        if after_count is None:
            violations.append(AssertionViolation(path, "file_removed", before_count, 0))
        elif after_count < before_count:
            violations.append(
                AssertionViolation(
                    path, "assertions_reduced", before_count, after_count
                )
            )
    return GuardReport(ok=not violations, violations=violations)
