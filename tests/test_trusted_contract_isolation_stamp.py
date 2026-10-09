"""The web server stamps how each build was isolated, at spawn (#1667, test 12).

The stamp is derived from the web server's own state at spawn: ``kubejob`` for a
Job dispatch, ``sandbox-pidns`` only when the real argv is bwrap with
``--unshare-pid`` before ``--``, ``none`` otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "apps" / "web-server", _REPO / "apps" / "backend"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))

from server.database import engine as db_engine  # noqa: E402
from server.database.models import Base  # noqa: E402
from server.services import agent_service as agent_mod  # noqa: E402
from server.services import build_backend as bb  # noqa: E402
from server.services import sandbox  # noqa: E402
from server.services.job_state_store import JobStateStore, SpawnArgs  # noqa: E402
from server.services.trusted_contract_store import (  # noqa: E402
    TrustedContractStore,
    spec_key_for_dir,
)
from server.specpath import spec_dir_for  # noqa: E402

from tests.test_build_backend_kubejob import _FakeBatch  # noqa: E402

_DATA_ROOT = "/home/nonroot/.aifactory"


class _Stop(Exception):
    """Raised by the fake spawn so the test stops right after the stamp."""


@pytest.fixture
async def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'i.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # TrustedContractStore() resolves its session factory lazily from here.
    monkeypatch.setattr(db_engine, "async_session_factory", factory)
    sandbox._bwrap_works.cache_clear()
    yield TrustedContractStore(session_factory=factory)
    sandbox._bwrap_works.cache_clear()
    await engine.dispose()


async def _spawn(project: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def _fake_exec(*argv: str, **_kw: object):
        seen.extend(argv)
        raise _Stop

    monkeypatch.setattr(agent_mod.asyncio, "create_subprocess_exec", _fake_exec)
    service = agent_mod.AgentService()
    with pytest.raises(_Stop):
        await service._spawn_task_execution("p:001-x", project, "001-x")
    return seen


async def test_12_spawn_without_bwrap_stamps_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: TrustedContractStore
) -> None:
    project = tmp_path / "proj"
    key = spec_key_for_dir(spec_dir_for(project, "001-x"))
    await store.put(key, "001-x", {"feature": "f"})
    monkeypatch.setattr(sandbox.shutil, "which", lambda _n: None)

    await _spawn(project, monkeypatch)

    rec = await store.get(key)
    assert rec is not None and rec.build_isolation == "none"


async def test_12_spawn_under_bwrap_unshare_pid_stamps_pidns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: TrustedContractStore
) -> None:
    project = tmp_path / "proj"
    key = spec_key_for_dir(spec_dir_for(project, "001-x"))
    await store.put(key, "001-x", {"feature": "f"})
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(
        sandbox,
        "build_sandboxed_command",
        lambda cmd, _root, **_k: [
            "/usr/bin/bwrap",
            "--die-with-parent",
            "--unshare-pid",
            "--",
            *cmd,
        ],
    )

    await _spawn(project, monkeypatch)

    rec = await store.get(key)
    assert rec is not None and rec.build_isolation == "sandbox-pidns"


async def test_12_unshare_pid_after_double_dash_does_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: TrustedContractStore
) -> None:
    project = tmp_path / "proj"
    key = spec_key_for_dir(spec_dir_for(project, "001-x"))
    await store.put(key, "001-x", {"feature": "f"})
    monkeypatch.setattr(sandbox, "_bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(
        sandbox,
        "build_sandboxed_command",
        lambda cmd, _root, **_k: ["/usr/bin/bwrap", "--", *cmd, "--unshare-pid"],
    )

    await _spawn(project, monkeypatch)

    rec = await store.get(key)
    assert rec is not None and rec.build_isolation == "none"


async def test_12_kubejob_dispatch_stamps_before_job_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: TrustedContractStore
) -> None:
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    monkeypatch.setattr(bb, "populate_build_worktree", lambda *_a, **_k: None)
    project = Path(_DATA_ROOT) / "workspaces" / "p"
    key = spec_key_for_dir(spec_dir_for(project, "s1"))
    await store.put(key, "s1", {"feature": "f"})

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'j.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        jobs = JobStateStore(
            session_factory=async_sessionmaker(engine, expire_on_commit=False)
        )
        await jobs.admit("p:s1", SpawnArgs(project_path="/data/p", spec_id="s1"), cap=2)

        stamped_at_create: list[str | None] = []

        class _Batch(_FakeBatch):
            async def create_namespaced_job(self, namespace: str, manifest: dict):
                rec = await store.get(key)
                stamped_at_create.append(rec.build_isolation if rec else None)
                await super().create_namespaced_job(namespace, manifest)

        await bb.KubeJobBuildBackend(jobs).dispatch(
            task_id="p:s1", project_path=project, spec_id="s1", batch=_Batch()
        )
    finally:
        await engine.dispose()

    assert stamped_at_create == ["kubejob"]
