"""Live-MinIO integration test for WorkspaceStore (Epic #35 #40 half-B PR-2).

Proves WorkspaceStore's full round-trip against a real S3-API-speaking
backend (MinIO, via the AIFACTORY_S3_ENDPOINT_URL override). The unit
tests in ``test_workspace_store.py`` cover semantics via fsspec's
LocalFileSystem; this module catches anything that depends on real
HTTP/S3 wire behavior (path-style vs virtual-hosted, listing
pagination, eventual-consistency assumptions, content-type
roundtrips, the boto3 cred chain).

## Skip-when-unreachable

Reads ``TEST_S3_URI_BASE`` and ``TEST_S3_ENDPOINT_URL`` env vars.
Skips the whole module when MinIO isn't reachable. Mirrors the
``TEST_REDIS_URL`` + ``TEST_POSTGRES_URL`` patterns. CI provides
MinIO as a service container so the test fires automatically.

Local dev runs without MinIO silently skip. To exercise locally::

    docker run -d --rm --name aif-test-minio \
      -p 9000:9000 \
      -e MINIO_ROOT_USER=minio \
      -e MINIO_ROOT_PASSWORD=minio123 \
      quay.io/minio/minio:latest server /data

    mc alias set local http://localhost:9000 minio minio123
    mc mb local/aifactory-test

    TEST_S3_URI_BASE=s3://aifactory-test/workspaces \
    TEST_S3_ENDPOINT_URL=http://localhost:9000 \
    AWS_ACCESS_KEY_ID=minio \
    AWS_SECRET_ACCESS_KEY=minio123 \
      pytest tests/test_workspace_store_integration.py -v
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import pytest

_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


from server.services.workspace_store import (
    MANIFEST_FILENAME,
    WorkspaceStore,
)


def _live_s3_reachable() -> bool:
    """Probe the configured S3 endpoint. False (and the module skips)
    when no env vars set OR the endpoint refuses connections."""
    uri_base = os.environ.get("TEST_S3_URI_BASE")
    if not uri_base:
        return False
    # Don't HEAD the bucket from here — fsspec will do that when the
    # tests run. We just want a "is something listening at all" probe.
    endpoint = os.environ.get("TEST_S3_ENDPOINT_URL")
    if endpoint:
        import socket
        from urllib.parse import urlparse

        u = urlparse(endpoint)
        try:
            with socket.create_connection((u.hostname, u.port or 80), timeout=2):
                return True
        except OSError:
            return False
    # No endpoint override → real AWS S3, assume reachable if env set.
    return True


@pytest.fixture(scope="module")
def live_store() -> WorkspaceStore:
    """Build a WorkspaceStore pointed at the live MinIO/S3.

    Sets the FSSPEC_S3_* envs from TEST_S3_* if provided so the store's
    fsspec lookup honors the endpoint override.
    """
    if not _live_s3_reachable():
        pytest.skip(
            "Live S3/MinIO not reachable; integration test skipped. "
            "Set TEST_S3_URI_BASE + TEST_S3_ENDPOINT_URL + AWS_* creds "
            "to fire this against a local MinIO."
        )

    # Translate TEST_* -> AIFACTORY_S3_* so the store's _open_fs picks
    # up the endpoint override automatically.
    endpoint = os.environ.get("TEST_S3_ENDPOINT_URL")
    if endpoint:
        os.environ["AIFACTORY_S3_ENDPOINT_URL"] = endpoint
    # MinIO needs path-style; AWS itself accepts auto.
    if endpoint:
        os.environ.setdefault("AIFACTORY_S3_ADDRESSING_STYLE", "path")

    store = WorkspaceStore(base_uri=os.environ["TEST_S3_URI_BASE"])

    # Auto-create the bucket so CI and local dev both work without an
    # external ``mc mb`` setup step. Idempotent — ignores already-
    # exists errors.
    # ponytail: fsspec backends (s3fs/local/etc) raise different, backend-
    # specific error types for "bucket already exists" or "no bucket concept",
    # so this stays Exception-wide rather than guessing one type -- it's test
    # fixture setup, not production code making a decision on the result
    with contextlib.suppress(Exception):
        fs, prefix = store._open_fs()
        bucket = prefix.split("/", 1)[0] if "/" in prefix else prefix
        with contextlib.suppress(Exception):
            fs.mkdir(bucket)

    return store


@pytest.fixture
def project_workspace(tmp_path: Path) -> Path:
    """Mini representative project workspace (cloned-repo-shaped)."""
    d = tmp_path / "my-repo"
    d.mkdir()
    (d / "README.md").write_text("# integration-test\n")
    (d / ".aifactory" / "specs" / "001").mkdir(parents=True)
    (d / ".aifactory" / "specs" / "001" / "spec.md").write_text(
        "# Spec 001\nIntegration test workspace.\n"
    )
    (d / ".git" / "objects" / "ab").mkdir(parents=True)
    (d / ".git" / "objects" / "ab" / "feed").write_bytes(b"\x00\x01\x02BINARY")
    exe = d / "scripts" / "run.sh"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\necho live\n")
    exe.chmod(0o755)
    return d


def _unique_project_id() -> str:
    """Each test run uses a fresh project_id so concurrent test runs
    against the same MinIO don't stomp each other."""
    import uuid

    return f"itest-{uuid.uuid4().hex[:12]}"


