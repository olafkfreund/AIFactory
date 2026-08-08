"""The ruff pin must be the same everywhere it is written -- and enforced (#1162).

`cq-ratchet.yml` calls `RUFF_VERSION` the toolchain pin and says it "matches the
ruff-pre-commit hook so local and CI agree byte-for-byte on lint + format".
Nothing enforced that, and the sentence was only ever half true: three FILES
declared 0.14.10 while the hook that actually runs locally
(`.husky/pre-commit`) resolved ruff from the venv or PATH and never looked at a
version at all. Measured while #1162 was open: venv 0.15.14, PATH 0.15.17, CI
0.14.10.

Formatting rules change between ruff minors, and the hook does not merely CHECK
formatting -- it rewrites staged files with whatever ruff it found and `git
add`s the result. So a skew lands a diff nobody intended and CI rejects it,
with the blame landing on the change rather than the toolchain. PFactory hit
the declared half of this in their #417 and #452; the test below is theirs,
extended with the half that bit here: the pin must reach the tool that runs.

These read the files rather than trusting the comment, because the comment was
already there and was already incomplete.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_HOOK = _REPO / ".husky" / "pre-commit"


def _ci_ruff_versions() -> dict[str, str]:
    """`RUFF_VERSION` as declared by each workflow that declares one."""
    found: dict[str, str] = {}
    for wf in (_REPO / ".github" / "workflows").glob("*.yml"):
        match = re.search(
            r'^\s*RUFF_VERSION:\s*"?([0-9][^"\s]*)"?',
            wf.read_text(encoding="utf-8"),
            re.M,
        )
        if match:
            found[wf.name] = match.group(1)
    return found


def _the_ci_pin() -> str:
    versions = set(_ci_ruff_versions().values())
    assert len(versions) == 1, f"workflows disagree about the ruff pin: {versions}"
    return versions.pop()


def test_every_ci_workflow_declares_the_same_ruff() -> None:
    versions = _ci_ruff_versions()
    assert versions, "expected at least one workflow to declare RUFF_VERSION"
    assert len(set(versions.values())) == 1, (
        f"workflows disagree about the ruff pin: {versions}"
    )


def test_precommit_config_runs_the_same_ruff_as_ci() -> None:
    """The local hook must be a preview of the gate, not a different tool."""
    text = (_REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    match = re.search(r"ruff-pre-commit\s*\n\s*rev:\s*v?([0-9][^\s]*)", text)
    assert match is not None, "could not find the ruff-pre-commit rev"
    assert match.group(1) == _the_ci_pin(), (
        f"pre-commit pins ruff {match.group(1)} while CI pins {_the_ci_pin()}. A "
        "file formatted clean by the hook can arrive red in CI."
    )


def test_the_test_venv_installs_the_same_ruff_as_ci() -> None:
    """`tests/test_cq_ratchet_test_bar.py` shells out to ruff to prove the gate.

    Measuring the gate with a different ruff than the gate uses is the same
    two-tools-disagreeing shape one layer down.
    """
    text = (_REPO / "tests" / "requirements-test.txt").read_text(encoding="utf-8")
    match = re.search(r"^ruff==([0-9][^\s]*)", text, re.M)
    assert match is not None, "tests/requirements-test.txt must pin ruff"
    assert match.group(1) == _the_ci_pin(), (
        f"requirements-test.txt pins ruff {match.group(1)}, CI pins {_the_ci_pin()}"
    )


# ── the pin has to reach the tool that actually runs (#1162) ────────────────


def _hook_pin_command() -> str:
    """The hook's own `RUFF_PIN=$(...)` line, verbatim.

    Read out of the hook rather than restated, so this test cannot pass against
    a parser the hook does not use.
    """
    for raw in _HOOK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("RUFF_PIN=$("):
            return line
    raise AssertionError(
        ".husky/pre-commit no longer reads RUFF_PIN. Without it the hook runs "
        "whatever ruff is installed and the pin means nothing locally (#1162)."
    )


def test_the_hook_extraction_still_finds_the_pin() -> None:
    """Run the hook's own extraction against the real workflow file.

    This is the half that rots silently: reformat cq-ratchet.yml's env block and
    the sed stops matching, RUFF_PIN comes back empty, and a version check that
    compares against nothing is a version check that passes. The hook treats an
    empty pin as a hard failure for that reason; this asserts it does not get
    there in the first place.
    """
    res = subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", f'{_hook_pin_command()}\nprintf "%s" "$RUFF_PIN"'],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout == _the_ci_pin(), (
        f"the hook's pin extraction returned {res.stdout!r}, but cq-ratchet.yml "
        f"declares {_the_ci_pin()!r}. The hook cannot check a pin it cannot read."
    )


def test_the_hook_does_not_hardcode_a_second_copy_of_the_pin() -> None:
    """A fourth place to write the version is a fourth place to forget it."""
    body = _HOOK.read_text(encoding="utf-8")
    literals = {
        m.group(1)
        for m in re.finditer(r"ruff[=\s]+v?([0-9]+\.[0-9]+\.[0-9]+)", body, re.I)
        # `ruff==$RUFF_PIN` in the fix-it message is the pin, not a copy of it.
    }
    assert not literals, (
        f".husky/pre-commit hardcodes ruff version(s) {sorted(literals)} instead "
        "of reading RUFF_VERSION from cq-ratchet.yml."
    )


def test_the_hook_refuses_a_ruff_that_is_not_the_pin() -> None:
    """The whole point: a mismatched ruff must stop the commit, not reformat it.

    Asserted on the hook text because running the hook end to end needs a staged
    Python file and a git index; what is checked here is that the comparison
    exists and that its failure branch exits non-zero.
    """
    body = _HOOK.read_text(encoding="utf-8")
    assert 'RUFF_HAVE=$("$RUFF" --version' in body, (
        "the hook must ask the ruff it RESOLVED for its version, not assume it"
    )
    guard = re.search(
        r'if \[ "\$RUFF_HAVE" != "\$RUFF_PIN" \]; then(.*?)\n    fi',
        body,
        re.S,
    )
    assert guard is not None, "the hook must compare the resolved ruff to the pin"
    assert "exit 1" in guard.group(1), (
        "a mismatched ruff must fail the commit; a warning gets ignored and the "
        "hook still rewrites the staged files with the wrong formatter"
    )
