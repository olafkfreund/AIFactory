"""A linter that never ran must fail the ratchet, not read as "no violations".

PFactory#455 (ruff), same shape found and fixed in TFactory#951. Swept across
the fleet because two repos finding it independently makes it a class: a
subprocess whose stdout is read for results while its `returncode` is ignored.

`_ruff_count` ended `return len(json.loads(res.stdout)) if res.stdout.strip()
else 0`. Under `--output-format json` a CLEAN run prints `[]`, never nothing, so
empty stdout was never the clean case -- it is ruff itself failing (binary
missing, config parse error, bad argv), all of which exit >=2 and write nothing
to stdout. The cause is environmental, so it hits the base worktree and the head
tree alike: both counts come back 0, nothing regresses, and the ratchet reports

    cq-ratchet (ruff): N changed file(s) checked ... 0 regressed

having measured nothing. Same family as #1124, where the pathspec silently
matched no files and the gate reported success for not running.

`_mypy_count` had it too. mypy exits 0 clean, 1 with errors, and 2 both for its
own failure AND for a blocking error -- but a blocking error still NAMES a file,
so it lands in the count, while mypy failing to run names nothing. Hence the
`count == 0` qualifier, which the last test here is the control for.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cq_ratchet  # noqa: E402

_FILE = "apps/backend/agent.py"


class _Res:
    """The subset of CompletedProcess the ratchet reads."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch):
    # _ruff_count reads the file's content off disk to feed ruff on stdin, and
    # _mypy_count resolves it to attribute error lines, so the ratchet's real
    # working directory (the repo root, as CI and the hook invoke it) is part of
    # what is under test. Only the tool invocation itself is stubbed.
    monkeypatch.chdir(Path(__file__).resolve().parents[1])

    def _apply(res: _Res) -> None:
        monkeypatch.setattr(cq_ratchet.subprocess, "run", lambda *_a, **_k: res)

    return _apply


def _ruff_count() -> Counter[str]:
    return cq_ratchet._ruff_count("ruff", "ruff.toml", _FILE, _FILE)


def _mypy_count() -> int:
    return cq_ratchet._mypy_count("mypy", "mypy.ini", _FILE)


# --------------------------------------------------------------------------- #
# ruff                                                                         #
# --------------------------------------------------------------------------- #


def test_ruff_own_failure_exits_rather_than_reporting_clean(stub) -> None:
    stub(_Res(2, stderr="error: invalid value for '--config <CONFIG_OPTION>'"))
    with pytest.raises(SystemExit) as exc:
        _ruff_count()
    assert exc.value.code == 2


def test_ruff_failure_surfaces_stderr_for_diagnosis(stub, capsys) -> None:
    stub(_Res(2, stderr="does not point to a configuration file"))
    with pytest.raises(SystemExit):
        _ruff_count()
    assert "does not point to a configuration file" in capsys.readouterr().err


def test_ruff_clean_file_still_counts_zero(stub) -> None:
    # Control: exit 0 with "[]" is ruff saying "checked it, nothing wrong".
    stub(_Res(0, stdout="[]"))
    assert _ruff_count() == Counter()


def test_ruff_violations_are_still_counted(stub) -> None:
    # Control: exit 1 is the ordinary "found something" path, not a failure.
    stub(_Res(1, stdout='[{"code": "S101"}, {"code": "PLR2004"}]'))
    # Keyed by rule code since #1189, so the two are separable rather than
    # summed into one number an added violation can hide inside.
    assert _ruff_count() == Counter({"S101": 1, "PLR2004": 1})


# --------------------------------------------------------------------------- #
# mypy                                                                         #
# --------------------------------------------------------------------------- #


def test_mypy_own_failure_exits_rather_than_reporting_zero_errors(stub) -> None:
    stub(_Res(2, stderr="mypy: error: Cannot find config file 'mypy.ini'"))
    with pytest.raises(SystemExit) as exc:
        _mypy_count()
    assert exc.value.code == 2


def test_mypy_failure_surfaces_stderr_for_diagnosis(stub, capsys) -> None:
    stub(_Res(2, stderr="mypy: error: unrecognized arguments: --bogus"))
    with pytest.raises(SystemExit):
        _mypy_count()
    assert "unrecognized arguments" in capsys.readouterr().err


def test_mypy_clean_file_still_counts_zero(stub) -> None:
    # Control: exit 0 with no error lines is a genuinely clean file.
    stub(_Res(0, stdout="Success: no issues found in 1 source file\n"))
    assert _mypy_count() == 0


def test_mypy_errors_are_still_counted(stub) -> None:
    # Control: exit 1 is the ordinary "found something" path.
    out = (
        f"{_FILE}:3: error: Function is missing a type annotation  [no-untyped-def]\n"
        f"{_FILE}:9: error: Returning Any from function  [no-any-return]\n"
    )
    stub(_Res(1, stdout=out))
    assert _mypy_count() == 2


def test_mypy_blocking_error_is_counted_not_treated_as_a_crash(stub) -> None:
    # Control with teeth: a syntax error exits 2 as well, but NAMES the file.
    # Keying the guard on the exit code alone would abort the ratchet here
    # instead of blocking the regression.
    stub(_Res(2, stdout=f"{_FILE}:1: error: invalid syntax  [syntax]\n"))
    assert _mypy_count() == 1
