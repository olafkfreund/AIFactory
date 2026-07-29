"""The shared two-store status writer (#1064, #1071).

Approve used to do the git work and leave the task at `human_review`, so an
approved+merged task stayed on the board asking for a review that had already
happened. These pin the write that fixes it -- and that it reports rather than
swallows a failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


def _writer():
    return pytest.importorskip("server.services.task_status").write_status


def _spec(tmp_path: Path, status: str = "human_review") -> Path:
    spec = tmp_path / "001-thing"
    spec.mkdir(parents=True)
    (spec / "implementation_plan.json").write_text(
        json.dumps({"status": status, "title": "thing"})
    )
    return spec


def test_writes_both_stores(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    assert _writer()(spec, status="done", reason="approved", updated_by="t") is None

    plan = json.loads((spec / "implementation_plan.json").read_text())
    assert plan["status"] == "done", "plan file still says human_review"
    control = json.loads((spec / "task_control.json").read_text())
    assert control["status"] == "done", "control store still says human_review"
    assert control.get("updatedBy") == "t"


def test_a_corrupt_plan_file_does_not_block_the_control_write(tmp_path: Path) -> None:
    """Corrupt plan files exist in the wild (#1069).

    Refusing to record a completed merge because a sibling file is malformed
    would strand the task in exactly the state this write exists to leave.
    """
    spec = _spec(tmp_path)
    (spec / "implementation_plan.json").write_text('{"status": "x", \\"bad\\": 1}')

    error = _writer()(spec, status="done", reason="approved", updated_by="t")

    # The authoritative store IS updated ...
    control = json.loads((spec / "task_control.json").read_text())
    assert control["status"] == "done"
    # ... and the caller is TOLD the other one was not.
    assert error and "implementation_plan.json" in error


def test_a_missing_plan_file_is_not_an_error(tmp_path: Path) -> None:
    spec = tmp_path / "002-thing"
    spec.mkdir(parents=True)
    assert _writer()(spec, status="done", reason="r", updated_by="t") is None
    assert json.loads((spec / "task_control.json").read_text())["status"] == "done"


def test_only_one_place_writes_the_approval_status() -> None:
    """The status write must have exactly one caller in the merge module.

    Two writers is how one of them ends up not writing (#1071): the PR-merge
    path and the local-merge path both finish an approval, and if each shapes
    its own response they drift. `_approved` is that one place.
    """
    src = (_WEB_SERVER / "server" / "services" / "approval.py").read_text()
    route = (_WEB_SERVER / "server" / "routes" / "worktree_merge.py").read_text()
    assert src.count("write_status(") == 1, "the approval status has more than one writer"
    assert "def approved(" in src, "the shared approval helper is gone"


def test_a_refused_pull_request_merge_is_not_recorded_as_approved() -> None:
    """GitHub declining the merge must not become "done".

    The refusal branch (`if pr_detail:` after a False merge result) means a PR
    exists and GitHub would not merge it -- conflicts, required checks, branch
    protection. Recording that as approved would claim success for something
    that did not happen, which is the defect class this whole change exists to
    remove.

    Asserted on the branch itself rather than by scanning backwards for a
    failure return: a mutation that REPLACES the failure return with an
    approval leaves nothing behind for a backwards scan to find, and the first
    version of this test passed such a mutation.
    """
    src = (_WEB_SERVER / "server" / "services" / "approval.py").read_text()
    route = (_WEB_SERVER / "server" / "routes" / "worktree_merge.py").read_text()
    branch = route[route.index("    if pr_detail:") :]
    body = branch[: branch.index("\n    # No PR")]
    assert '"success": False' in body, "the refused-PR branch must report failure"
    assert "approved(" not in body, "a refused PR merge is being recorded as approved"


def test_the_pr_path_records_the_approval_on_success() -> None:
    src = (_WEB_SERVER / "server" / "services" / "approval.py").read_text()
    route = (_WEB_SERVER / "server" / "routes" / "worktree_merge.py").read_text()
    branch = route[route.index("    if pr_merged:") :]
    body = branch[: branch.index("    if pr_detail:")]
    assert "approved(" in body, "a merged PR is not being recorded as approved"


def test_merge_reports_a_failed_status_write_instead_of_claiming_done() -> None:
    src = (_WEB_SERVER / "server" / "services" / "approval.py").read_text()
    route = (_WEB_SERVER / "server" / "routes" / "worktree_merge.py").read_text()
    assert '"taskStatus": "done" if not status_error else "unchanged"' in src, (
        "the response must not say done when the status write failed"
    )


def test_merge_addresses_the_spec_dir_by_sanitized_spec_id() -> None:
    """Not by task_id, which CodeQL caught as a path injection.

    Two things go wrong with task_id here. It still carries the "project_id:"
    prefix, so the join addresses a directory that does not exist -- and
    write_control would helpfully CREATE it, landing the status nowhere while
    reporting success. And it is raw URL input, so a "../" would escape the
    specs directory entirely.

    spec_id is the value the handler has already put through
    safe_spec_component.
    """
    src = (_WEB_SERVER / "server" / "services" / "approval.py").read_text()
    route = (_WEB_SERVER / "server" / "routes" / "worktree_merge.py").read_text()
    call = src.index("status_error = write_status(")
    args = src[call : src.index("updated_by=", call)]
    assert '"specs" / safe_spec_component(spec_id)' in args, (
        "the spec dir must be addressed by a BARRIERED spec_id -- callers do "
        "sanitise, but a helper that trusts its parameter is one refactor away "
        "from a caller that does not, and CodeQL is right to say so"
    )
    assert "/ task_id" not in args, (
        "task_id is raw URL input and carries the project prefix"
    )
