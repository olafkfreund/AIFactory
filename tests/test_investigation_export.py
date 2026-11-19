"""Pinned investigation exports keep secrets and unsafe links out of /src."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "web-server"))

from server.database.models import Base
from server.services import investigation_export as exports
from server.services.project_settings_store import ProjectSettingsStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.org")
    git(repo, "config", "user.name", "Test")
    for name in (".env", "id_rsa", "a/.npmrc", "terraform.tfstate", "custom.private"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SECRET")
    (repo / "safe.txt").write_text("safe")
    (repo / "absolute").symlink_to("/etc/passwd")
    (repo / "escape").symlink_to("../../x")
    (repo / "safe-link").symlink_to("safe.txt")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")
    sha = git(repo, "rev-parse", "HEAD")
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    (repo / "safe.txt").write_text("local change")
    return repo, sha


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", str(root))
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "data-pvc")
    monkeypatch.setattr(exports.os.path, "ismount", lambda path: Path(path) == root)
    return root


def test_export_filters_and_does_not_change_checkout(repo, storage):
    path, sha = repo
    dest = storage / "investigations" / "run" / "src"
    head = git(path, "rev-parse", "HEAD")
    assert exports.export(path, sha, dest, "custom.private") == {"bytes": 4}
    assert (dest / "safe.txt").read_text() == "safe"
    assert (dest / "safe-link").is_symlink()
    assert (dest / ".aifactory-export-ok").exists()
    for name in (
        ".git",
        ".env",
        "id_rsa",
        "a/.npmrc",
        "terraform.tfstate",
        "custom.private",
        "absolute",
        "escape",
    ):
        assert not (dest / name).exists() and not (dest / name).is_symlink()
    assert git(path, "rev-parse", "HEAD") == head
    assert (path / "safe.txt").read_text() == "local change"


def test_unknown_sha_and_size_cap(repo, storage, monkeypatch):
    path, sha = repo
    dest = storage / "investigations" / "run" / "src"
    with pytest.raises(exports.ExportError):
        exports.export(path, "0" * 40, dest)
    monkeypatch.setattr(exports, "MAX_BYTES", 1)
    with pytest.raises(exports.ExportError, match="2 GiB"):
        exports.export(path, sha, dest)
    assert not dest.exists()


def test_extra_patterns_cannot_remove_exclusions(repo, storage):
    path, sha = repo
    dest = storage / "investigations" / "run" / "src"
    with pytest.raises(exports.ExportError, match="negation"):
        exports.export(path, sha, dest, "!.env")
    assert not dest.exists()


def test_storage_must_be_shared(repo, storage, monkeypatch):
    path, sha = repo
    dest = storage / "investigations" / "run" / "src"
    monkeypatch.delenv("AIFACTORY_SANDBOX_REPO_PVC")
    with pytest.raises(exports.ExportError, match="shared storage unavailable"):
        exports.export(path, sha, dest)
    monkeypatch.setenv("AIFACTORY_SANDBOX_REPO_PVC", "data-pvc")
    monkeypatch.setattr(exports.os.path, "ismount", lambda path: False)
    with pytest.raises(exports.ExportError, match="shared storage unavailable"):
        exports.export(path, sha, dest)


@pytest.mark.asyncio
async def test_settings_store_secret_paths_round_trip_and_reject_negation(
    tmp_path: Path,
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        store = ProjectSettingsStore(async_sessionmaker(engine, class_=AsyncSession))
        repo = tmp_path / "repo"
        repo.mkdir()
        await store.put_settings(
            repo, {"INVESTIGATE_SECRET_PATHS": "custom.private\nkeys/**"}
        )
        assert (await store.get_settings(repo))[
            "INVESTIGATE_SECRET_PATHS"
        ] == "custom.private\nkeys/**"
        with pytest.raises(ValueError, match="negation"):
            await store.put_settings(repo, {"INVESTIGATE_SECRET_PATHS": "!.env"})
    finally:
        await engine.dispose()
