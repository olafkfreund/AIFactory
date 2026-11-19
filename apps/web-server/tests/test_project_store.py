"""The project registry DB store (#2157)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from server import project_registry as reg
from server import project_store as store


def _entry(path: str, name: str | None = None, **extra) -> dict:
    return {"path": path, "name": name or Path(path).name, "org_id": "default", **extra}


def test_save_load_round_trip_keeps_unknown_keys_and_assigns_identity():
    pid = "11111111-1111-1111-1111-111111111111"
    reg.save_projects(
        {
            pid: _entry(
                "/tmp/My Repo",
                "My Repo!",
                tenant_id="t1",
                clonedFrom="git@GitHub.com:Owner/Repo.git",
                settings={"githubToken": "tok", "autoFix": {"queue": [1]}},
                weird={"a": [1, 2]},
            )
        }
    )
    got = reg.load_projects()[pid]
    assert got["slug"] == "my-repo"
    assert got["repo_identity"] == "github.com/owner/repo"
    assert got["team_id"]
    assert got["tenant_id"] == "t1"
    assert got["settings"]["githubToken"] == "tok"
    assert got["weird"] == {"a": [1, 2]}


def test_slug_collisions_get_suffixes_and_rename_keeps_id():
    reg.save_projects({"a": _entry("/tmp/a", "api"), "b": _entry("/tmp/b", "api")})
    loaded = reg.load_projects()
    assert {loaded["a"]["slug"], loaded["b"]["slug"]} == {"api", "api-2"}
    loaded["a"]["slug"] = "renamed"
    reg.save_projects(loaded)
    again = reg.load_projects()
    assert again["a"]["slug"] == "renamed" and "a" in again
    again["b"]["slug"] = "renamed"
    with pytest.raises(store.SlugConflict):
        reg.save_projects(again)
    again["b"]["slug"] = "Bad Slug"
    with pytest.raises(store.SlugConflict) as exc:
        reg.save_projects(again)
    assert exc.value.invalid


def test_save_is_upsert_only_and_archive_hides_but_reserves_slug():
    reg.save_projects({"a": _entry("/tmp/a", "alpha"), "b": _entry("/tmp/b", "beta")})
    reg.save_projects({"a": reg.load_projects()["a"]})  # partial dict: b survives
    assert set(reg.load_projects()) == {"a", "b"}
    assert store.archive_project("b") is True
    assert set(reg.load_projects()) == {"a"}
    assert store.get_project("b") is None
    assert store.get_project("b", include_archived=True)["slug"] == "beta"
    reg.save_projects({"c": _entry("/tmp/c", "beta")})
    assert reg.load_projects()["c"]["slug"] == "beta-2"


def test_stale_snapshot_does_not_overwrite_a_concurrent_update():
    reg.save_projects({"a": _entry("/tmp/a", "alpha"), "b": _entry("/tmp/b", "beta")})
    handler_a = reg.load_projects()  # loads before B's update
    handler_b = reg.load_projects()
    handler_b["b"]["name"] = "beta-new"
    reg.save_projects(handler_b)
    handler_a["a"]["name"] = "alpha-new"  # A only touches a
    reg.save_projects(handler_a)
    final = reg.load_projects()
    assert final["b"]["name"] == "beta-new"
    assert final["a"]["name"] == "alpha-new"


def test_default_team_is_per_org_and_reused():
    reg.save_projects(
        {
            "a": _entry("/tmp/a"),
            "b": _entry("/tmp/b"),
            "c": {**_entry("/tmp/c"), "org_id": "other-org"},
        }
    )
    p = reg.load_projects()
    assert p["a"]["team_id"] == p["b"]["team_id"] != p["c"]["team_id"]


def test_import_preserves_ids_is_idempotent_and_renames_the_file():
    f = store._projects_file()
    legacy = {
        "id-1": _entry("/tmp/one", "One", settings={"x": 1}),
        "id-2": _entry("/tmp/two", "One"),
    }
    f.write_text(json.dumps(legacy))
    loaded = reg.load_projects()  # lazy import on first use
    assert set(loaded) == {"id-1", "id-2"}
    assert {loaded["id-1"]["slug"], loaded["id-2"]["slug"]} == {"one", "one-2"}
    assert not f.exists() and f.with_name("projects.json.imported").exists()
    # a second file with an old and a new id: only the new one is added
    f.write_text(json.dumps({"id-1": _entry("/tmp/CHANGED"), "id-3": _entry("/tmp/three")}))
    reg.load_projects()
    after = reg.load_projects()
    assert set(after) == {"id-1", "id-2", "id-3"}
    assert after["id-1"]["path"] == "/tmp/one"


def test_export_round_trip_and_overwrite_roll_forward(tmp_path):
    reg.save_projects({"a": _entry("/tmp/a", "alpha"), "b": _entry("/tmp/b", "beta")})
    out = tmp_path / "projects.json"
    assert store.export_projects_json(out) == 2
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"
    data = json.loads(out.read_text())
    assert set(data) == {"a", "b"}
    # the old release edits the file: rename a, drop b; roll forward with overwrite
    data["a"]["name"] = "alpha-edited"
    del data["b"]
    out.write_text(json.dumps(data))
    assert store.import_projects_json(out, overwrite=True) == 1
    now = reg.load_projects()
    assert set(now) == {"a"} and now["a"]["name"] == "alpha-edited"


def test_repo_identity_is_recorded_not_merged():
    reg.save_projects(
        {
            "a": _entry("/tmp/a", "same", clonedFrom="https://github.com/o/r.git"),
            "b": _entry("/tmp/b", "same", clonedFrom="https://github.com/o/r"),
            "c": _entry("/tmp/c", "local"),
        }
    )
    p = reg.load_projects()
    assert set(p) == {"a", "b", "c"}
    assert p["a"]["repo_identity"] == p["b"]["repo_identity"] == "github.com/o/r"
    assert p["c"]["repo_identity"] is None


def test_aload_does_not_stall_the_event_loop(monkeypatch):
    real = store._load

    async def slow(*a, **k):
        await asyncio.sleep(0.4)
        return await real(*a, **k)

    monkeypatch.setattr(store, "_load", slow)

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        t = asyncio.create_task(ticker())
        start = time.monotonic()
        await reg.aload_projects()
        t.cancel()
        return ticks, time.monotonic() - start

    ticks, took = asyncio.run(main())
    assert took >= 0.4 and ticks > 10
