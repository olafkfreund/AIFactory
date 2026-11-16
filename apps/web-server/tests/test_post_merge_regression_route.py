"""POST /api/tasks/{id}/post-merge-regression (#2054)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    spec = tmp_path / ".aifactory" / "specs" / "010-x"
    spec.mkdir(parents=True)
    (spec / "task_metadata.json").write_text(
        json.dumps({"transcriptUris": ["s3://b/k.jsonl", "evil`\n[x](y)", 3]})
    )
    monkeypatch.setattr(tr, "load_projects", lambda: {"p1": {"path": str(tmp_path)}})
    monkeypatch.setattr(tr, "org_id_for_path", lambda _p: "org-1")
    calls: dict[str, Any] = {"pinned": [], "open": []}

    async def _warm(org: str) -> None:
        calls["warm"] = org

    @contextlib.contextmanager
    def _tok(org: str, *, pinned: bool = False) -> Iterator[None]:
        calls["pinned"].append((org, pinned))
        yield

    monkeypatch.setattr(tr.github_app, "warm_org_token", _warm)
    monkeypatch.setattr(tr.github_app, "org_github_token", _tok)

    def _open(**kw: Any) -> dict[str, Any]:
        calls["open"].append(kw)
        out: dict[str, Any] = calls.get(
            "result", {"ok": True, "reason": "created", "url": "u/9"}
        )
        return out

    monkeypatch.setattr(tr.revert_pr, "open_revert_pr", _open)
    return calls


def _call(
    task_id: str = "p1:010-x", sha: str = SHA, url: str = "https://ci/run/1"
) -> dict[str, Any]:
    body = tr.PostMergeRegression(merge_sha=sha, run_url=url)
    return asyncio.run(tr.post_merge_regression(task_id, body, {}))


def test_happy_path_filters_uris_and_pins_the_org(env: dict[str, Any]) -> None:
    assert _call()["url"] == "u/9"
    assert env["pinned"] == [("org-1", True)]
    assert env["open"][0]["transcript_uris"] == ["s3://b/k.jsonl"]
    assert env["open"][0]["project_id"] == "p1"


def test_refusal_is_409(env: dict[str, Any]) -> None:
    env["result"] = {"ok": False, "reason": "not_pipeline_merged"}
    with pytest.raises(HTTPException) as e:
        _call()
    assert e.value.status_code == 409


@pytest.mark.parametrize("task_id,code", [("p1:../x", 400), ("nope:010-x", 404)])
def test_bad_task_ids(env: dict[str, Any], task_id: str, code: int) -> None:
    with pytest.raises(HTTPException) as e:
        _call(task_id)
    assert e.value.status_code == code
    assert not env["open"]


@pytest.mark.parametrize(
    "kw", [{"url": "http://x"}, {"url": "https://a b"}, {"sha": "xyz"}, {"sha": "ab"}]
)
def test_bad_body_rejected(kw: dict[str, str]) -> None:
    with pytest.raises(ValueError):  # pydantic ValidationError
        tr.PostMergeRegression(
            merge_sha=kw.get("sha", SHA), run_url=kw.get("url", "https://ok/1")
        )


def test_route_requires_member_role() -> None:
    route = next(r for r in tr.router.routes if "post-merge" in r.path)
    checkers = [
        d.call
        for d in route.dependant.dependencies
        if isinstance(d.call, TaskAccessChecker)
    ]
    assert [c.minimum_role for c in checkers] == ["member"]
