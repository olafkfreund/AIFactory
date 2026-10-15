"""The PR review-tier note and signed-floor helper (#1698)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WS = Path(__file__).resolve().parents[1]
_BACKEND = _WS.parents[0] / "backend"
for _p in (str(_WS), str(_BACKEND)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from server.services import pr_endgame as pe  # noqa: E402
from tests.test_trusted_contract_merge import (  # noqa: E402
    _contract_file,
    _isolated_host,  # noqa: F401
    _rec,
    _spec,
)


@pytest.mark.parametrize(
    ("tier", "signed", "expected"),
    [
        ("low", "blocking", ("blocking", True)),
        ("blocking", "auto", ("blocking", False)),
        (None, "auto", ("auto", False)),
        ("auto", "auto", ("auto", False)),
    ],
)
def test_raise_to_signed(
    tier: str | None, signed: str, expected: tuple[str, bool]
) -> None:
    assert pe._raise_to_signed(tier, signed) == expected


def test_raise_to_signed_unrecognised_tier_is_preserved() -> None:
    effective, below = pe._raise_to_signed("bogus", "auto")
    assert effective == "bogus"
    assert below is True


def test_review_tier_note_flags_tier_below_signed(tmp_path: Path) -> None:
    spec, signed = _spec(tmp_path, review_tier="blocking")

    note = pe.review_tier_note(spec, "low", trusted=_rec(signed))

    assert "Effective review tier: blocking (signed floor blocking)." in note
    assert "Possible tampering" in note
    assert "reviewTier low is below the signed tier blocking" in note
    assert "the merge gate decides on blocking." in note


def test_review_tier_note_matching_signed_has_no_tampering_warning(
    tmp_path: Path,
) -> None:
    spec, signed = _spec(tmp_path, review_tier="auto")

    note = pe.review_tier_note(spec, "auto", trusted=_rec(signed))

    assert note == "Effective review tier: auto (signed floor auto)."
    assert "Possible tampering" not in note


def test_review_tier_note_legacy_without_contract(tmp_path: Path) -> None:
    spec, _ = _spec(tmp_path)
    _contract_file(spec).unlink()

    note = pe.review_tier_note(spec, "low", trusted=None)

    assert "Review tier: low (not checked" in note


def test_review_tier_note_crafted_tier_uses_constant_label(tmp_path: Path) -> None:
    spec, _ = _spec(tmp_path)
    _contract_file(spec).unlink()

    note = pe.review_tier_note(spec, "low\n\n@evil", trusted=None)

    assert "unrecognised" in note
    assert "@evil" not in note
