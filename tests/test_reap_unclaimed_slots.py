"""#1606: a ``running`` row that no worker ever claimed must be reconciled.

Dispatch creates the k8s Job and only then writes ``worker_ref``
(``build_backend`` ``create_namespaced_job`` -> ``set_worker_ref``). A crash
between the two leaves a ``job_states`` row ``running`` with no reference, which
used to be skipped by every reaper while still counting against the global
concurrency cap — a permanent, silent loss of capacity.

The reaper now rebuilds the deterministic Job name and asks the API. These tests
pin the four outcomes, the collision guard that keeps it from reading another
task's verdict, and the fact that the slot actually comes back.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Backend core (vendored job_dispatch) + web-server server packages on path.
_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "apps" / "web-server", _REPO / "apps" / "backend"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))

from core.job_dispatch import job_name  # noqa: E402
from server.database.models import Base  # noqa: E402
from server.services import build_backend as bb  # noqa: E402
from server.services.job_state_store import (  # noqa: E402
    JobStateStore,
    SpawnArgs,
)

_TASK = "p:s1"
_JOB_NAME = job_name("aifactory", _TASK)

_ENGINES: list[Any] = []


class _ApiError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"api error {status}")
        self.status = status


class _FakeBatch:
    """BatchV1Api stand-in with Job ownership annotations."""

    def __init__(
        self,
        existing: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        # job_name -> {"succeeded": int, "failed": int, "job_id_owner": str|None}
        self._existing: dict[str, dict[str, Any]] = dict(existing or {})
        self.api_client = None
        self.reads: list[str] = []

    async def read_namespaced_job(self, name: str, namespace: str) -> Any:
        self.reads.append(name)
        if name not in self._existing:
            raise _ApiError(404)
        spec = self._existing[name]
        if "read_error" in spec:
            raise _ApiError(spec["read_error"])
        owner = spec.get("job_id_owner", _TASK)
        annotations = {} if owner is None else {"factory.io/job-id-full": owner}
        return SimpleNamespace(
            metadata=SimpleNamespace(annotations=annotations),
            status=SimpleNamespace(
                succeeded=spec.get("succeeded") or None,
                failed=spec.get("failed") or None,
            ),
        )


@pytest.fixture(autouse=True)
async def _dispose_engines() -> Any:
    yield
    while _ENGINES:
        await _ENGINES.pop().dispose()


async def _store_with_refless_row(db_path: Path, task: str = _TASK) -> JobStateStore:
    """A store holding one admitted ``running`` row that no worker has claimed —
    exactly the state a crash between Job creation and the ref write leaves."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    _ENGINES.append(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    store = JobStateStore(
        session_factory=async_sessionmaker(engine, expire_on_commit=False)
    )
    await store.admit(task, SpawnArgs(project_path="/data/p", spec_id="s1"), cap=2)
    # Deliberately NO set_worker_ref: that is the bug.
    return store


async def test_admission_stamps_pending_not_a_backend(tmp_path: Path) -> None:
    """THE PREMISE, pinned (plan step 1).

    ``admit`` cannot know which backend will run the slot, so it must not claim
    one. An earlier revision of this fix assumed the row was left ref-less and was
    dead code: ``admit`` in fact stamped ``{"kind": "subprocess"}``, which is
    indistinguishable from a live subprocess build. This assertion is what makes
    that class of mistake impossible to repeat silently.
    """
    store = await _store_with_refless_row(tmp_path / "a0.db")

    state = await store.get_state(_TASK)
    assert state is not None
    assert state["lifecycle_state"] == "running"
    assert (state["worker_ref"] or {}).get("kind") == "pending", (
        "a granted slot must not assert a backend before one has claimed it"
    )

    # The subprocess path declaring ITSELF is what makes the two separable.
    await store.mark_running(_TASK)
    state = await store.get_state(_TASK)
    assert state is not None
    assert (state["worker_ref"] or {}).get("kind") == "subprocess"


