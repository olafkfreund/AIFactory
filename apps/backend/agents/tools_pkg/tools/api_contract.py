"""AIFactory#1421: an enumerated API in the spec is a contract, not a suggestion.

A card that names the exact functions to export got a working implementation
with different names, twice, on the same card::

    spec:  emptyBoard()  move(board, index, player)  winner(board)  winningLine(board)
    run 140 (haiku):  createGame, isValidMove, makeMove, getWinningLines, ...   0/4
    run 141 (sonnet): WIN_LINES, createEmptyBoard, getWinner, createGame, move  1/4

Run 140 reported 24 passing tests. Every one passed. None exercised the API the
card specified -- because the coder writes the tests too, against whatever API it
invented. A green suite written against an invented API is indistinguishable from
a green suite written against the required one, which is why this needs a check
rather than a better prompt: a prompt cannot be verified, and raising the tier
only moved 0/4 to 1/4.

The cost is not cosmetic. The card declared those functions so the NEXT three
cards could import them. With the names changed each dependent card re-implements
the whole thing, which is exactly what happened: four cards, four implementations,
``games/`` still empty on main.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# An identifier that could be a function or class name in any of the languages
# this fleet builds. Deliberately not language-specific: the check is for a name
# being ABSENT, and absence looks the same everywhere.
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

# `git status --porcelain` prefixes each path with two status columns and a
# space, so the path starts at index 3 and a shorter line carries no path.
_PORCELAIN_PREFIX = 3

# Contract forms, most explicit first. Each must be something an author WROTE
# deliberately -- a heading they typed, or literal export code they pasted --
# never prose that merely happens to mention a function. A heuristic that fires
# on prose would block legitimate builds, and a refusal nobody can predict is
# worse than the bug it prevents.
_EXPORT_BLOCK = re.compile(
    r"(?:module\.exports\s*=\s*\{|export\s*\{)([^}]*)\}", re.MULTILINE
)
_EXPORT_DECL = re.compile(
    rf"export\s+(?:default\s+)?(?:async\s+)?(?:function|const|class)\s+({_IDENT})"
)
# A heading that announces an API, then the backticked names listed under it.
_API_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s*(?:required\s+)?"
    r"(?:exports?|public\s+api|api|public\s+interface|interface)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_BACKTICKED_CALL = re.compile(rf"`({_IDENT})\s*\(")

# A list item whose FIRST token is a backticked call: "- `move(board, index)` — ..."
# This is how an API is enumerated in practice, and the heading above such a list
# is usually about the deliverable ("## What to build"), not about exports -- which
# is why keying only off headings left the check inert on the very card that
# prompted it.
_BULLET_CALL = re.compile(
    rf"^\s{{0,3}}(?:[-*+]|\d+\.)\s+`({_IDENT})\s*\(", re.MULTILINE
)

# Two is the threshold, and it is the whole safety story. One bullet starting with
# a backticked call is ordinary prose -- "- `npm run build()` to rebuild" -- and
# refusing a build over it would be a refusal nobody can predict. Two or more in
# one document is someone listing an API.
_BULLET_CALL_MIN = 2


def _names_in_export_blocks(text: str) -> set[str]:
    names: set[str] = set()
    for body in _EXPORT_BLOCK.findall(text):
        for part in body.split(","):
            # `{ a, b as c }` and `{ a: impl }` both contract to the exported name.
            name = part.split(" as ")[0].split(":")[0].strip()
            if re.fullmatch(_IDENT, name):
                names.add(name)
    names.update(_EXPORT_DECL.findall(text))
    return names


def _names_under_api_heading(text: str) -> set[str]:
    """Backticked ``name(`` tokens between an API heading and the next heading.

    Scoped to the section on purpose. Collecting backticked calls from the whole
    document would sweep up examples, prose and test snippets, and each one would
    become a name the build is refused for not exporting.
    """
    names: set[str] = set()
    for m in _API_HEADING.finditer(text):
        rest = text[m.end() :]
        nxt = re.search(r"^\s{0,3}#{1,6}\s", rest, re.MULTILINE)
        names.update(_BACKTICKED_CALL.findall(rest[: nxt.start()] if nxt else rest))
    return names


def _names_in_bullet_lists(text: str) -> set[str]:
    """Names from a bulleted enumeration of function signatures.

    Only fires at ``_BULLET_CALL_MIN`` or more, because a single bullet opening
    with a backticked call is ordinary prose. A list of several is an API being
    spelled out, whatever the heading above it happens to say.
    """
    names = _BULLET_CALL.findall(text)
    return set(names) if len(names) >= _BULLET_CALL_MIN else set()


def required_exports(spec_text: str) -> set[str]:
    """Names the spec states as a required API. Empty when it states none.

    Empty is the common case and means the check does not fire at all. That is
    the point: this only ever refuses a build whose card explicitly enumerated an
    API, so a card that never made the promise cannot be blocked for breaking it.
    """
    return (
        _names_in_export_blocks(spec_text)
        | _names_under_api_heading(spec_text)
        | _names_in_bullet_lists(spec_text)
    )


def _build_output_files(project_dir: Path) -> list[Path]:
    """Files this build added or changed -- committed-but-unpushed, plus dirty.

    Same two signals, and the same ``--not --remotes=origin`` idiom, as
    ``_nothing_was_built``: the worktree is cut from a base commit that IS on
    origin, so that commit is excluded and only what this build produced is
    counted, without having to resolve the base branch by name.

    Restricted to the build's own output rather than the whole worktree so a
    same-named function elsewhere in the repo -- a vendored copy, an unrelated
    module -- cannot vouch for an API this build never wrote.
    """
    paths: set[str] = set()
    try:
        committed = subprocess.run(
            [  # noqa: S607
                "git",
                "log",
                "--name-only",
                "--pretty=format:",
                "HEAD",
                "--not",
                "--remotes=origin",
            ],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],  # noqa: S607
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if committed.returncode == 0:
        paths.update(ln.strip() for ln in committed.stdout.splitlines() if ln.strip())
    if dirty.returncode == 0:
        for ln in dirty.stdout.splitlines():
            if len(ln) > _PORCELAIN_PREFIX:
                paths.add(ln[_PORCELAIN_PREFIX:].strip().split(" -> ")[-1])
    return [p for p in (project_dir / rel for rel in sorted(paths)) if p.is_file()]


def missing_exports(spec_text: str, project_dir: Path) -> list[str]:
    """Required names this build did not define. Empty when the contract is met.

    Checks for a DEFINITION, not for a correct export statement. The failure
    being caught is a name that is absent entirely (0/4, 1/4) -- and definition
    presence catches that with far fewer ways to be wrong than trying to model
    every language's export syntax. A name defined but not exported would slip
    through; that is a narrower bug than the one this exists for, and a check
    that cried wolf on export syntax would be turned off.
    """
    required = required_exports(spec_text)
    if not required:
        return []
    files = _build_output_files(project_dir)
    if not files:
        # Nothing was built. #1396's check owns that refusal and says it better;
        # reporting every required name as missing here would bury it.
        return []
    defined: set[str] = set()
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name in required:
            if name in defined:
                continue
            if re.search(
                rf"\b(?:function|const|let|var|class|def)\s+{re.escape(name)}\b"
                rf"|\b{re.escape(name)}\s*[:=]\s*(?:async\s+)?(?:function\b|\()"
                rf"|\bexports\.{re.escape(name)}\b",
                text,
            ):
                defined.add(name)
    return sorted(required - defined)
