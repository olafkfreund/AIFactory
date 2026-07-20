"""The verify handoff must never send a base branch as source_branch (#980).

A build always lands on ``aifactory/<spec_id>``; the control-plane worktree
stays on the base after a kubejob build. Sending that base to TFactory makes it
check out code WITHOUT the build, generate tests against the unbuilt feature,
and reject all of them as permanently red -- a hollow verify that reads like a
failed verification (TFactory #729).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pfactory.tfactory_client as tc
import pytest


def _spec_dir(tmp_path: Path, meta: dict[str, Any] | None = None) -> Path:
    d = tmp_path / "spec"
    d.mkdir()
    (d / "spec.md").write_text("# Feat\n\n## Acceptance Criteria\n- does the thing\n")
    (d / "requirements.json").write_text(json.dumps({"title": "Feat"}))
    if meta is not None:
        (d / "task_metadata.json").write_text(json.dumps(meta))
    return d


def test_dev_base_is_not_sent_as_source_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live failure: base=dev, worktree HEAD=dev, so dev was handed over."""
    spec_dir = _spec_dir(tmp_path, {"base_branch": "dev"})
    monkeypatch.setattr(tc, "_git_info_and_push", lambda *_: ("git@x:o/r.git", "dev"))
    monkeypatch.setattr(tc, "_aifactory_project_name", lambda *_: "tfactory")
    monkeypatch.setattr(tc, "_project_git_url", lambda *_: None)
    monkeypatch.setattr(tc, "load_task_contract", lambda *_: {})

    payload = tc.build_ingest_payload(spec_dir, "002-mcp-health")

    assert payload["source_branch"] == "aifactory/002-mcp-health"
    assert payload["source_branch"] != "dev", "a base branch is never the build output"


def test_arbitrary_base_is_not_sent_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a dev special-case: any configured base behaves the same."""
    spec_dir = _spec_dir(tmp_path, {"base_branch": "integration"})
    monkeypatch.setattr(
        tc, "_git_info_and_push", lambda *_: ("git@x:o/r.git", "integration")
    )
    monkeypatch.setattr(tc, "_aifactory_project_name", lambda *_: "demo")
    monkeypatch.setattr(tc, "_project_git_url", lambda *_: None)
    monkeypatch.setattr(tc, "load_task_contract", lambda *_: {})

    payload = tc.build_ingest_payload(spec_dir, "spec-9")
    assert payload["source_branch"] == "aifactory/spec-9"


def test_a_real_build_branch_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not clobber a genuine head that differs from the base."""
    spec_dir = _spec_dir(tmp_path, {"base_branch": "dev"})
    monkeypatch.setattr(
        tc, "_git_info_and_push", lambda *_: ("git@x:o/r.git", "aifactory/spec-7")
    )
    monkeypatch.setattr(tc, "_aifactory_project_name", lambda *_: "demo")
    monkeypatch.setattr(tc, "_project_git_url", lambda *_: None)
    monkeypatch.setattr(tc, "load_task_contract", lambda *_: {})

    payload = tc.build_ingest_payload(spec_dir, "spec-7")
    assert payload["source_branch"] == "aifactory/spec-7"


def test_missing_task_metadata_still_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No task_metadata.json: the conventional bases must still be caught."""
    spec_dir = _spec_dir(tmp_path, None)
    monkeypatch.setattr(tc, "_git_info_and_push", lambda *_: ("git@x:o/r.git", "main"))
    monkeypatch.setattr(tc, "_aifactory_project_name", lambda *_: "demo")
    monkeypatch.setattr(tc, "_project_git_url", lambda *_: None)
    monkeypatch.setattr(tc, "load_task_contract", lambda *_: {})

    payload = tc.build_ingest_payload(spec_dir, "spec-6")
    assert payload["source_branch"] == "aifactory/spec-6"
