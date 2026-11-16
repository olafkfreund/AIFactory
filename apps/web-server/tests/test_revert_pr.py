"""Revert PR after a post-merge regression (#2054). Stub runner, no network."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

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


def _raise_full(*_args: object) -> None:
    raise OSError("full")


class Runner:
    def __init__(self, view: dict[str, Any] = VIEW, listing: Any = ()) -> None:
        self.view, self.listing = view, listing
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], _cwd: str | None = None) -> CmdResult:
        self.calls.append(argv)
        if argv[:3] == ["gh", "pr", "view"]:
            return CmdResult(0, json.dumps(self.view), "")
        if argv[:3] == ["gh", "pr", "list"]:
            if self.listing is None:
                return CmdResult(1, "", "boom")
            return CmdResult(0, json.dumps(self.listing), "")
        if argv[:3] == ["gh", "api", "graphql"]:
            return CmdResult(0, json.dumps(CREATED), "")
        return CmdResult(1, "", "unexpected")

    def creates(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["gh", "api", "graphql"]]


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("server.paths.get_data_dir", lambda: tmp_path)


def _record(runner: Runner | None = None) -> bool:
    ok: bool = rp.record_pipeline_merge(
        "p1", "010-x", "o", "r", 7, "aifactory/010-x", runner or Runner()
    )
    return ok


def _open(
    runner: Runner, sha: str = SHA, project: str = "p1", spec: str = "010-x"
) -> dict[str, Any]:
    out: dict[str, Any] = rp.open_revert_pr(
        project_id=project,
        spec_id=spec,
        failing_sha=sha,
        run_url="https://ci/run/1",
        transcript_uris=["s3://b/k.jsonl"],
        runner=runner,
    )
    return out


def test_record_then_revert_links_run_and_transcript() -> None:
    assert _record()
    r = Runner()
    out = _open(r)
    assert out == {"ok": True, "reason": "created", "url": "u/9"}
    body = next(a for a in r.creates()[0] if a.startswith("body="))
    assert "https://ci/run/1" in body and "s3://b/k.jsonl" in body
    assert "Reverts o/r#7" in body
    assert not any("merge" in c[:3] for c in r.calls)  # never merged


def test_prefix_sha_accepted_and_repeat_is_idempotent() -> None:
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
def test_unrecorded_commit_refused(kw: Any) -> None:
    _record()
    r = Runner()
    assert _open(r, **kw)["ok"] is False
    assert not r.creates()


def test_no_record_refused() -> None:
    r = Runner()
    assert _open(r)["reason"] == "not_pipeline_merged"
    assert not r.calls


@pytest.mark.parametrize(
    "patch",
    [{"state": "OPEN"}, {"headRefName": "evil"}, {"mergeCommit": {"oid": "c" * 40}}],
)
def test_github_must_confirm_the_merge(patch: Any) -> None:
    _record()
    r = Runner(view={**VIEW, **patch})
    assert _open(r)["reason"] == "merge_not_confirmed"
    assert not r.creates()


@pytest.mark.parametrize("state", ["open", "closed", "merged"])
def test_earlier_revert_of_any_state_reconciled(state: Any) -> None:
    _record()
    r = Runner(listing=[{"url": f"u/{state}", "body": "Reverts o/r#7\n..."}])
    out = _open(r)
    assert out["url"] == f"u/{state}" and not r.creates()


def test_failed_lookup_does_not_create() -> None:
    _record()
    r = Runner(listing=None)
    assert _open(r)["reason"] == "reconcile_failed"
    assert not r.creates()


def test_similar_pr_number_is_not_a_match() -> None:
    _record()
    r = Runner(listing=[{"url": "u/70", "body": "Reverts o/r#70\n"}])
    assert _open(r)["reason"] == "created"


def test_ambiguous_prefix_refused() -> None:
    _record()
    other = "a" * 7 + "b" * 33
    rp._record_path(other).write_text(
        json.dumps({"project_id": "p1", "spec_id": "010-x", "merge_sha": other})
    )
    r = Runner()
    assert _open(r, sha="a" * 7)["reason"] == "not_pipeline_merged"
    assert not r.calls


def test_url_persist_failure_keeps_the_record_and_retry_reconciles(
    monkeypatch: Any,
) -> None:
    _record()
    real = rp.atomic_write_secret_json
    monkeypatch.setattr(
        rp,
        "atomic_write_secret_json",
        _raise_full,
    )
    r = Runner()
    assert _open(r)["reason"] == "created"
    monkeypatch.setattr(rp, "atomic_write_secret_json", real)
    r.listing = [{"url": "u/9", "body": "Reverts o/r#7\nx"}]
    assert _open(r) == {"ok": True, "reason": "exists", "url": "u/9"}
    assert len(r.creates()) == 1


def test_concurrent_calls_create_once() -> None:
    _record()
    r = Runner()
    threads = [threading.Thread(target=_open, args=(r,)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(r.creates()) == 1


def test_record_without_merge_commit_is_skipped() -> None:
    assert _record(Runner(view={**VIEW, "mergeCommit": None})) is False
