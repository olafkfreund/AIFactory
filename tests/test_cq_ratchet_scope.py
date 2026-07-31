"""The ratchet's pathspec must actually reach every backend Python file (#1124).

`--paths` is handed straight to `git diff`/`git ls-files` as a **pathspec**, not
to fnmatch. In a git pathspec `*` already spans `/`, so `apps/backend/*.py`
matches at any depth -- while `apps/backend/**/*.py` requires at least one
intervening directory and therefore silently excludes every `.py` sitting
directly in `apps/backend/`.

That was 40 files, including `agent.py`, `client.py`, `model_registry.py` and
`prompts.py`, ungated by ruff AND mypy in both pre-commit and CI. It reported
success while doing so:

    Running cq ratchet (ruff, staged vs HEAD)...
    cq-ratchet (ruff): no changed Python files in scope

which is the exact shape the #1084 line of work exists to stamp out -- a gate
that did not run, reading like one that passed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _ls_files(*pathspecs: str) -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", *pathspecs],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in out.stdout.splitlines() if line}


def test_pathspec_covers_files_directly_under_apps_backend() -> None:
    """The regression itself: top-level backend modules must be in scope."""
    top_level = {
        f
        for f in _ls_files("apps/backend/*.py")
        if f.count("/") == 2  # apps/backend/<name>.py
    }
    assert top_level, "expected some .py directly under apps/backend/"

    double_star = _ls_files("apps/backend/**/*.py")
    missed = top_level - double_star
    assert missed == top_level, (
        "sanity: '**/*.py' is expected to miss ALL top-level files; git pathspec "
        "semantics may have changed"
    )

    # The pathspec the gates actually use must lose none of them.
    assert not (top_level - _ls_files("apps/backend/*.py"))


def test_gate_configs_do_not_use_the_double_star_pathspec() -> None:
    """pre-commit, CI and the script default must all use the working form."""
    offenders = []
    for rel in (
        "scripts/cq_ratchet.py",
        ".github/workflows/cq-ratchet.yml",
        ".husky/pre-commit",
    ):
        text = (_REPO / rel).read_text(encoding="utf-8")
        if "apps/backend/**/*.py" in text or "apps/web-server/**/*.py" in text:
            offenders.append(rel)
    assert not offenders, (
        "these gate configs use a pathspec that silently excludes files directly "
        f"under the package root: {offenders}"
    )