async def test_pending_row_is_returned_by_the_query(tmp_path: Path) -> None:
    """A granted-but-unclaimed row is this reaper's business (plan step 4)."""
    store = await _store_with_refless_row(tmp_path / "a.db")

    rows = await store.get_active_kubejobs()

    assert [r["job_id"] for r in rows] == [_TASK]
    assert rows[0]["job_name"] is None
    assert rows[0]["namespace"] is None


async def test_subprocess_row_is_still_skipped(tmp_path: Path) -> None:
    """A row the subprocess path claimed is NOT ours to reap — reaping it would
    kill a live build, which is the failure this whole change must avoid."""
    store = await _store_with_refless_row(tmp_path / "b.db")
    await store.mark_running(_TASK)

    assert await store.get_active_kubejobs() == []


async def test_kubejob_row_is_still_returned(tmp_path: Path) -> None:
    """The pre-existing behaviour the reaper was built on must be untouched."""
    store = await _store_with_refless_row(tmp_path / "b2.db")
    await store.set_worker_ref(
        _TASK, {"kind": "k8s-job", "job_name": _JOB_NAME, "namespace": "factory"}
    )

    rows = await store.get_active_kubejobs()

    assert [r["job_id"] for r in rows] == [_TASK]
    assert rows[0]["job_name"] == _JOB_NAME


async def test_unclaimed_row_with_a_live_job_is_left_running(tmp_path: Path) -> None:
    """THE regression (step 2). On origin/dev the reaper skips this row without
    ever asking the API; the assertion on ``batch.reads`` is what fails there."""
    store = await _store_with_refless_row(tmp_path / "c.db")
    backend = bb.KubeJobBuildBackend(store)
    batch = _FakeBatch({_JOB_NAME: {}})  # present, no terminal status → running

    reaped = await backend.reap_vanished_jobs(batch=batch)

    assert batch.reads == [_JOB_NAME]
    assert reaped == []
    state = await store.get_state(_TASK)
    assert state is not None
    assert state["lifecycle_state"] == "running", "a live build must not be orphaned"


async def test_unclaimed_row_with_no_job_is_failed_and_frees_the_slot(
    tmp_path: Path,
) -> None:
    """The leak itself: no Job under the reconstructed name → reap, and the
    concurrency slot comes back."""
    store = await _store_with_refless_row(tmp_path / "d.db")
    backend = bb.KubeJobBuildBackend(store)

    # cap=1 and one running row → the next task can only queue.
    assert (
        await store.admit(
            "p:other", SpawnArgs(project_path="/data/p", spec_id="other"), cap=1
        )
        == "queued"
    )
    # ...and with the slot still held, nothing can be promoted into it.
    assert await store.drain(cap=1) == []

    # Past the dispatch grace (#2141): this is a crashed dispatch, not a live one.
    reaped = await backend.reap_vanished_jobs(
        batch=_FakeBatch({}), now=time.time() + bb._DISPATCH_GRACE_SECONDS + 5
    )

    assert reaped == [_TASK]
    state = await store.get_state(_TASK)
    assert state is not None
    assert state["lifecycle_state"] == "failed"
    assert "no worker ever claimed this slot" in (state["error"] or "")

    # The slot is genuinely back: the queued task is now promoted into it. This is
    # the outcome the intent promises — capacity returns without a human.
    promoted = await store.drain(cap=1)
    assert [job_id for job_id, _args in promoted] == ["p:other"]


async def test_unclaimed_row_whose_job_succeeded_is_marked_done(tmp_path: Path) -> None:
    store = await _store_with_refless_row(tmp_path / "e.db")
    backend = bb.KubeJobBuildBackend(store)

    await backend.reap_vanished_jobs(batch=_FakeBatch({_JOB_NAME: {"succeeded": 1}}))

    state = await store.get_state(_TASK)
    assert state is not None
    assert state["lifecycle_state"] == "done"


async def test_unclaimed_row_whose_job_failed_is_marked_failed(tmp_path: Path) -> None:
    store = await _store_with_refless_row(tmp_path / "f.db")
    backend = bb.KubeJobBuildBackend(store)

    reaped = await backend.reap_vanished_jobs(
        batch=_FakeBatch({_JOB_NAME: {"failed": 1}})
    )

    assert reaped == [_TASK]
    state = await store.get_state(_TASK)
    assert state is not None
    assert state["lifecycle_state"] == "failed"


