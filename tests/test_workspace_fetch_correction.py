"""Correction Job handback validates a complete file set before applying it."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/backend"))
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


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file(data: bytes, old: bytes | None = None) -> dict[str, str | None]:
    return {
        "b64": base64.b64encode(data).decode(),
        "base_sha256": _hash(old) if old is not None else None,
    }


def _put(store: type[FakeStore], files: dict, deleted: dict) -> None:
    store.objects[workspace_fetch._correction_key("fid")] = json.dumps(
        {"files": files, "deleted": deleted}
    ).encode()


def test_fetch_applies_create_modify_delete(
    store: type[FakeStore], tmp_path: Path
) -> None:
    (tmp_path / "old").write_bytes(b"before")
    (tmp_path / "gone").write_bytes(b"gone")
    _put(
        store,
        {"new/sub": _file(b"new"), "old": _file(b"after", b"before")},
        {"gone": _hash(b"gone")},
    )
    assert workspace_fetch.fetch_correction(tmp_path, "fid") is True
    assert (tmp_path / "new/sub").read_bytes() == b"new"
    assert (tmp_path / "old").read_bytes() == b"after"
    assert not (tmp_path / "gone").exists()


def test_fetch_rejects_download_over_cap(
    store: type[FakeStore], tmp_path: Path
) -> None:
    store.objects[workspace_fetch._correction_key("fid")] = b" " * ((12 << 20) + 1)
    with pytest.raises(ValueError):
        workspace_fetch.fetch_correction(tmp_path, "fid")


@pytest.mark.parametrize("payload", [b"[]", b"null", b'{"files": [], "deleted": {}}'])
def test_fetch_rejects_non_object(
    store: type[FakeStore], tmp_path: Path, payload: bytes
) -> None:
    store.objects[workspace_fetch._correction_key("fid")] = payload
    with pytest.raises(ValueError):
        workspace_fetch.fetch_correction(tmp_path, "fid")


@pytest.mark.parametrize(
    "name", ["../x", ".git/x", ".aifactory/x", "a//b", "a/./b", "a\\b", "/x"]
)
def test_fetch_rejects_bad_paths(
    store: type[FakeStore], tmp_path: Path, name: str
) -> None:
    _put(store, {name: _file(b"bad")}, {})
    with pytest.raises(ValueError):
        workspace_fetch.fetch_correction(tmp_path, "fid")


@pytest.mark.parametrize("link", ["ancestor", "target"])
def test_fetch_rejects_symlinks(
    store: type[FakeStore], tmp_path: Path, link: str
) -> None:
    outside = tmp_path.parent / "outside-correction"
    outside.mkdir(exist_ok=True)
    (tmp_path / link).symlink_to(outside)
    _put(store, {f"{link}/x" if link == "ancestor" else link: _file(b"bad")}, {})
    with pytest.raises(ValueError):
        workspace_fetch.fetch_correction(tmp_path, "fid")


def test_fetch_rejects_overlap(store: type[FakeStore], tmp_path: Path) -> None:
    _put(store, {"a": _file(b"a"), "a/b": _file(b"b")}, {})
    with pytest.raises(ValueError):
        workspace_fetch.fetch_correction(tmp_path, "fid")


def test_fetch_rejects_write_and_delete_same_path(
    store: type[FakeStore], tmp_path: Path
) -> None:
    _put(store, {"a": _file(b"new")}, {"a": None})
    with pytest.raises(ValueError):
        workspace_fetch.fetch_correction(tmp_path, "fid")


def test_fetch_rejects_too_many(store: type[FakeStore], tmp_path: Path) -> None:
    _put(store, {str(i): _file(b"") for i in range(501)}, {})
    with pytest.raises(ValueError):
        workspace_fetch.fetch_correction(tmp_path, "fid")


def test_fetch_rejects_conflict_without_writes(
    store: type[FakeStore], tmp_path: Path
) -> None:
    (tmp_path / "old").write_bytes(b"local")
    _put(store, {"new": _file(b"new"), "old": _file(b"job", b"base")}, {})
    with pytest.raises(ValueError, match="conflict"):
        workspace_fetch.fetch_correction(tmp_path, "fid")
    assert not (tmp_path / "new").exists()
    assert (tmp_path / "old").read_bytes() == b"local"


def test_fetch_rejects_bad_base64(store: type[FakeStore], tmp_path: Path) -> None:
    _put(store, {"new": {"b64": "!!!", "base_sha256": None}}, {})
    with pytest.raises(ValueError):
        workspace_fetch.fetch_correction(tmp_path, "fid")


def test_fetch_rejects_decoded_size(
    store: type[FakeStore], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_fetch, "_CORRECTION_MAX_BYTES", 3)
    _put(store, {"new": _file(b"four")}, {})
    with pytest.raises(ValueError):
        workspace_fetch.fetch_correction(tmp_path, "fid")


@pytest.mark.parametrize(
    "value", [{"b64": "", "base_sha256": "bad"}, {"base_sha256": None}, "text"]
)
def test_fetch_rejects_bad_file_shape(
    store: type[FakeStore], tmp_path: Path, value: object
) -> None:
    _put(store, {"new": value}, {})
    with pytest.raises(ValueError):
        workspace_fetch.fetch_correction(tmp_path, "fid")


def test_fetch_rolls_back_after_replace_failure(
    store: type[FakeStore], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "first").write_bytes(b"old")
    _put(store, {"first": _file(b"new", b"old"), "second": _file(b"new")}, {})
    real_replace = os.replace
    calls = 0

    def fail_second(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("failed")
        real_replace(src, dst)

    monkeypatch.setattr(workspace_fetch.os, "replace", fail_second)
    assert workspace_fetch.fetch_correction(tmp_path, "fid") is False
    assert (tmp_path / "first").read_bytes() == b"old"
    assert not (tmp_path / "second").exists()


def test_push_includes_committed_untracked_and_deleted(
    store: type[FakeStore], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (tmp_path / "edit").write_bytes(b"base")
    (tmp_path / "gone").write_bytes(b"gone")
    git("add", ".")
    git("commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (tmp_path / "edit").write_bytes(b"job")
    (tmp_path / "gone").unlink()
    git("add", "-u")
    git("commit", "-qm", "job")
    (tmp_path / "new").write_bytes(b"untracked")
    control = tmp_path / ".aifactory"
    control.mkdir()
    (control / "ignored").write_bytes(b"control")
    monkeypatch.setenv(workspace_fetch.CORRECTION_FETCH_ID_ENV, "fid")
    assert workspace_fetch.maybe_push_correction(tmp_path, base)
    doc = json.loads(store.objects[workspace_fetch._correction_key("fid")])
    assert doc == {
        "files": {"edit": _file(b"job", b"base"), "new": _file(b"untracked")},
        "deleted": {"gone": _hash(b"gone")},
    }
    store.objects.clear()
    (tmp_path / "link").symlink_to(tmp_path / "new")
    assert workspace_fetch.maybe_push_correction(tmp_path, base) is False
    assert store.objects == {}


def test_push_noop_without_id(
    store: type[FakeStore], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(workspace_fetch.CORRECTION_FETCH_ID_ENV, raising=False)
    assert workspace_fetch.maybe_push_correction(tmp_path, "base") is False
    assert store.objects == {}
