"""Memory survives a PACKED-path (Job) build — the root cause (#1038).

Three fixes failed before this one because all three assumed the Job's `/work`
was durable. It is not, and `core/job_dispatch.py` says so plainly: a Job gets
the data PVC co-mounted at /work ONLY when `data_pvc` and `worktree_subpath` are
both set. On the packed path (`WORKSPACE_URI` present) it gets an **emptyDir**,
and the workspace is unpacked into it at start.

So a Job's filesystem is write-once-and-discard. CODE escapes via `git push`;
everything else escapes only if it is explicitly pushed to object storage. That
is why `workspace_fetch` already carries the branch, the usage file, the task
logs and the plan — each added after the same bug was hit again. Memory is the
fifth, and was simply never added.
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from core import workspace_fetch as wf  # noqa: E402


class _Store:
    """Stand-in ArtifactStore; the real one needs MinIO."""

    blobs: dict[str, bytes] = {}

    def put_bytes(self, key, data, content_type, role=None):
        _Store.blobs[key] = data

    def get_bytes(self, key):
        return _Store.blobs[key]


@pytest.fixture(autouse=True)
def _store(monkeypatch):
    _Store.blobs = {}
    import core.artifact_store as a

    monkeypatch.setattr(a, "ArtifactStore", _Store)
    yield


def _insight(spec: Path, name: str, body: str) -> None:
    d = spec / "memory" / "session_insights"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)


# ── the round trip that was missing ──────────────────────────────────────────


def test_memory_survives_the_packed_path(tmp_path, monkeypatch):
    """MUTATION GUARD: without the push, a packed build's memory dies with the pod."""
    monkeypatch.setenv(wf.WORKSPACE_URI_ENV, "s3://bucket/ws.tar.gz")

    job_spec = tmp_path / "work" / ".aifactory" / "specs" / "088"
    _insight(job_spec, "session_001.json", '"the cache is cold on first hit"')

    assert wf.maybe_push_memory(job_spec, "088")

    # The Job's filesystem is gone; the control plane has its own spec dir.
    control_spec = tmp_path / "project" / ".aifactory" / "specs" / "088"
    control_spec.mkdir(parents=True)
    assert wf.maybe_fetch_memory(control_spec, "088")

    carried = control_spec / "memory" / "session_insights" / "session_001.json"
    assert carried.exists(), "the packed build's memory did not survive"
    assert "cache is cold" in carried.read_text()


def test_off_the_packed_path_the_push_is_a_no_op(tmp_path, monkeypatch):
    """On the co-mount path the spec dir is already durable; pushing is waste."""
    monkeypatch.delenv(wf.WORKSPACE_URI_ENV, raising=False)
    spec = tmp_path / "specs" / "088"
    _insight(spec, "a.json", "{}")
    assert wf.maybe_push_memory(spec, "088") is False


def test_fetching_merges_and_never_clears(tmp_path, monkeypatch):
    """MUTATION GUARD: the destination accumulates; a replacing fetch would
    discard exactly what this chain exists to keep."""
    monkeypatch.setenv(wf.WORKSPACE_URI_ENV, "s3://bucket/ws.tar.gz")
    job_spec = tmp_path / "work" / "088"
    _insight(job_spec, "new.json", '"new"')
    wf.maybe_push_memory(job_spec, "088")

    control_spec = tmp_path / "project" / ".aifactory" / "specs" / "088"
    _insight(control_spec, "existing.json", '"existing"')
    assert wf.maybe_fetch_memory(control_spec, "088")

    names = {p.name for p in (control_spec / "memory" / "session_insights").iterdir()}
    assert names == {"existing.json", "new.json"}


def test_an_empty_memory_dir_is_not_pushed(tmp_path, monkeypatch):
    monkeypatch.setenv(wf.WORKSPACE_URI_ENV, "s3://bucket/ws.tar.gz")
    spec = tmp_path / "088"
    (spec / "memory").mkdir(parents=True)
    assert wf.maybe_push_memory(spec, "088") is False


def test_nothing_pushed_means_nothing_fetched(tmp_path):
    spec = tmp_path / "088"
    spec.mkdir()
    assert wf.maybe_fetch_memory(spec, "088") is False


def test_the_extraction_filter_is_pinned_explicitly():
    """tarfile extraction is a classic path-traversal sink.

    Python 3.14 (PEP 706) already defaults to ``filter="data"``, so a behavioural
    test cannot distinguish its removal on this runtime — I mutated it away and
    every test still passed. The guard is therefore on the SOURCE: the filter
    stays explicit so the protection does not silently depend on a language
    default that differed in 3.11-3.13 and could differ again.
    """
    src = (_BACKEND / "core" / "workspace_fetch.py").read_text()
    body = src.split("def maybe_fetch_memory", 1)[1]
    # Asserted on the CALL, not on the text anywhere in the function: a comment
    # mentioning filter="data" must not be enough to satisfy this. (First
    # version of this test did exactly that and passed with the code removed.)
    # The rest of the CALL LINE. Splitting on the first ")" would land inside
    # str(dest) and truncate before the filter — which is how the first version
    # of this assertion failed against correct code.
    call = body.split("tar.extractall(", 1)[1].split("\n", 1)[0]
    assert 'filter="data"' in call, "the traversal filter was dropped from extractall"


def test_a_crafted_archive_cannot_escape_the_spec_dir(tmp_path, monkeypatch):
    """The behaviour, for the runtime we actually ship on."""
    monkeypatch.setenv(wf.WORKSPACE_URI_ENV, "s3://bucket/ws.tar.gz")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escaped.txt")
        payload = b"pwned"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    _Store.blobs[wf._memory_key("088")] = buf.getvalue()

    spec = tmp_path / "project" / "specs" / "088"
    spec.mkdir(parents=True)
    wf.maybe_fetch_memory(spec, "088")

    assert not (tmp_path / "project" / "specs" / "escaped.txt").exists()
    assert not (tmp_path / "project" / "escaped.txt").exists()


# ── both ends are wired ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,needle",
    [
        ("apps/backend/cli/main.py", "maybe_push_memory(spec_dir, spec_dir.name)"),
        (
            "apps/web-server/server/services/completion.py",
            "maybe_fetch_memory(spec_dir, spec_id)",
        ),
        (
            "apps/web-server/server/services/completion.py",
            "_pool_memory_at_project_level(spec_dir)",
        ),
    ],
)
def test_both_ends_are_wired(path, needle):
    """A push with no fetch, or a helper nobody calls, is decoration — the exact
    failure mode of #1031/#1036/#1037."""
    src = (_BACKEND.parent.parent / path).read_text()
    assert needle in src, f"{path} no longer calls it — memory stops propagating"
