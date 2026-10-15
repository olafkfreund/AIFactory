"""The web server stamps how each build was isolated, at spawn (#1667, test 12).

The stamp is derived from the web server's own state at spawn: ``kubejob`` for a
Job dispatch, ``sandbox-pidns`` only when the real argv is bwrap with
``--unshare-pid`` before ``--``, ``none`` otherwise.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_REPO = Path(__file__).resolve().parents[1]
for _p in (_REPO / "apps" / "web-server", _REPO / "apps" / "backend"):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))

from server.database.models import Base  # noqa: E402
from server.services import agent_service as agent_mod  # noqa: E402
from server.services import build_backend as bb  # noqa: E402
from server.services import sandbox  # noqa: E402
from server.services import trusted_contract as tcm  # noqa: E402
from server.services import trusted_contract_store as tcs  # noqa: E402
from server.services.job_state_store import JobStateStore, SpawnArgs  # noqa: E402
from server.services.trusted_contract_store import (  # noqa: E402
    TrustedContractStore,
    spec_key_for_dir,
)
from server.specpath import spec_dir_for  # noqa: E402

from tests.test_build_backend_kubejob import _FakeBatch  # noqa: E402

_DATA_ROOT = "/home/nonroot/.aifactory"
_ENV = "AIFACTORY_TRUSTED_CONTRACT"


@pytest.fixture(autouse=True)
def _no_trusted_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV, raising=False)


def _manifest_env(node: object) -> dict[str, str]:
    """Every ``{name, value}`` env entry anywhere in a Job manifest."""
    found: dict[str, str] = {}
    if isinstance(node, dict):
        if "name" in node and "value" in node:
            found[node["name"]] = node["value"]
        for v in node.values():
            found.update(_manifest_env(v))
    elif isinstance(node, list):
        for v in node:
            found.update(_manifest_env(v))
    return found


class _Stop(Exception):
    """Raised by the fake spawn so the test stops right after the stamp."""


@pytest.fixture
async def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'i.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # TrustedContractStore() resolves its session factory lazily from here.
    # Resolve at run time: other suites swap sys.modules entries after collection.
    db_engine = importlib.import_module("server.database.engine")
    monkeypatch.setattr(db_engine, "async_session_factory", factory)
    sandbox._bwrap_works.cache_clear()
    yield TrustedContractStore(session_factory=factory)
    sandbox._bwrap_works.cache_clear()
    await engine.dispose()


async def _spawn(
    project: Path, monkeypatch: pytest.MonkeyPatch, envs: list | None = None
) -> list[str]:
    seen: list[str] = []

    async def _fake_exec(*argv: str, **_kw: object):
        seen.extend(argv)
        if envs is not None:
            envs.append(_kw.get("env"))
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


async def _kubejob_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: TrustedContractStore,
    *,
    record: dict | None,
) -> tuple[list[str], list[str | None], dict[str, str]]:
    monkeypatch.setenv("AIFACTORY_DATA_ROOT", _DATA_ROOT)
    monkeypatch.setattr(bb, "populate_build_worktree", lambda *_a, **_k: None)
    project = Path(_DATA_ROOT) / "workspaces" / "p"
    key = spec_key_for_dir(spec_dir_for(project, "s1"))
    if record is not None:
        await store.put(key, "s1", record)

    order: list[str] = []
    real_stamp, real_env, real_spawn = (
        tcs.stamp_spawn,
        bb.build_job_env,
        tcm.spawn_env,
    )

    async def stamp(*a: object, **k: object) -> None:
        order.append("stamp")
        await real_stamp(*a, **k)

    def job_env(*a: object, **k: object):
        order.append("env")
        return real_env(*a, **k)

    async def spawn(*a: object, **k: object):
        order.append("spawn_env")
        return await real_spawn(*a, **k)

    monkeypatch.setattr(tcs, "stamp_spawn", stamp)
    monkeypatch.setattr(bb, "build_job_env", job_env)
    monkeypatch.setattr(tcm, "spawn_env", spawn)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'j.db'}")
    stamped_at_create: list[str | None] = []
    batch_holder: list[_FakeBatch] = []
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        jobs = JobStateStore(
            session_factory=async_sessionmaker(engine, expire_on_commit=False)
        )
        await jobs.admit("p:s1", SpawnArgs(project_path="/data/p", spec_id="s1"), cap=2)

        class _Batch(_FakeBatch):
            async def create_namespaced_job(self, namespace: str, manifest: dict):
                order.append("create")
                rec = await store.get(key)
                stamped_at_create.append(rec.build_isolation if rec else None)
                await super().create_namespaced_job(namespace, manifest)

        batch = _Batch()
        batch_holder.append(batch)
        await bb.KubeJobBuildBackend(jobs).dispatch(
            task_id="p:s1", project_path=project, spec_id="s1", batch=batch
        )
    finally:
        await engine.dispose()
    return order, stamped_at_create, _manifest_env(batch_holder[0].created[0][1])


async def test_12_kubejob_dispatch_stamps_before_job_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: TrustedContractStore
) -> None:
    order, stamped_at_create, env = await _kubejob_dispatch(
        tmp_path, monkeypatch, store, record={"feature": "f"}
    )
    assert stamped_at_create == ["kubejob"]
    assert order == ["stamp", "env", "spawn_env", "create"]
    assert env[_ENV] == "hold"


async def test_kubejob_legacy_and_stray_server_env_carry_no_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: TrustedContractStore
) -> None:
    monkeypatch.setenv(_ENV, "forged")
    _order, _stamped, env = await _kubejob_dispatch(
        tmp_path, monkeypatch, store, record=None
    )
    assert _ENV not in env


async def test_inpod_spawn_merges_value_after_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: TrustedContractStore
) -> None:
    project = tmp_path / "proj"
    key = spec_key_for_dir(spec_dir_for(project, "001-x"))
    await store.put(key, "001-x", {"feature": "f"})
    monkeypatch.setenv(_ENV, "forged")
    order: list[str] = []
    real_stamp, real_spawn = tcs.stamp_spawn, tcm.spawn_env

    async def stamp(*a: object, **k: object) -> None:
        order.append("stamp")
        await real_stamp(*a, **k)

    async def spawn(*a: object, **k: object):
        order.append("spawn_env")
        return await real_spawn(*a, **k)

    monkeypatch.setattr(tcs, "stamp_spawn", stamp)
    monkeypatch.setattr(tcm, "spawn_env", spawn)
    envs: list = []
    await _spawn(project, monkeypatch, envs)
    assert order == ["stamp", "spawn_env"]
    assert envs[0][_ENV] == "hold"


async def test_inpod_legacy_spawn_carries_no_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: TrustedContractStore
) -> None:
    monkeypatch.setenv(_ENV, "forged")
    envs: list = []
    await _spawn(tmp_path / "proj", monkeypatch, envs)
    assert _ENV not in envs[0]