@pytest.mark.asyncio
async def test_round_trip_against_live_minio(
    live_store,
    project_workspace,
    tmp_path,
):
    """Upload to MinIO, verify the manifest landed, download to a
    fresh dir, byte-for-byte match. This is the load-bearing proof
    that the spec's contract works against a real S3 wire."""
    org_id = "live-test"
    project_id = _unique_project_id()

    await live_store.upload_project(
        org_id=org_id,
        project_id=project_id,
        local_path=project_workspace,
        triggered_by_task_id="001:demo",
        triggered_by_phase="completed",
    )
    # Manifest must exist now.
    assert (
        await live_store.project_exists(
            org_id=org_id,
            project_id=project_id,
        )
    ) is True

    restored = tmp_path / "restored"
    ok = await live_store.download_project(
        org_id=org_id,
        project_id=project_id,
        local_path=restored,
    )
    assert ok is True

    # Same file set.
    src = sorted(
        f.relative_to(project_workspace).as_posix()
        for f in project_workspace.rglob("*")
        if f.is_file()
    )
    dst = sorted(
        f.relative_to(restored).as_posix() for f in restored.rglob("*") if f.is_file()
    )
    assert src == dst

    # Bytes match.
    assert (restored / "README.md").read_text() == "# integration-test\n"
    assert (restored / ".git/objects/ab/feed").read_bytes() == b"\x00\x01\x02BINARY"

    # +x bit re-applied via manifest.
    assert (restored / "scripts/run.sh").stat().st_mode & 0o111 != 0


@pytest.mark.asyncio
async def test_partial_upload_simulation_against_live_minio(
    live_store,
    project_workspace,
    tmp_path,
):
    """Write some files via fsspec WITHOUT writing the manifest, then
    confirm download treats it as missing and doesn't pollute the
    local dir. Defends the partial-upload-detection contract against
    real S3 listing behavior (eventual consistency edge cases)."""
    import fsspec

    org_id = "live-test"
    project_id = _unique_project_id()

    # Reach under the store and write a stray file at the expected
    # key prefix WITHOUT a manifest.
    fs, prefix = live_store._open_fs()
    key_root = f"{prefix}/{org_id}/{project_id}"
    # Use fsspec to put a single file; deliberately skip the manifest
    # write that upload_project would do last.
    stray = project_workspace / "README.md"
    fs.makedirs(key_root, exist_ok=True)
    fs.put_file(str(stray), f"{key_root}/README.md")

    # project_exists must be False (no manifest = no snapshot).
    assert (
        await live_store.project_exists(
            org_id=org_id,
            project_id=project_id,
        )
    ) is False

    # download must return False AND must not leave the partial file
    # behind locally.
    restored = tmp_path / "should-be-empty"
    ok = await live_store.download_project(
        org_id=org_id,
        project_id=project_id,
        local_path=restored,
    )
    assert ok is False
    assert not (restored / "README.md").exists()
