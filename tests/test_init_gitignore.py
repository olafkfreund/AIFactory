"""Regression tests for ``init.init_magestic_ai_dir`` (#1185).

The control being tested: every managed project's ``.gitignore`` must contain
``.aifactory/``, re-checked on every call. It used to be gated on a
``.aifactory/.gitignore_checked`` marker, so the check ran once per project
ever; if the entry was later dropped (rebase, merge-theirs, manual tidy-up)
nothing put it back and the function still reported ``False``, which is
indistinguishable from "already correct".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from init import init_magestic_ai_dir  # noqa: E402


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    return tmp_path


def test_adds_entry_on_first_call(project: Path) -> None:
    magestic_dir, updated = init_magestic_ai_dir(project)

    assert magestic_dir == project / ".aifactory"
    assert magestic_dir.is_dir()
    assert updated is True
    assert ".aifactory/" in (project / ".gitignore").read_text().splitlines()


def test_second_call_is_a_no_op(project: Path) -> None:
    init_magestic_ai_dir(project)
    before = (project / ".gitignore").read_text()

    _, updated = init_magestic_ai_dir(project)

    assert updated is False
    assert (project / ".gitignore").read_text() == before


def test_re_enforces_after_the_entry_is_removed(project: Path) -> None:
    """The #1185 regression: the marker made this case unreachable.

    Three calls, deliberately. Under the old code the first created the dir
    (and wrote no marker), the second wrote the marker, and only from the
    third onwards was the entry never re-checked. A two-call reproduction
    passes against the bug.
    """
    for _ in range(3):
        init_magestic_ai_dir(project)

    # A rebase / merge-theirs / tidy-up drops the line. .aifactory/ still
    # exists on disk, so the old code took the marker branch and did nothing.
    gitignore = project / ".gitignore"
    gitignore.write_text(
        "\n".join(
            line
            for line in gitignore.read_text().splitlines()
            if line.strip() != ".aifactory/"
        )
        + "\n"
    )
    assert ".aifactory/" not in gitignore.read_text().splitlines()

    _, updated = init_magestic_ai_dir(project)

    assert updated is True, "the entry must be restored, not reported as fine"
    assert ".aifactory/" in gitignore.read_text().splitlines()


def test_no_marker_file_is_written(project: Path) -> None:
    # Twice: the old code only wrote the marker on the call that found the
    # directory already present.
    init_magestic_ai_dir(project)
    init_magestic_ai_dir(project)

    assert not (project / ".aifactory" / ".gitignore_checked").exists()


def test_retired_root_status_file_is_not_injected(project: Path) -> None:
    """`.aifactory-status` moved to `.aifactory/status.json` in #1106; nothing
    writes the root file any more, so it must not be appended to a repo the
    factory does not own."""
    init_magestic_ai_dir(project)

    lines = (project / ".gitignore").read_text().splitlines()
    assert ".aifactory-security.json" in lines
    assert ".aifactory-status" not in lines


def test_creates_gitignore_when_absent(tmp_path: Path) -> None:
    _, updated = init_magestic_ai_dir(tmp_path)

    assert updated is True
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert ".aifactory/" in lines
    assert ".aifactory-security.json" in lines
