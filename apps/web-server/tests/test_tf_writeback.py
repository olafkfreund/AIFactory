"""A write-back is trusted only when task and commit evidence agree."""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

_WS = Path(__file__).resolve().parents[1]
for _path in (_WS, _WS.parent / "backend"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from pfactory import tfactory_client as tc  # noqa: E402
from server.services import tf_writeback as wb  # noqa: E402

HEAD = "b" * 40
SOURCE = "a" * 40
PATH = "tests/test_example.py"


def _task(**writer_changes: object) -> dict[str, object]:
    writer = {
        "ok": True,
        "dry_run": False,
        "commit_sha": HEAD,
        "committed_paths": [PATH],
    }
    writer.update(writer_changes)
    return {
        "source_sha": SOURCE,
        "status_json": {"status": "triaged", "git_writer": writer},
    }


def test_accepts_exact_writeback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tc, "tfactory_task", lambda *_a, **_k: _task())
    assert (
        wb.writeback_base(tmp_path, HEAD, reader=lambda _h: ([SOURCE], [PATH]))
        == SOURCE
    )


@pytest.mark.parametrize(
    ("task", "parents", "files"),
    [
        (_task(), [], [PATH]),
        (_task(), [SOURCE, "c" * 40], [PATH]),
        (_task(), ["c" * 40], [PATH]),
        (_task(), [SOURCE], []),
        (_task(), [SOURCE], ["src/main.py"]),
        (_task(), [SOURCE], ["tests/other.py"]),
        (_task(committed_paths=[PATH] * 300), [SOURCE], [PATH] * 300),
        (_task(dry_run=True), [SOURCE], [PATH]),
        (_task(dry_run=None), [SOURCE], [PATH]),
        (
            _task(dry_run=False)
            | {
                "status_json": {
                    "status": "triaged",
                    "git_writer": {
                        "ok": True,
                        "commit_sha": HEAD,
                        "committed_paths": [PATH],
                    },
                }
            },
            [SOURCE],
            [PATH],
        ),
        (_task(ok=False), [SOURCE], [PATH]),
        (_task(commit_sha="c" * 40), [SOURCE], [PATH]),
        (_task() | {"source_sha": "bad"}, [SOURCE], [PATH]),
        (_task() | {"source_sha": HEAD}, [SOURCE], [PATH]),
        (_task(committed_paths=[]), [SOURCE], [PATH]),
        (
            _task() | {"status_json": {"status": "running", "git_writer": {}}},
            [SOURCE],
            [PATH],
        ),
    ],
)
def test_rejects_untrusted_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task: dict[str, object],
    parents: list[str],
    files: list[str],
) -> None:
    monkeypatch.setattr(tc, "tfactory_task", lambda *_a, **_k: task)
    assert wb.writeback_base(tmp_path, HEAD, reader=lambda _h: (parents, files)) is None


def test_rejects_bad_head_without_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tc, "tfactory_task", lambda *_a, **_k: pytest.fail("fetched"))
    assert (
        wb.writeback_base(tmp_path, "bad", reader=lambda _h: ([SOURCE], [PATH])) is None
    )


def test_reader_error_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tc, "tfactory_task", lambda *_a, **_k: _task())

    def broken(_head: str) -> tuple[list[str], list[str]]:
        raise OSError("offline")

    assert wb.writeback_base(tmp_path, HEAD, reader=broken) is None


@pytest.mark.parametrize(
    "path", [".github/tests/test_x.py", "tests/.github/test_x.py", "src/main.py"]
)
def test_non_test_paths(path: str) -> None:
    assert not wb._is_test_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "test_x.py",
        "x_test.py",
        "x_test.go",
        "x.test.ts",
        "x.spec.jsx",
        "spec/helper.txt",
    ],
)
def test_test_paths(path: str) -> None:
    assert wb._is_test_path(path)


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/owner/repo.git", ("owner", "repo")),
        ("git@github.com:owner/repo.git", ("owner", "repo")),
        ("https://example.com/owner/repo", None),
    ],
)
def test_remote_parsing(
    monkeypatch: pytest.MonkeyPatch, remote: str, expected: tuple[str, str] | None
) -> None:
    monkeypatch.setattr(
        wb.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=remote),
    )
    assert wb._github_repo(Path("project")) == expected


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (
            0,
            json.dumps(
                {
                    "parents": [{"sha": SOURCE}, {"sha": "c" * 40}],
                    "files": [{"filename": PATH}],
                }
            ),
            None,
        ),
        (1, "", None),
        (0, "{invalid", None),
        (
            0,
            json.dumps(
                {
                    "parents": [{"sha": SOURCE}],
                    "files": [
                        {
                            "filename": "tests/test_new.py",
                            "previous_filename": "tests/test_old.py",
                        }
                    ],
                }
            ),
            ([SOURCE], ["tests/test_new.py", "tests/test_old.py"]),
        ),
    ],
)
def test_gh_commit_reader(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    expected: tuple[list[str], list[str]] | None,
) -> None:
    monkeypatch.setattr(wb, "project_github_env", lambda *_a: nullcontext({}))
    monkeypatch.setattr(wb, "child_env", lambda: {})
    monkeypatch.setattr(
        wb.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=returncode, stdout=stdout),
    )
    assert wb._gh_commit("owner", "repo", HEAD, Path("project")) == expected


def test_default_reader_rejects_rename_from_non_test_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tc,
        "tfactory_task",
        lambda *_a, **_k: _task(committed_paths=["tests/test_new.py", "src/main.py"]),
    )
    monkeypatch.setattr(wb, "_github_repo", lambda _project: ("owner", "repo"))
    monkeypatch.setattr(wb, "project_github_env", lambda *_a: nullcontext({}))
    monkeypatch.setattr(wb, "child_env", lambda: {})
    monkeypatch.setattr(
        wb.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "parents": [{"sha": SOURCE}],
                    "files": [
                        {
                            "filename": "tests/test_new.py",
                            "previous_filename": "src/main.py",
                        }
                    ],
                }
            ),
        ),
    )
    assert wb.writeback_base(tmp_path, HEAD) is None
