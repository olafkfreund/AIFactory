"""Unit tests for the graphify graph cache (#804).

Covers the acceptance behaviours: a cache hit skips the subprocess build, a
cache miss builds and uploads, and ALL storage errors are swallowed — a cache
failure must never fail a build.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest
from core import artifact_store, graphify_cache
from core.graphify_cache import (
    cache_key,
    ensure_graph,
    fetch_cached_graph,
    store_cached_graph,
)


class _FakeStore:
    """In-memory stand-in for core.artifact_store.ArtifactStore."""

    objects: ClassVar[dict[str, bytes]] = {}

    def get_bytes(self, key: str) -> bytes:
        return _FakeStore.objects[key]

    def put_bytes(self, key: str, data: bytes, _content_type: str | None = None) -> str:
        _FakeStore.objects[key] = data
        return f"s3://factory-artifacts/{key}"


class _BrokenStore:
    """A store whose every operation raises (MinIO unreachable)."""

    def get_bytes(self, _key: str) -> bytes:
        raise RuntimeError("store unreachable")

    def put_bytes(
        self, _key: str, _data: bytes, _content_type: str | None = None
    ) -> str:
        raise RuntimeError("store unreachable")


def _run(*cmd: str) -> str:
    """Run a trusted test-fixture command (git on paths we created)."""
    res = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, check=True
    )
    return res.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny git repo with one commit and an origin remote."""
    _run("git", "init", "-q", str(tmp_path))
    _run("git", "-C", str(tmp_path), "config", "user.email", "t@t")
    _run("git", "-C", str(tmp_path), "config", "user.name", "t")
    _run(
        "git", "-C", str(tmp_path),
        "remote", "add", "origin", "git@github.com:acme/widgets.git",
    )  # fmt: skip
    (tmp_path / "a.py").write_text("x = 1\n")
    _run("git", "-C", str(tmp_path), "add", ".")
    _run("git", "-C", str(tmp_path), "commit", "-q", "-m", "init")
    return tmp_path


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> type[_FakeStore]:
    _FakeStore.objects = {}
    monkeypatch.setattr(artifact_store, "ArtifactStore", _FakeStore)
    return _FakeStore


_REAL_RUN = subprocess.run


def _intercept_graphify(on_build: Callable[[], None]) -> Callable[..., Any]:
    """A subprocess.run stand-in that intercepts only the graphify build call;
    the module's own git queries pass through to the real subprocess.run."""

    def run(cmd: list[str], *args: Any, **kwargs: Any) -> Any:
        if cmd and cmd[0] == "graphify":
            return on_build()
        return _REAL_RUN(cmd, *args, **kwargs)

    return run


def _head(repo: Path) -> str:
    return _run("git", "-C", str(repo), "rev-parse", "HEAD")


def test_cache_key_is_repo_slug_plus_head_commit(repo: Path) -> None:
    assert cache_key(repo) == f"graphify/acme/widgets/{_head(repo)}/graph.json"


def test_cache_key_none_outside_a_git_repo(tmp_path: Path) -> None:
    assert cache_key(tmp_path) is None


def test_cache_key_falls_back_to_dir_name_without_remote(repo: Path) -> None:
    _run("git", "-C", str(repo), "remote", "remove", "origin")
    assert cache_key(repo) == f"graphify/{repo.name}/{_head(repo)}/graph.json"


def test_hit_writes_graph_and_skips_build(
    repo: Path, fake_store: type[_FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    key = cache_key(repo)
    assert key is not None
    fake_store.objects[key] = b'{"nodes": []}'

    def _no_build() -> None:
        raise AssertionError("cache hit must skip the graphify build subprocess")

    monkeypatch.setattr(
        graphify_cache.subprocess, "run", _intercept_graphify(_no_build)
    )
    graph_json = repo / "graphify-out" / "graph.json"
    ensure_graph(repo, graph_json)
    assert graph_json.read_bytes() == b'{"nodes": []}'


def test_miss_builds_and_uploads(
    repo: Path, fake_store: type[_FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    graph_json = repo / "graphify-out" / "graph.json"

    def _build() -> None:
        graph_json.parent.mkdir(parents=True, exist_ok=True)
        graph_json.write_bytes(b'{"built": true}')

    monkeypatch.setattr(graphify_cache.subprocess, "run", _intercept_graphify(_build))
    ensure_graph(repo, graph_json)
    key = cache_key(repo)
    assert key is not None
    assert fake_store.objects[key] == b'{"built": true}'


def test_miss_with_failed_build_uploads_nothing(
    repo: Path, fake_store: type[_FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail() -> None:
        raise FileNotFoundError("graphify not on PATH")

    monkeypatch.setattr(graphify_cache.subprocess, "run", _intercept_graphify(_fail))
    ensure_graph(repo, repo / "graphify-out" / "graph.json")
    assert fake_store.objects == {}


def test_storage_errors_are_swallowed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifact_store, "ArtifactStore", _BrokenStore)
    graph_json = repo / "graphify-out" / "graph.json"
    assert fetch_cached_graph(repo, graph_json) is False
    graph_json.parent.mkdir(parents=True)
    graph_json.write_bytes(b"{}")
    assert store_cached_graph(repo, graph_json) is False
    # ensure_graph never raises even with a broken store and no graphify CLI.
    graph_json.unlink()
    ensure_graph(repo, graph_json)


def test_existing_graph_short_circuits(
    repo: Path, fake_store: type[_FakeStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    graph_json = repo / "graphify-out" / "graph.json"
    graph_json.parent.mkdir(parents=True)
    graph_json.write_bytes(b"{}")

    def _no_build() -> None:
        raise AssertionError("existing graph must not trigger a build")

    monkeypatch.setattr(
        graphify_cache.subprocess, "run", _intercept_graphify(_no_build)
    )
    ensure_graph(repo, graph_json)
    assert fake_store.objects == {}


@pytest.mark.usefixtures("fake_store")
def test_store_no_ops_without_a_graph_file(repo: Path) -> None:
    assert store_cached_graph(repo, repo / "graphify-out" / "graph.json") is False
