"""The discard endpoint must DISCOVER the task branch, not spell it (#1082).

A source-level check on purpose, and it is worth saying why rather than
pretending it is stronger than it is. The behaviour needs a git repo, a project
registry and a worktree to exercise, which is a large fixture for one line of
logic. The regression this actually guards is small and specific: someone
re-introducing ``f"aifactory/{spec_id}"`` because it reads more directly than a
resolver call.

That regression is invisible at runtime, which is what makes it worth a test at
all -- ``git branch -D`` runs with ``capture_output=True`` and its returncode is
never checked, so a wrong branch name deletes nothing and still reports success.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROUTES = (
    Path(__file__).resolve().parents[1]
    / "apps/web-server/server/routes/worktree_merge.py"
)


def _discard_handler_source() -> str:
    """The handler's EXECUTABLE lines only.

    Comments are stripped because the fix's own comment quotes the literal it
    removed, in order to explain it -- so a whole-body match flags the
    documentation rather than the code. Same trap as a grep for `curl | sh`
    finding the paragraph warning against it.
    """
    src = _ROUTES.read_text()
    start = src.index("async def discard_worktree")
    nxt = src.find("\nasync def ", start + 1)
    body = src[start:] if nxt == -1 else src[start:nxt]
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


def test_discard_does_not_hardcode_the_branch_convention():
    """`aifactory/` is owned by core.worktree.get_branch_name; a copy here drifts."""
    body = _discard_handler_source()
    assert not re.search(r'f"aifactory/\{', body), (
        "discard_worktree hardcodes the branch name again -- use "
        "resolve_task_branch so the convention stays in one place"
    )


def test_discard_uses_the_shared_resolver():
    body = _discard_handler_source()
    assert "resolve_task_branch(" in body, (
        "discard_worktree must resolve the task branch via services/task_branch.py"
    )


def test_discard_skips_the_delete_when_no_branch_was_identified():
    """Destroying endpoint: never delete a branch nobody identified."""
    body = _discard_handler_source()
    assert "if branch_name:" in body, (
        "the `git branch -D` call must be guarded on a resolved branch"
    )
