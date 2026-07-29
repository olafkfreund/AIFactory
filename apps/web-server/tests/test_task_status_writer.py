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


def test_merge_records_done_only_on_the_success_path() -> None:
    """The conflict and failure returns must not be reachable from the write.

    Structural, because the alternative is standing up a git repo: assert the
    write_status call sits after the CalledProcessError-free success return and
    that no failure return follows it inside the same try.
    """
    src = (_WEB_SERVER / "server" / "routes" / "worktree_merge.py").read_text()
    assert "write_status(" in src, "merge no longer records a status"
    call = src.index("write_status(\n            project_path")
    success = src.index('"merged": True', call)
    assert success > call, "status write must precede the success return"
    between = src[call:success]
    assert '"success": False' not in between, (
        "a failure return sits inside the write path"
    )


def test_merge_reports_a_failed_status_write_instead_of_claiming_done() -> None:
    src = (_WEB_SERVER / "server" / "routes" / "worktree_merge.py").read_text()
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
    src = (_WEB_SERVER / "server" / "routes" / "worktree_merge.py").read_text()
    call = src.index("status_error = write_status(")
    args = src[call : src.index("updated_by=", call)]
    assert '"specs" / spec_id' in args, (
        "merge must address the spec dir by sanitized spec_id"
    )
    assert "/ task_id" not in args, (
        "task_id is raw URL input and carries the project prefix"
    )
