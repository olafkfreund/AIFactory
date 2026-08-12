"""The discard message must not contradict the structured fields (#1082).

`/discard` returns both a human-readable `message` and the structured
`branchDeleted` / `branchReason`. The message used to append "and branch X"
whenever a branch was *identified*, so a refused `git branch -D` produced a
response that said the branch was deleted in prose and null in the field.

A reader believes the sentence. These are the three outcomes, kept apart.
"""

from __future__ import annotations

from server.routes.worktree_merge import _branch_suffix


def test_a_deleted_branch_is_the_only_case_that_claims_deletion() -> None:
    assert _branch_suffix("feat/x", "feat/x", None) == " and branch feat/x"


def test_a_refused_delete_says_so_and_carries_the_reason() -> None:
    out = _branch_suffix(None, "feat/x", "branch not fully merged")
    assert "NOT deleted" in out
    assert "branch not fully merged" in out
    # The bug: prose claiming a deletion that branchDeleted=null denies.
    assert " and branch feat/x" not in out


def test_a_refused_delete_with_no_reason_still_does_not_claim_deletion() -> None:
    out = _branch_suffix(None, "feat/x", None)
    assert "NOT deleted" in out
    assert "unknown error" in out


def test_no_branch_identified_is_its_own_outcome() -> None:
    assert _branch_suffix(None, None, None) == "; no task branch identified"


def test_the_three_outcomes_are_mutually_distinguishable() -> None:
    """Guards the collapse itself: no two outcomes may read the same."""
    outcomes = {
        _branch_suffix("feat/x", "feat/x", None),
        _branch_suffix(None, "feat/x", "refused"),
        _branch_suffix(None, None, None),
    }
    assert len(outcomes) == 3
