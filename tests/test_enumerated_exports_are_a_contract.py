"""An API the card enumerates is a contract, not a suggestion (#1421).

The incident: a card naming the exact functions to export got a working
implementation with different names, twice, on the same card::

    spec:  emptyBoard()  move(board, index, player)  winner(board)  winningLine(board)
    run 140 (low/haiku):   createGame, isValidMove, makeMove, checkWinner, ...  0/4
    run 141 (medium/sonnet): WIN_LINES, createEmptyBoard, getWinner, move       1/4

Run 140 reported 24 passing tests, all passing, none exercising the API the card
specified -- the coder writes the tests too, so a green suite against an invented
API is indistinguishable from a green suite against the required one. Raising the
tier moved 0/4 to 1/4 without making the contract binding, which is why this is a
check and not a better prompt: a prompt cannot be verified.

Real git repositories, not mocked subprocess, for the same reason as #1396: the
thing under test is partly what git reports about the build's own output.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from agents.tools_pkg.tools.api_contract import (  # noqa: E402
    missing_exports,
    required_exports,
)

SPEC = """# Tic tac toe core

## Required exports

- `emptyBoard()` returns a fresh board
- `move(board, index, player)` returns a new board
- `winner(board)` returns the winning player or null
- `winningLine(board)` returns the three indices
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    """A clone whose base commit is on origin -- the build worktree's shape."""
    origin, seed = tmp_path / "origin.git", tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("base\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-qm", "base")
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(origin)],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True
    )
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "checkout", "-qb", "aifactory/140-tictactoe")
    return work


# ── reading the contract ──────────────────────────────────────────────────────


def test_a_required_exports_heading_is_a_contract() -> None:
    assert required_exports(SPEC) == {"emptyBoard", "move", "winner", "winningLine"}


def test_a_literal_exports_block_is_a_contract() -> None:
    """The workaround operators already use — restating the API as real code."""
    assert required_exports(
        "Implement it.\n\n```js\nmodule.exports = { emptyBoard, move, winner };\n```\n"
    ) == {"emptyBoard", "move", "winner"}


def test_export_declarations_are_a_contract() -> None:
    assert required_exports(
        "```ts\nexport function move(b) {}\nexport const winner = 1;\n```"
    ) == {
        "move",
        "winner",
    }


def test_prose_mentioning_a_function_is_not_a_contract() -> None:
    """The check must only fire on something the author wrote deliberately.

    A refusal nobody can predict is worse than the bug it prevents, so a card
    that never promised an API cannot be blocked for breaking one.
    """
    assert required_exports("Fix the bug in `render()` and tidy `main()`.") == set()
    assert (
        required_exports("# Overview\n\nA tic tac toe game. No API is specified.")
        == set()
    )


def test_names_are_scoped_to_the_api_section() -> None:
    """Backticked calls elsewhere are examples, not requirements. Sweeping the
    whole document would make each one a name the build is refused for."""
    spec = SPEC + "\n## Notes\n\nCall `console.log()` while debugging.\n"
    assert "console" not in required_exports(spec)
    assert "log" not in required_exports(spec)


# ── checking the build against it ─────────────────────────────────────────────


def test_the_observed_incident_is_refused(clone: Path) -> None:
    """Run 140's actual exports against run 140's actual spec: 0/4."""
    (clone / "game.js").write_text(
        "function createGame() {}\nfunction isValidMove() {}\n"
        "function makeMove() {}\nfunction checkWinner() {}\n"
        "module.exports = { createGame, isValidMove, makeMove, checkWinner };\n"
    )
    _git(clone, "add", "game.js")
    _git(clone, "commit", "-qm", "tictactoe")

    assert missing_exports(SPEC, clone) == [
        "emptyBoard",
        "move",
        "winner",
        "winningLine",
    ]


def test_a_build_that_honours_the_contract_passes(clone: Path) -> None:
    """The check must not cost a correct build its sign-off."""
    (clone / "game.js").write_text(
        "function emptyBoard() {}\nconst move = (b, i, p) => b;\n"
        "function winner() {}\nfunction winningLine() {}\n"
    )
    _git(clone, "add", "game.js")
    _git(clone, "commit", "-qm", "tictactoe")

    assert missing_exports(SPEC, clone) == []


def test_a_partial_match_names_only_what_is_missing(clone: Path) -> None:
    """Run 141's shape: one of four. The refusal must say which."""
    (clone / "game.js").write_text("function move() {}\nfunction getWinner() {}\n")
    _git(clone, "add", "game.js")
    _git(clone, "commit", "-qm", "partial")

    assert missing_exports(SPEC, clone) == ["emptyBoard", "winner", "winningLine"]


def test_uncommitted_work_counts_as_output(clone: Path) -> None:
    """Same two signals as #1396 — a build mid-flight has not failed."""
    (clone / "game.js").write_text(
        "function emptyBoard() {}\nfunction move() {}\n"
        "function winner() {}\nfunction winningLine() {}\n"
    )
    assert missing_exports(SPEC, clone) == []


