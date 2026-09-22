"""#1569: a task's creation time is the one recorded at intake, not the spec
directory's ctime.

`st_ctime` is the inode CHANGE time: a control-plane status write, an agent sync
or a restore moves it. Live on 2026-09-18, 18 specs created 09-08..09-11 all
reported the minute the merger wrote their task_control.json.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

from server.routes.task_service import spec_to_task  # noqa: E402

_STAMP = "2026-09-08T09:15:00"


def _spec(tmp_path: Path, *, stamp: str | None = _STAMP, spec_md: bool = True) -> Path:
    spec_dir = tmp_path / ".aifactory" / "specs" / "001-feature"
    spec_dir.mkdir(parents=True)
    requirements: dict[str, object] = {
        "title": "Feature",
        "description": "Do the thing",
    }
    if stamp is not None:
        requirements["created_at"] = stamp
    (spec_dir / "requirements.json").write_text(json.dumps(requirements))
    if spec_md:
        (spec_dir / "spec.md").write_text("# Feature\n")
    return spec_dir


def test_recorded_stamp_is_reported_verbatim(tmp_path: Path) -> None:
    task = spec_to_task("proj", _spec(tmp_path))
    assert task.created_at == _STAMP
    assert task.created_at_is_estimate is False


def test_a_later_write_does_not_re_date_the_task(tmp_path: Path) -> None:
    """The #1569 case: something writes into the spec dir, moving its ctime."""
    spec_dir = _spec(tmp_path)
    before = spec_to_task("proj", spec_dir).created_at

    (spec_dir / "task_control.json").write_text(json.dumps({"status": "done"}))

    after = spec_to_task("proj", spec_dir)
    assert after.created_at == before == _STAMP
    assert after.created_at_is_estimate is False


def test_no_stamp_falls_back_to_the_spec_s_own_files(tmp_path: Path) -> None:
    spec_dir = _spec(tmp_path, stamp=None)
    oldest = min(
        (spec_dir / name).stat().st_mtime for name in ("requirements.json", "spec.md")
    )

    task = spec_to_task("proj", spec_dir)

    assert task.created_at == datetime.fromtimestamp(oldest).isoformat()
    assert task.created_at_is_estimate is True


@pytest.mark.parametrize("stamp", ["yesterday", "", "2026-13-45T99:99"])
def test_an_unusable_stamp_falls_through(tmp_path: Path, stamp: str) -> None:
    task = spec_to_task("proj", _spec(tmp_path, stamp=stamp))
    assert task.created_at_is_estimate is True
    datetime.fromisoformat(task.created_at)  # still a usable timestamp


def test_without_its_own_files_the_dir_ctime_is_the_last_resort(
    tmp_path: Path,
) -> None:
    spec_dir = _spec(tmp_path, stamp=None, spec_md=False)
    (spec_dir / "requirements.json").unlink()

    task = spec_to_task("proj", spec_dir)

    assert task.created_at_is_estimate is True
    assert (
        task.created_at == datetime.fromtimestamp(spec_dir.stat().st_ctime).isoformat()
    )


def test_a_listing_still_sorts_newest_first(tmp_path: Path) -> None:
    """Mirrors routes/tasks.py: all_tasks.sort(key=..., reverse=True)."""
    tasks = []
    for name, stamp in (
        ("001-a", "2026-09-08T09:00:00"),
        ("002-b", "2026-09-11T09:00:00"),
    ):
        spec_dir = tmp_path / ".aifactory" / "specs" / name
        spec_dir.mkdir(parents=True)
        (spec_dir / "requirements.json").write_text(
            json.dumps({"title": name, "description": "x", "created_at": stamp})
        )
        tasks.append(spec_to_task("proj", spec_dir))

    tasks.sort(key=lambda t: t.created_at, reverse=True)

    assert [t.spec_id for t in tasks] == ["002-b", "001-a"]
