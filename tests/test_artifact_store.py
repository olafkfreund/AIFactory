"""Unit tests for the vendored S3-compatible artifact-store client.

RFC-0017 Stage E / RFC-0016 #190: AIFactory vends ``core/artifact_store.py``
(a faithful copy of the Factory hub reference ``scripts/artifact_store.py``) so
build Jobs can pack/unpack the workspace through object storage instead of the
RWO local-path co-mount.

These tests pin the public key layout (apis/concurrency-conventions.md §2), the
value-object validation, and the workspace pack/unpack round-trip plus the
path-traversal guard — all driven through the module's in-memory fake S3, so
they run on every PR without needing Docker, S3, or MinIO.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from core import artifact_store as a_s  # noqa: E402


def test_key_layout_matches_conventions() -> None:
    ref = a_s.ArtifactRef(
        service="aifactory", job_id="job-7", role="workspace", correlation_key=42
    )
    assert ref.key() == "aifactory/42/job-7/workspace"
    assert ref.uri() == f"s3://{ref.bucket}/aifactory/42/job-7/workspace"


def test_missing_correlation_key_stays_well_formed() -> None:
    # correlation_key may be unknown before the upstream issue number is set;
    # it must record as `_` so the key stays re-keyable, not blank.
    ref = a_s.ArtifactRef(service="aifactory", job_id="j", role="build")
    assert ref.key() == "aifactory/_/j/build"


def test_subpath_hierarchy_is_preserved() -> None:
    ref = a_s.ArtifactRef(
        service="aifactory",
        job_id="j",
        role="build",
        correlation_key=1,
        path="dist/bin/app",
    )
    assert ref.key() == "aifactory/1/j/build/dist/bin/app"


@pytest.mark.parametrize("bad", [{"service": "nope"}, {"role": "nope"}])
def test_validation_rejects_unknown_service_or_role(bad: dict[str, str]) -> None:
    kwargs = {"service": "aifactory", "job_id": "j", "role": "build", **bad}
    with pytest.raises(ValueError):
        a_s.ArtifactRef(**kwargs)


def test_workspace_pack_unpack_round_trip(tmp_path: Path) -> None:
    # A packed workspace must survive the s3 round-trip byte-for-byte, including
    # nested paths — this is the RWO-comount replacement the Job relies on.
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "main.py").write_text("print('hi')\n")
    (src / "pkg" / "mod.py").write_text("X = 1\n")

    store = a_s._fake_store()
    ref = a_s.ArtifactRef(
        service="aifactory", job_id="j", role="workspace", correlation_key=7
    )
    uri = a_s.pack_workspace(store, ref, src)
    # pack lands on the canonical workspace archive object under the role prefix.
    assert uri == f"s3://{ref.bucket}/aifactory/7/j/workspace/{a_s.WORKSPACE_ARCHIVE}"

    dest = tmp_path / "dest"
    a_s.unpack_workspace(store, uri, dest)
    assert (dest / "main.py").read_text() == "print('hi')\n"
    assert (dest / "pkg" / "mod.py").read_text() == "X = 1\n"


@pytest.mark.parametrize("evil_name", ["../escape.txt", "/etc/passwd"])
def test_unpack_rejects_path_traversal(tmp_path: Path, evil_name: str) -> None:
    # An archive whose member escapes the destination must be refused before
    # anything is written — defends the Job's /work against a poisoned URI.
    blob = a_s._evil_archive(evil_name)
    with pytest.raises(Exception):  # noqa: B017 — guard raises ValueError/tarfile error
        a_s._safe_extract(blob, tmp_path / "out")
