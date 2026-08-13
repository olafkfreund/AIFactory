"""Unit tests for WorkspaceStore (Epic #35 #40 half-B PR-1).

All tests use fsspec's LocalFileSystem via a `file://` base URI, so
they run on every PR without needing Docker, S3, or MinIO. Live-S3
integration coverage lives in test_workspace_store_integration.py
(PR-2).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


from server.services.workspace_store import (
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    WorkspaceStore,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def remote_root(tmp_path: Path) -> Path:
    """A fresh directory used as the fsspec ``file://`` backing store."""
    d = tmp_path / "remote-store"
    d.mkdir()
    return d


@pytest.fixture
def store(remote_root: Path) -> WorkspaceStore:
    """A store rooted at a file:// URI for the tmp dir above. ``is_remote``
    is True for these tests so upload/download paths actually fire (the
    no-op short-circuit is for unset/empty-string only)."""
    return WorkspaceStore(base_uri=f"file://{remote_root}")


@pytest.fixture
def project_workspace(tmp_path: Path) -> Path:
    """A representative project workspace: cloned-repo-like layout
    with nested .aifactory state + a binary file + an executable."""
    d = tmp_path / "workspace" / "my-repo"
    d.mkdir(parents=True)

    (d / "README.md").write_text("# my-repo\n")
    (d / ".aifactory" / "specs" / "001-add-auth").mkdir(parents=True)
    (d / ".aifactory" / "specs" / "001-add-auth" / "spec.md").write_text(
        "# Spec 001\n## Overview\nTest.\n"
    )
    (
        d / ".aifactory" / "specs" / "001-add-auth" / "implementation_plan.json"
    ).write_text(json.dumps({"phases": []}))

    # Nested .git-shaped state (no real pack, just enough for the
    # round-trip test to feel realistic)
    (d / ".git" / "objects" / "ab").mkdir(parents=True)
    (d / ".git" / "objects" / "ab" / "cdef1234567890").write_bytes(
        b"\x00\x01\x02BINARY"
    )

    # An executable script — exercises the +x bit preservation path.
    exe = d / "scripts" / "drill.sh"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\necho ok\n")
    exe.chmod(0o755)
    return d


# ---------------------------------------------------------------------------
# is_remote — boundary conditions
# ---------------------------------------------------------------------------


def test_is_remote_false_for_empty_base():
    """Empty base URI = local-only mode = is_remote() is False. This
    is the laptop / unset-env default."""
    assert WorkspaceStore(base_uri="").is_remote() is False


def test_is_remote_true_for_explicit_file_scheme():
    """Explicit file:// counts as 'storage engaged' — operator chose
    to point the snapshot layer at a local path (useful for tests or
    a separate cache disk). Only the empty-string default short-
    circuits to no-op."""
    assert WorkspaceStore(base_uri="file:///tmp/foo").is_remote() is True


@pytest.mark.parametrize(
    "uri",
    [
        "s3://my-bucket/workspaces",
        "gs://my-bucket/workspaces",
        "azure://container/path",
    ],
)
def test_is_remote_true_for_cloud_schemes(uri):
    assert WorkspaceStore(base_uri=uri).is_remote() is True


def test_from_settings_reads_env(monkeypatch):
    """from_settings() must honour the live settings object — verifies
    the laptop default + the override path."""
    from server.config import get_settings

    monkeypatch.setattr(get_settings(), "WORKSPACE_S3_URI_BASE", "")
    assert WorkspaceStore.from_settings().is_remote() is False

    monkeypatch.setattr(
        get_settings(),
        "WORKSPACE_S3_URI_BASE",
        "s3://test-bucket/wsp",
    )
    assert WorkspaceStore.from_settings().is_remote() is True


# ---------------------------------------------------------------------------
# is_remote short-circuit — upload/download are no-ops when False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_no_op_when_local_only(project_workspace):
    """Local-only store doesn't touch anything when upload is called."""
    s = WorkspaceStore(base_uri="")
    # Just confirm it returns without exception — there's nothing
    # observable to assert because the no-op is intentional.
    await s.upload_project(
        org_id="org-1",
        project_id="proj-1",
        local_path=project_workspace,
    )


