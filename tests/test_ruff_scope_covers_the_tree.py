"""Every Python file in the repo is inside a ruff gate (#1205, #1179).

Both defects were the same one: a gate whose stated scope was wider than its
actual scope, and nothing that noticed.

* ``ruff check`` ran over ``apps/backend tests`` while the pytest steps had been
  extended to ``apps/web-server`` (#903). The whole web-server tree -- routes,
  services, websockets, its co-located tests -- had never been linted by CI, and
  carried 142 findings.
* ``ruff format --check`` ran over ``apps/backend scripts`` under a job named
  "repo-wide". ``tests/`` was linted and never format-checked, and 26 files had
  drifted.

Both were found by a human reading the workflow, which is not a control. These
tests read the workflow files and compare their scope to what is actually on
disk, so a new top-level Python directory cannot land outside the gates in
silence -- the way ``apps/web-server`` did.

Deliberately NOT asserting a hardcoded list of directories: that is the same
defect one level up, a written-down scope nobody re-derives. The tree is the
source of truth and the workflow is checked against it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO / ".github" / "workflows"

# scripts/ is linted by the diff-scoped ratchet (cq-ratchet.yml --paths) and
# format-checked below, but is NOT in ci.yml's repo-wide `ruff check`: the root
# and strict ruff configs disagree about its import blocks and each one's
# autofix is the other's violation (#1211). Delete this exemption when #1211
# lands -- an exemption that outlives its issue is a permanent hole.
_LINT_EXEMPT = {"scripts"}


def _tracked_python_roots() -> set[str]:
    """Top-level directories that contain tracked ``.py`` files.

    ``apps`` is reported one level deeper because that is how the workflows name
    it (``apps/backend``, ``apps/web-server``) -- and how they came to disagree.
    """
    out = subprocess.run(  # noqa: S603
        ["git", "ls-files", "*.py"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    roots: set[str] = set()
    for path in out:
        parts = path.split("/")
        assert len(parts) > 1, (
            f"{path} is a Python file at the repo root, outside every ruff scope. "
            "Move it under apps/, scripts/ or tests/, or widen both gates."
        )
        roots.add("/".join(parts[:2]) if parts[0] == "apps" else parts[0])
    return roots


def _scope_of(workflow: str, command: str) -> set[str]:
    """The paths *command* is invoked with in *workflow*.

    Anchored on ``run:`` so it reads the STEP and not a comment describing it.
    Without the anchor this matched the header comment above the job, which is
    prose about the scope rather than the scope -- the same
    trust-the-comment mistake #1162 is about, made by the test meant to stop it.
    """
    text = (_WORKFLOWS / workflow).read_text(encoding="utf-8")
    match = re.search(
        rf"^\s*run: \S*{re.escape(command)}((?: +[\w./-]+)+)$", text, re.M
    )
    assert match is not None, (
        f"could not find a `run:` step invoking `{command}` in {workflow}"
    )
    return set(match.group(1).split())


def test_ruff_format_check_covers_every_python_directory() -> None:
    scope = _scope_of("cq-ratchet.yml", "ruff format --check")
    missing = _tracked_python_roots() - scope
    assert not missing, (
        f"these directories hold Python and are not format-checked: {sorted(missing)}. "
        "A whole-tree format gate cannot grandfather anything, so widen the scope "
        "and reformat in the same change (#1179)."
    )


def test_ruff_check_covers_every_python_directory() -> None:
    scope = _scope_of("ci.yml", "ruff check")
    missing = _tracked_python_roots() - scope - _LINT_EXEMPT
    assert not missing, (
        f"these directories hold Python and are not linted by CI: {sorted(missing)}. "
        "The diff-scoped ratchet only gates CHANGED files, which is not the same "
        "guarantee (#1205)."
    )


def test_the_format_job_name_matches_the_scope_it_runs() -> None:
    """A job name is a claim, and #1179 is what an unchecked claim costs.

    The job called itself "repo-wide" while checking one directory of four.
    """
    text = (_WORKFLOWS / "cq-ratchet.yml").read_text(encoding="utf-8")
    match = re.search(r"^\s*name: (ruff format --check.*)$", text, re.M)
    assert match is not None, "the format-check job must be named"
    name = match.group(1)
    claims_everything = "every Python" in name or "repo-wide" in name or "whole" in name
    covers_everything = not (
        _tracked_python_roots() - _scope_of("cq-ratchet.yml", "ruff format --check")
    )
    assert claims_everything == covers_everything, (
        f"the format-check job is named {name!r}, which does not match what it "
        "runs. Either widen the scope or rename the job."
    )
