"""POST /api/tasks/{id}/post-merge-regression (#2054)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

_WS = Path(__file__).resolve().parents[1]
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

import server.services.project_settings_store  # noqa: E402,F401  (sets sys.path)
from server.routes import tasks_regression as tr  # noqa: E402
from server.routes.project_authz import TaskAccessChecker  # noqa: E402

SHA = "a" * 40


@pytest.fixture
def env(tmp_path, monkeypatch):
    spec = tmp_path / ".aifactory" / "specs" / "010-x"
    spec.mkdir(parents=True)
    (spec / "task_metadata.json").write_text(
        json.dumps({"transcriptUris": ["s3://b/k.jsonl", "evil`\n[x](y)", 3]})
    )
    monkeypatch.setattr(tr, "load_projects", lambda: {"p1": {"path": str(tmp_path)}})
    monkeypatch.setattr(tr, "org_id_for_path", lambda _p: "org-1")
    calls: dict = {"pinned": [], "open": []}

    async def _warm(org):
        calls["warm"] = org

    import contextlib

    @contextlib.contextmanager
    def _tok(org, *, pinned=False):
        calls["pinned"].append((org, pinned))
        yield

    monkeypatch.setattr(tr.github_app, "warm_org_token", _warm)
    monkeypatch.setattr(tr.github_app, "org_github_token", _tok)

    def _open(**kw):
        calls["open"].append(kw)
        return calls.get("result", {"ok": True, "reason": "created", "url": "u/9"})

    monkeypatch.setattr(tr.revert_pr, "open_revert_pr", _open)
    return calls


def _call(task_id="p1:010-x", sha=SHA, url="https://ci/run/1"):
    body = tr.PostMergeRegression(merge_sha=sha, run_url=url)
    return asyncio.run(tr.post_merge_regression(task_id, body))


def test_happy_path_filters_uris_and_pins_the_org(env):
    assert _call()["url"] == "u/9"
    assert env["pinned"] == [("org-1", True)]
    assert env["open"][0]["transcript_uris"] == ["s3://b/k.jsonl"]
    assert env["open"][0]["project_id"] == "p1"


def test_refusal_is_409(env):
    env["result"] = {"ok": False, "reason": "not_pipeline_merged"}
    with pytest.raises(HTTPException) as e:
        _call()
    assert e.value.status_code == 409


@pytest.mark.parametrize("task_id,code", [("p1:../x", 400), ("nope:010-x", 404)])
def test_bad_task_ids(env, task_id, code):
    with pytest.raises(HTTPException) as e:
        _call(task_id)
    assert e.value.status_code == code
    assert not env["open"]


@pytest.mark.parametrize(
    "kw", [{"url": "http://x"}, {"url": "https://a b"}, {"sha": "xyz"}, {"sha": "ab"}]
)
def test_bad_body_rejected(kw):
    with pytest.raises(ValueError):  # pydantic ValidationError
        tr.PostMergeRegression(
            merge_sha=kw.get("sha", SHA), run_url=kw.get("url", "https://ok/1")
        )


def test_route_requires_member_role():
    route = next(r for r in tr.router.routes if "post-merge" in r.path)
    checkers = [
        d.call
        for d in route.dependant.dependencies
        if isinstance(d.call, TaskAccessChecker)
    ]
    assert [c.minimum_role for c in checkers] == ["member"]
