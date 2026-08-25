"""An enumerated API reaches the coder as a hard requirement (#1430).

Two live failures on the same card, both after the spec itself was fixed:

Spec 157 received the full spec, saw emptyBoard/move/winner/winningLine, and
shipped newGame/checkWinner/isBoardFull/move -- 1 of 4. Fixing the INPUT was not
enough; the model read the required API and paraphrased it anyway.

Spec 158 wrote nothing, reporting "this work was already completed and committed
on this branch -- description matches this spec almost verbatim". It was judging
by DESCRIPTION: the existing module exported newGame and checkWinner while the
spec named emptyBoard and winner.
"""

from __future__ import annotations

from pathlib import Path

from prompts_pkg.prompts import get_api_contract_context, get_solo_prompt

SPEC = """# Tic tac toe

## What to build

- `emptyBoard()` — a fresh board
- `move(board, index, player)` — returns a NEW board
- `winner(board)` — X, O, draw or null
"""


def _spec_dir(tmp_path: Path, text: str) -> Path:
    (tmp_path / "spec.md").write_text(text)
    return tmp_path


def test_the_enumerated_names_reach_the_prompt(tmp_path: Path) -> None:
    block = get_api_contract_context(_spec_dir(tmp_path, SPEC))

    assert "`emptyBoard`" in block
    assert "`move`" in block
    assert "`winner`" in block


def test_it_tells_the_agent_to_check_rather_than_assume(tmp_path: Path) -> None:
    """Spec 158's exact failure: a matching description taken as proof."""
    block = get_api_contract_context(_spec_dir(tmp_path, SPEC)).lower()

    assert "verify" in block
    assert "not evidence" in block


def test_a_spec_with_no_enumerated_api_adds_nothing(tmp_path: Path) -> None:
    """The common case. An advisory block that fires on every spec is noise, and
    noise is what gets ignored."""
    assert get_api_contract_context(_spec_dir(tmp_path, "# Fix the bug.\n")) == ""


def test_an_unreadable_spec_adds_nothing(tmp_path: Path) -> None:
    """Degrades silently: a prompt block must never fail a build."""
    assert get_api_contract_context(tmp_path / "nope") == ""


def test_the_block_is_wired_into_the_solo_prompt(tmp_path: Path) -> None:
    """The wiring, not just the helper. Solo is coder AND QA in one flow, so
    nothing downstream catches a paraphrase before the branch is pushed -- and a
    block that never reaches the prompt changes nothing."""
    prompt = get_solo_prompt(_spec_dir(tmp_path, SPEC))

    assert "REQUIRED API (HARD REQUIREMENT)" in prompt
    assert "`emptyBoard`" in prompt
