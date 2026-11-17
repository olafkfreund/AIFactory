"""PR review Job result is fetched as one validated file (A4, #1932)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
for _path in (_REPO / "apps/web-server", _REPO / "apps/backend"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core import artifact_store, workspace_fetch  # noqa: E402


class FakeStore:
    objects: dict[str, bytes] = {}

    def get_bytes(self, key: str, max_bytes: int | None = None) -> bytes:
        data = self.objects[key]
        if max_bytes is not None and len(data) > max_bytes:
            raise ValueError("too large")
        return data

    def put_bytes(self, key: str, data: bytes, *_a: object, **_k: object) -> None:
        self.objects[key] = data


@pytest.fixture(autouse=True)
def store(monkeypatch: pytest.MonkeyPatch) -> type[FakeStore]:
    FakeStore.objects = {}
    monkeypatch.setattr(artifact_store, "ArtifactStore", FakeStore)
    return FakeStore


def _key(pr: int = 7) -> str:
    return workspace_fetch._pr_review_key("fid", pr)


def test_fetch_writes_valid_object(store: type[FakeStore], tmp_path: Path) -> None:
    store.objects[_key()] = b'{"findings": []}'
    assert workspace_fetch.fetch_pr_review(tmp_path, "fid", 7) is True
    out = tmp_path / ".aifactory/github/pr/review_7.json"
    assert json.loads(out.read_text()) == {"findings": []}
    assert out.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("payload", [b"[1]", b"not json", b'"s"'])
def test_fetch_rejects_non_object(
    store: type[FakeStore], tmp_path: Path, payload: bytes
) -> None:
    store.objects[_key()] = payload
    with pytest.raises(ValueError):
        workspace_fetch.fetch_pr_review(tmp_path, "fid", 7)
    assert not (tmp_path / ".aifactory/github/pr/review_7.json").exists()


def test_fetch_rejects_oversize(
    store: type[FakeStore], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_fetch, "_PR_REVIEW_MAX_BYTES", 8)
    store.objects[_key()] = b'{"a": "0123456789"}'
    with pytest.raises(ValueError):
        workspace_fetch.fetch_pr_review(tmp_path, "fid", 7)


def test_fetch_missing_result_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        workspace_fetch.fetch_pr_review(tmp_path, "fid", 7)


@pytest.mark.parametrize(
    "link", [".aifactory", ".aifactory/github", ".aifactory/github/pr"]
)
def test_fetch_refuses_symlinked_ancestor(
    store: type[FakeStore], tmp_path: Path, link: str
) -> None:
    store.objects[_key()] = b"{}"
    outside = tmp_path / "outside"
    outside.mkdir()
    proj = tmp_path / "proj"
    target = proj / link
    target.parent.mkdir(parents=True)
    os.symlink(outside, target)
    with pytest.raises(ValueError):
        workspace_fetch.fetch_pr_review(proj, "fid", 7)
    assert not list(outside.rglob("*.json"))


def test_push_only_the_review_file(
    store: type[FakeStore], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(workspace_fetch.PR_REVIEW_FETCH_ID_ENV, "fid")
    d = tmp_path / ".aifactory/github/pr"
    d.mkdir(parents=True)
    (d / "review_7.json").write_text('{"ok": 1}')
    (d / "other.json").write_text("{}")
    assert workspace_fetch.maybe_push_pr_review(7, tmp_path) is True
    assert list(store.objects) == [_key()]


def test_push_is_noop_without_fetch_id(
    store: type[FakeStore], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(workspace_fetch.PR_REVIEW_FETCH_ID_ENV, raising=False)
    assert workspace_fetch.maybe_push_pr_review(7, tmp_path) is False
    assert store.objects == {}


def test_fetch_cancelled_before_commit_leaves_no_file(
    store: type[FakeStore], tmp_path: Path
) -> None:
    import threading

    store.objects[_key()] = b"{}"
    cancel = threading.Event()
    real = workspace_fetch._checked_pr_review_dest

    def dest_then_cancel(path: Path) -> Path:
        out = real(path)
        cancel.set()  # the stop arrives while the file is being written
        return out

    workspace_fetch._checked_pr_review_dest = dest_then_cancel
    try:
        assert workspace_fetch.fetch_pr_review(tmp_path, "fid", 7, cancel) is False
    finally:
        workspace_fetch._checked_pr_review_dest = real
    assert not (tmp_path / ".aifactory/github/pr/review_7.json").exists()