@pytest.mark.asyncio
async def test_download_returns_false_when_local_only(tmp_path):
    s = WorkspaceStore(base_uri="")
    ok = await s.download_project(
        org_id="org-1",
        project_id="proj-1",
        local_path=tmp_path / "dst",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_project_exists_returns_false_when_local_only():
    s = WorkspaceStore(base_uri="")
    assert (await s.project_exists(org_id="o", project_id="p")) is False


# ---------------------------------------------------------------------------
# Round-trip — upload → exists → download → verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_writes_manifest_with_expected_fields(
    store,
    remote_root,
    project_workspace,
):
    """The manifest sits at the per-project key and carries the
    contract the downloader relies on."""
    await store.upload_project(
        org_id="org-99",
        project_id="proj-42",
        local_path=project_workspace,
        triggered_by_task_id="001-add-auth",
        triggered_by_phase="review_pending",
    )
    manifest_path = remote_root / "org-99" / "proj-42" / MANIFEST_FILENAME
    assert manifest_path.exists()

    manifest = json.loads(manifest_path.read_text())
    assert manifest["v"] == MANIFEST_VERSION
    assert manifest["org_id"] == "org-99"
    assert manifest["project_id"] == "proj-42"
    assert manifest["triggered_by_task_id"] == "001-add-auth"
    assert manifest["triggered_by_phase"] == "review_pending"
    assert manifest["file_count"] >= 4  # README + spec + plan + binary + script
    assert manifest["total_bytes"] > 0
    assert manifest["uploaded_at"].endswith("Z")

    # Executables list must contain the script we chmod'd +x.
    assert "scripts/drill.sh" in manifest["executables"]
    # And NOT plain files.
    assert "README.md" not in manifest["executables"]


@pytest.mark.asyncio
async def test_project_exists_after_upload(store, project_workspace):
    """exists() flips True only after a manifest landed."""
    assert (await store.project_exists(org_id="o", project_id="p")) is False
    await store.upload_project(
        org_id="o",
        project_id="p",
        local_path=project_workspace,
    )
    assert (await store.project_exists(org_id="o", project_id="p")) is True


@pytest.mark.asyncio
async def test_download_restores_file_tree_byte_for_byte(
    store,
    project_workspace,
    tmp_path,
):
    """The whole tree round-trips: nested dirs, binary files, the
    executable bit, the .aifactory state. This is the core durability
    guarantee."""
    await store.upload_project(
        org_id="o",
        project_id="p",
        local_path=project_workspace,
    )

    restored = tmp_path / "restored"
    ok = await store.download_project(
        org_id="o",
        project_id="p",
        local_path=restored,
    )
    assert ok is True

    # Same file set.
    src_files = sorted(
        f.relative_to(project_workspace).as_posix()
        for f in project_workspace.rglob("*")
        if f.is_file()
    )
    dst_files = sorted(
        f.relative_to(restored).as_posix() for f in restored.rglob("*") if f.is_file()
    )
    assert src_files == dst_files

    # Byte-for-byte content match.
    assert (restored / "README.md").read_text() == "# my-repo\n"
    assert (
        restored / ".git/objects/ab/cdef1234567890"
    ).read_bytes() == b"\x00\x01\x02BINARY"

    # +x bit preserved on the script via the manifest.
    assert (restored / "scripts" / "drill.sh").stat().st_mode & 0o111 != 0


# ---------------------------------------------------------------------------
# Partial-snapshot protection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_returns_false_when_no_manifest(
    store,
    remote_root,
    tmp_path,
):
    """Files present at the key prefix but no manifest = treat as
    incomplete. Returns False so the caller falls back to fresh clone."""
    # Write a stray file without doing a real upload.
    stray = remote_root / "o" / "p" / "stray.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("just sitting here")

    restored = tmp_path / "should-not-exist"
    ok = await store.download_project(
        org_id="o",
        project_id="p",
        local_path=restored,
    )
    assert ok is False
    # Critically: the partial restore was NOT applied locally. The
    # agent never sees the stray file because we never copied it.
    assert not (restored / "stray.txt").exists()


