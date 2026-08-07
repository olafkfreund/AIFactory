"""A violation added in the same diff that removes others must still fail (#1189).

`cq_ratchet` compared ONE INTEGER per changed file: `if after > before`. Three
findings removed and one added nets to -2, reads as "improved", and the gate --
which is blocking -- reports success on a PR that introduced a violation. That is
the exact case a ratchet exists for: a file with no debt being paid down is the
easy case, and the only one the old comparison actually gated.

Observed, not hypothetical. Injecting an F401 into `scripts/cq_ratchet.py` on top
of the Factory#597 cleanup, with the base set to before that cleanup:

    cq-ratchet (ruff): 1 changed file(s) checked against the strict ruff baseline
    (1 improved, 0 unchanged, 0 regressed)
    EXIT=0

Base 18, head 1. Per rule code, the same tree now says:

    REGRESSION scripts/cq_ratchet.py: strict ruff violations 18 -> 1
      F401 +1 (base 0 -> head 1)
    EXIT=1

The three sibling forks (`scripts/ratchet_lint.py` in PFactory, TFactory and
CFactory) have compared a `Counter` keyed by rule code all along; this fork was
the odd one out and the weaker one. Factory#590 is the standing lesson about what
independent restatement of one shared rule costs, so `regressed_codes` is ported
from the siblings rather than reinvented.

The second half of this file covers #1188: the mypy target version. Told to
target the shared baseline's floor of 3.11 while reading a 3.12 venv's numpy
stubs, mypy exits 2 having checked nothing -- so the version it declares has to
come from the interpreter the gate is running under, not a literal.

Refs #1189, #1188, Factory#597, PFactory#467.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cq_ratchet  # noqa: E402

# --------------------------------------------------------------------------- #
# #1189: per-rule-code comparison                                              #
# --------------------------------------------------------------------------- #


def test_a_new_code_regresses_even_when_the_total_falls() -> None:
    """THE regression. Anything less than this and the fix is cosmetic.

    The Factory#597 shape exactly: a file that sheds 18 findings and gains one
    F401. `18 -> 1` is an improvement by total and a regression by rule.
    """
    base = Counter(
        {"S603": 7, "PLW1510": 4, "T201": 3, "S607": 2, "I001": 1, "PLR0912": 1}
    )
    head = Counter({"F401": 1})
    assert sum(head.values()) < sum(base.values()), (
        "the total must FALL, or this proves nothing"
    )
    assert cq_ratchet.regressed_codes(base, head) == ["F401 +1 (base 0 -> head 1)"]


def test_swapping_one_violation_for_another_is_not_neutral() -> None:
    """The net-zero case: -1 S101, +1 PLR2004. The old total said "unchanged"."""
    assert cq_ratchet.regressed_codes(
        Counter({"S101": 1}), Counter({"PLR2004": 1})
    ) == ["PLR2004 +1 (base 0 -> head 1)"]


def test_a_genuine_cleanup_is_not_a_regression() -> None:
    """The control. Without it, "fail on everything" passes the tests above.

    This is what keeps ORDINARY PRs green: removing findings, and leaving a
    file's existing legacy debt alone, must both stay silent.
    """
    base = Counter({"S603": 7, "T201": 3})
    assert cq_ratchet.regressed_codes(base, Counter({"S603": 2})) == []
    assert cq_ratchet.regressed_codes(base, base) == []


def test_a_new_file_has_no_base_so_every_code_counts() -> None:
    """base_count returns an empty Counter for a file absent on the base."""
    assert cq_ratchet.regressed_codes(Counter(), Counter({"S101": 2})) == [
        "S101 +2 (base 0 -> head 2)"
    ]


def test_every_regressed_code_is_reported_not_just_the_first() -> None:
    base = Counter({"S101": 1, "T201": 5})
    head = Counter({"S101": 3, "T201": 5, "F401": 1})
    assert cq_ratchet.regressed_codes(base, head) == [
        "F401 +1 (base 0 -> head 1)",
        "S101 +2 (base 1 -> head 3)",
    ]


# --------------------------------------------------------------------------- #
# #1188: the mypy target version follows the venv, not the baseline floor      #
# --------------------------------------------------------------------------- #


def test_mypy_targets_the_running_interpreter() -> None:
    assert (
        cq_ratchet.interpreter_target()
        == f"{sys.version_info.major}.{sys.version_info.minor}"
    )


def test_mypy_is_told_that_version(monkeypatch) -> None:
    """The wiring, which is where a derived value still goes astray.

    `standards/mypy.ini` declares 3.11 -- correct as the fleet FLOOR, wrong as
    this gate's target against a 3.12 venv whose numpy stubs use PEP 695 `type`
    statements. `--python-version` must appear on the argv, carrying the
    interpreter's own version, or mypy exits 2 having checked nothing.
    """
    seen: list[str] = []

    class _Res:
        returncode = 0
        stdout = ""
        stderr = ""

    def _record(argv: list[str], **_kwargs: object) -> _Res:
        seen.extend(argv)
        return _Res()

    monkeypatch.setattr(cq_ratchet.subprocess, "run", _record)
    cq_ratchet._mypy_count("mypy", "standards/mypy.ini", "apps/backend/agent.py")

    assert "--python-version" in seen
    assert seen[seen.index("--python-version") + 1] == cq_ratchet.interpreter_target()
    # Not a literal: a hard-coded "3.12" is exactly how "3.11" went stale.
    assert "3.11" not in seen
