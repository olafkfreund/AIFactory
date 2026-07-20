"""#964: build_ingest_payload must thread the origin GitHub issue into the
handoff contract provenance so TFactory can correlate the verify task with
its build + plan (the label-driven fast path carries no PFactory plan)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pfactory.tfactory_client as tc
import pytest


def _spec_dir(tmp_path: Path, req: dict[str, Any]) -> Path:
    d = tmp_path / "spec"
    d.mkdir()
    (d / "spec.md").write_text("# Feat\n\n## Acceptance Criteria\n- does the thing\n")
    (d / "requirements.json").write_text(json.dumps(req))
    return d


def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    # keep the payload builder off real git/env
    monkeypatch.setattr(tc, "_git_info_and_push", lambda *_: (None, None))
    monkeypatch.setattr(tc, "_aifactory_project_name", lambda *_: "demo")
    monkeypatch.setattr(tc, "_project_git_url", lambda *_: None)
    monkeypatch.setattr(tc, "load_task_contract", lambda *_: {})


def test_issue_backfilled_from_github_issue_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_git(monkeypatch)
    spec_dir = _spec_dir(tmp_path, {"githubIssue": {"number": 382}})
    payload = tc.build_ingest_payload(spec_dir, "048-feat")
    assert payload["contract"]["provenance"]["github_issue"] == 382


def test_issue_backfilled_from_provenance_issue_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_git(monkeypatch)
    spec_dir = _spec_dir(tmp_path, {"provenance": {"issue_number": 382}})
    payload = tc.build_ingest_payload(spec_dir, "048-feat")
    assert payload["contract"]["provenance"]["github_issue"] == 382


def test_no_issue_leaves_contract_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_git(monkeypatch)
    spec_dir = _spec_dir(tmp_path, {"description": "no issue here"})
    payload = tc.build_ingest_payload(spec_dir, "048-feat")
    # no contract seed + no issue → no synthetic provenance
    assert "contract" not in payload or "provenance" not in payload.get("contract", {})


def test_existing_contract_provenance_not_clobbered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tc, "_git_info_and_push", lambda *_: (None, None))
    monkeypatch.setattr(tc, "_aifactory_project_name", lambda *_: "demo")
    monkeypatch.setattr(tc, "_project_git_url", lambda *_: None)
    monkeypatch.setattr(
        tc, "load_task_contract", lambda *_: {"provenance": {"github_issue": 99}}
    )
    spec_dir = _spec_dir(tmp_path, {"githubIssue": {"number": 382}})
    payload = tc.build_ingest_payload(spec_dir, "048-feat")
    # setdefault: an already-signed issue wins over the requirements backfill
    assert payload["contract"]["provenance"]["github_issue"] == 99