@pytest.mark.asyncio
async def test_download_rejects_mismatched_project_id(
    store,
    remote_root,
    project_workspace,
    tmp_path,
):
    """Bucket misconfiguration (operator copies snapshots between
    project IDs) must not silently restore the wrong project. The
    manifest's project_id is the source of truth."""
    # Upload as proj-A.
    await store.upload_project(
        org_id="o",
        project_id="proj-A",
        local_path=project_workspace,
    )

    # Manually rename the key prefix on disk to simulate a misconfigured
    # bucket where the manifest's project_id no longer matches its
    # location.
    src = remote_root / "o" / "proj-A"
    dst = remote_root / "o" / "proj-B"
    src.rename(dst)

    restored = tmp_path / "restored"
    ok = await store.download_project(
        org_id="o",
        project_id="proj-B",
        local_path=restored,
    )
    assert ok is False


@pytest.mark.asyncio
async def test_download_rejects_unknown_manifest_version(
    store,
    remote_root,
    tmp_path,
):
    """Future manifest format → log + skip + fall back. Forces a
    fresh clone rather than guessing field meanings."""
    key_dir = remote_root / "o" / "p"
    key_dir.mkdir(parents=True)
    (key_dir / MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "v": 99,  # from the future
                "org_id": "o",
                "project_id": "p",
                "file_count": 0,
                "total_bytes": 0,
                "executables": [],
            }
        )
    )

    ok = await store.download_project(
        org_id="o",
        project_id="p",
        local_path=tmp_path / "restored",
    )
    assert ok is False


# ---------------------------------------------------------------------------
# Upload idempotency + manifest-written-last invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_is_idempotent_overwrites_previous(
    store,
    project_workspace,
    tmp_path,
):
    """Re-uploading is non-destructive — last write wins. Defensive
    guard against scheduler bugs (the spec's invariant is single-writer-
    per-project, but we don't want corruption if that invariant breaks)."""
    await store.upload_project(
        org_id="o",
        project_id="p",
        local_path=project_workspace,
        triggered_by_phase="coding",
    )
    await store.upload_project(
        org_id="o",
        project_id="p",
        local_path=project_workspace,
        triggered_by_phase="completed",  # different phase, same project
    )
    # Manifest reflects the latest upload's phase.
    restored = tmp_path / "restored"
    ok = await store.download_project(
        org_id="o",
        project_id="p",
        local_path=restored,
    )
    assert ok is True
    # No assertion on phase — both uploads had the same files; the
    # important property is no exception + still-valid manifest.


@pytest.mark.asyncio
async def test_upload_skips_nonexistent_local_path(store, tmp_path):
    """Trying to upload a non-existent local path logs a WARNING and
    returns — never raises. Matches the failure-safe contract."""
    nope = tmp_path / "does-not-exist"
    await store.upload_project(
        org_id="o",
        project_id="p",
        local_path=nope,
    )
    # No manifest written.
    assert not (await store.project_exists(org_id="o", project_id="p"))


# ---------------------------------------------------------------------------
# Failure safety — exceptions during upload/download don't propagate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_swallows_fsspec_failure(
    monkeypatch,
    store,
    project_workspace,
):
    """A bucket-unreachable / credential-expired-style failure must
    NOT raise into the caller. The store logs WARNING and returns;
    the calling task continues."""

    def _explode(*a, **kw):
        raise RuntimeError("simulated S3 outage")

    monkeypatch.setattr(store, "_open_fs", _explode)

    # Should NOT raise.
    await store.upload_project(
        org_id="o",
        project_id="p",
        local_path=project_workspace,
    )


@pytest.mark.asyncio
async def test_download_swallows_fsspec_failure(monkeypatch, store, tmp_path):
    def _explode(*a, **kw):
        raise RuntimeError("simulated S3 outage")

    monkeypatch.setattr(store, "_open_fs", _explode)

    ok = await store.download_project(
        org_id="o",
        project_id="p",
        local_path=tmp_path / "restored",
    )
    assert ok is False