def test_a_spec_with_no_contract_never_refuses(clone: Path) -> None:
    (clone / "whatever.js").write_text("function anything() {}\n")
    _git(clone, "add", "whatever.js")
    _git(clone, "commit", "-qm", "x")

    assert missing_exports("# Just do something sensible.\n", clone) == []


def test_an_empty_build_defers_to_the_1396_refusal(clone: Path) -> None:
    """Nothing was built at all. #1396 owns that refusal and says it better;
    listing every required name here would bury it."""
    assert missing_exports(SPEC, clone) == []


def test_a_definition_elsewhere_in_the_repo_does_not_vouch(clone: Path) -> None:
    """Only the build's OWN output counts. A same-named function in a file this
    build never touched must not satisfy the contract."""
    (clone / "vendor.js").write_text(
        "function emptyBoard() {}\nfunction move() {}\n"
        "function winner() {}\nfunction winningLine() {}\n"
    )
    _git(clone, "add", "vendor.js")
    _git(clone, "commit", "-qm", "vendored")
    _git(clone, "push", "-q", "origin", "HEAD:main")
    _git(clone, "fetch", "-q", "origin")
    (clone / "game.js").write_text("function createGame() {}\n")
    _git(clone, "add", "game.js")
    _git(clone, "commit", "-qm", "build")

    assert missing_exports(SPEC, clone) == [
        "emptyBoard",
        "move",
        "winner",
        "winningLine",
    ]


# ── the glue: reading spec.md at sign-off time ────────────────────────────────


def test_the_qa_helper_reads_the_contract_from_spec_md(
    clone: Path, tmp_path: Path
) -> None:
    """The wiring, not just the checker. A guard that is written but never
    reaches its call site is inert, and looks identical to one that passes."""
    from agents.tools_pkg.tools.qa import _missing_contract_exports

    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text(SPEC)
    (clone / "game.js").write_text("function createGame() {}\n")
    _git(clone, "add", "game.js")
    _git(clone, "commit", "-qm", "wrong api")

    assert _missing_contract_exports(spec_dir, clone) == [
        "emptyBoard",
        "move",
        "winner",
        "winningLine",
    ]


def test_an_unreadable_spec_does_not_refuse(clone: Path, tmp_path: Path) -> None:
    """An absent spec.md is not evidence a contract was broken. Blocking on it
    would trade a false pass for a false failure — the trade _nothing_was_built
    already refuses to make when git cannot answer."""
    from agents.tools_pkg.tools.qa import _missing_contract_exports

    empty = tmp_path / "no_spec"
    empty.mkdir()
    (clone / "game.js").write_text("function createGame() {}\n")
    _git(clone, "add", "game.js")
    _git(clone, "commit", "-qm", "x")

    assert _missing_contract_exports(empty, clone) == []


# ── the shape the reported card actually used ─────────────────────────────────
#
# The first cut keyed only off headings matching exports/api/interface. Run
# against the real FCT-4 body it returned NOTHING: that card enumerates its API
# under "## What to build", which is how a deliverable is usually described. The
# check was inert on the very card that prompted it.


FCT4 = """## What to build

`games/tictactoe/index.html` — a single self-contained page.

`games/tictactoe/game.js` — the rules as pure functions:

- `emptyBoard()` — a fresh 9-cell board
- `move(board, index, player)` — returns a NEW board
- `winner(board)` — `"X"`, `"O"`, `"draw"`, or `null`
- `winningLine(board)` — the three indices that won, or `null`
"""


def test_a_bulleted_list_of_signatures_is_a_contract() -> None:
    assert required_exports(FCT4) == {"emptyBoard", "move", "winner", "winningLine"}


def test_a_numbered_list_counts_too() -> None:
    assert required_exports("1. `alpha()` does a\n2. `beta()` does b\n") == {
        "alpha",
        "beta",
    }


def test_one_bullet_is_prose_not_a_contract() -> None:
    """The threshold is the whole safety story. A single bullet opening with a
    backticked call is ordinary writing, and refusing a build over it would be a
    refusal nobody could predict."""
    assert required_exports("## Notes\n\n- `cleanup()` runs on exit\n") == set()


def test_a_dotted_call_is_not_a_bare_name() -> None:
    """`console.log()` must not contribute `console`: the identifier stops at the
    dot, so a debugging note in a bullet list cannot become a required export."""
    spec = "## Notes\n\n- `console.log()` for debug\n- `process.exit()` to bail\n"
    assert required_exports(spec) == set()


def test_bullets_without_parens_are_commands_not_functions() -> None:
    assert required_exports("- `npm install` first\n- `npm test` after\n") == set()


def test_the_bullet_shape_reaches_missing_exports(clone: Path) -> None:
    """End to end on the reported card's own shape: the wrong API is refused and
    every missing name is listed."""
    (clone / "game.js").write_text(
        "function createGame() {}\nfunction isValidMove() {}\n"
    )
    _git(clone, "add", "game.js")
    _git(clone, "commit", "-qm", "wrong api")

    assert missing_exports(FCT4, clone) == [
        "emptyBoard",
        "move",
        "winner",
        "winningLine",
    ]
