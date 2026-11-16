"""Revert PR after a post-merge regression (#2054). Stub runner, no network."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

_WS = Path(__file__).resolve().parents[1]
if str(_WS) not in sys.path:
    sys.path.insert(0, str(_WS))

import server.services.project_settings_store  # noqa: E402,F401  (sets sys.path for core)
from server.services import revert_pr as rp  # noqa: E402
from server.services.pr_endgame import CmdResult  # noqa: E402

SHA = "a" * 40
VIEW = {
    "id": "PR_node",
    "state": "MERGED",
    "title": "Add x",
    "headRefName": "aifactory/010-x",
    "mergeCommit": {"oid": SHA},
}
CREATED = {
    "data": {"revertPullRequest": {"revertPullRequest": {"number": 9, "url": "u/9"}}}
}


class Runner:
    def __init__(self, view=VIEW, listing=None):
        self.view, self.listing, self.calls = view, listing or [], []

    def __call__(self, argv, cwd=None):
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return CmdResult(0, json.dumps(self.view), "")
        if argv[:3] == ["gh", "pr", "list"]:
            return CmdResult(0, json.dumps(self.listing), "")
        if argv[:3] == ["gh", "api", "graphql"]:
            return CmdResult(0, json.dumps(CREATED), "")
        return CmdResult(1, "", "unexpected")

    def creates(self):
        return [c for c in self.calls if c[:3] == ["gh", "api", "graphql"]]


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("server.paths.get_data_dir", lambda: tmp_path)


def _record(runner=None):
    return rp.record_pipeline_merge(
        "p1", "010-x", "o", "r", 7, "aifactory/010-x", runner or Runner()
    )


def _open(runner, sha=SHA, project="p1", spec="010-x"):
    return rp.open_revert_pr(
        project_id=project,
        spec_id=spec,
        failing_sha=sha,
        run_url="https://ci/run/1",
        transcript_uris=["s3://b/k.jsonl"],
        runner=runner,
    )


def test_record_then_revert_links_run_and_transcript():
    assert _record()
    r = Runner()
    out = _open(r)
    assert out == {"ok": True, "reason": "created", "url": "u/9"}
    body = next(a for a in r.creates()[0] if a.startswith("body="))
    assert "https://ci/run/1" in body and "s3://b/k.jsonl" in body
    assert "Reverts o/r#7" in body
    assert not any("merge" in c[:3] for c in r.calls)  # never merged


def test_prefix_sha_accepted_and_repeat_is_idempotent():
    _record()
    r = Runner()
    assert _open(r, sha=SHA[:8])["reason"] == "created"
    assert _open(r)["reason"] == "exists"
    assert len(r.creates()) == 1


@pytest.mark.parametrize(
    "kw",
    [
        {"sha": "b" * 40},
        {"sha": "zz"},
        {"project": "other"},
        {"spec": "other"},
    ],
)
def test_unrecorded_commit_refused(kw):
    _record()
    r = Runner()
    assert _open(r, **kw)["ok"] is False
    assert not r.creates()


def test_no_record_refused():
    r = Runner()
    assert _open(r)["reason"] == "not_pipeline_merged"
    assert not r.calls


@pytest.mark.parametrize(
    "patch",
    [{"state": "OPEN"}, {"headRefName": "evil"}, {"mergeCommit": {"oid": "c" * 40}}],
)
def test_github_must_confirm_the_merge(patch):
    _record()
    r = Runner(view={**VIEW, **patch})
    assert _open(r)["reason"] == "merge_not_confirmed"
    assert not r.creates()


@pytest.mark.parametrize("state", ["open", "closed", "merged"])
def test_earlier_revert_of_any_state_reconciled(state):
    _record()
    r = Runner(listing=[{"url": f"u/{state}", "body": "Reverts o/r#7\n..."}])
    out = _open(r)
    assert out["url"] == f"u/{state}" and not r.creates()


def test_concurrent_calls_create_once():
    _record()
    r = Runner()
    threads = [threading.Thread(target=_open, args=(r,)) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(r.creates()) == 1


def test_record_without_merge_commit_is_skipped():
    assert _record(Runner(view={**VIEW, "mergeCommit": None})) is False