@pytest.mark.parametrize("stored", [False, True])
async def test_colliding_job_name_does_not_decide_this_rows_verdict(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    stored: bool,
) -> None:
    """A Job at the expected name can still belong to another task."""
    store = await _store_with_refless_row(tmp_path / "g.db")
    if stored:
        await store.set_worker_ref(
            _TASK, {"kind": "k8s-job", "job_name": _JOB_NAME, "namespace": "factory"}
        )
    backend = bb.KubeJobBuildBackend(store)
    # Same name, but the Job belongs to a different task and has FAILED.
    batch = _FakeBatch({_JOB_NAME: {"failed": 1, "job_id_owner": "some-other-task"}})

    with caplog.at_level("WARNING"):
        reaped = await backend.reap_vanished_jobs(batch=batch)

    assert reaped == []
    state = await store.get_state(_TASK)
    assert state is not None
    assert state["lifecycle_state"] == "running", "another task's verdict was used"
    assert batch.reads == [_JOB_NAME]
    assert "belongs to another task" in caplog.text


@pytest.mark.parametrize("stored", [False, True])
async def test_job_without_the_annotation_is_not_trusted(
    tmp_path: Path, stored: bool
) -> None:
    """Fail closed: an unstamped Job cannot be proven to be ours."""
    store = await _store_with_refless_row(tmp_path / "h.db")
    if stored:
        await store.set_worker_ref(
            _TASK, {"kind": "k8s-job", "job_name": _JOB_NAME, "namespace": "factory"}
        )
    backend = bb.KubeJobBuildBackend(store)
    batch = _FakeBatch({_JOB_NAME: {"failed": 1, "job_id_owner": None}})

    assert await backend.reap_vanished_jobs(batch=batch) == []
    state = await store.get_state(_TASK)
    assert state is not None
    assert state["lifecycle_state"] == "running"


async def test_read_error_leaves_row_running_without_owner_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = await _store_with_refless_row(tmp_path / "read-error.db")
    backend = bb.KubeJobBuildBackend(store)
    batch = _FakeBatch({_JOB_NAME: {"read_error": 500}})

    with caplog.at_level("WARNING"):
        assert await backend.reap_vanished_jobs(batch=batch) == []

    assert batch.reads == [_JOB_NAME]
    assert (await store.get_state(_TASK))["lifecycle_state"] == "running"
    assert "belongs to another task" not in caplog.text


async def test_verified_reference_is_persisted_for_later_ticks(
    tmp_path: Path,
) -> None:
    """Step 4: a verified reconstruction is written back, so the next tick takes
    the ordinary recorded-ref path instead of rebuilding the name again."""
    store = await _store_with_refless_row(tmp_path / "i.db")
    backend = bb.KubeJobBuildBackend(store)

    await backend.reap_vanished_jobs(batch=_FakeBatch({_JOB_NAME: {}}))

    rows = await store.get_active_kubejobs()
    assert rows[0]["job_name"] == _JOB_NAME
    assert rows[0]["namespace"] == "factory"


async def test_row_inside_dispatch_grace_is_not_reaped(tmp_path: Path) -> None:
    """#2141: dispatch is still packing the worktree, so no Job exists yet. The
    reconciler must not fail the build it is about to create."""
    store = await _store_with_refless_row(tmp_path / "g.db")
    backend = bb.KubeJobBuildBackend(store)

    reaped = await backend.reap_vanished_jobs(batch=_FakeBatch({}))

    assert reaped == []
    state = await store.get_state(_TASK)
    assert state is not None
    assert state["lifecycle_state"] == "running"


def test_naive_utc_timestamp_is_read_as_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2141: job_states stamps naive UTC; a non-UTC pod must not skew the age."""
    import datetime as dt

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        naive = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        assert abs(time.time() - bb._to_epoch(naive)) < 5
    finally:
        monkeypatch.undo()
        time.tzset()
