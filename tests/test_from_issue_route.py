"""Tests for the ``/api/tasks/from-issue`` governed-ingest route (RFC-0011 #635).

Drives ``create_from_issue`` directly with the heavy build path mocked (the
conftest pre-mocks the agent SDK). Asserts: tier classification from labels,
the execution block written into task_metadata.json, migration forcing hard,
the provider-fetch path, and the validation error when no issue is supplied.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from fastapi import HTTPException  # noqa: E402
from server.routes import from_issue  # noqa: E402
from server.routes.from_issue import FromIssueRequest, create_from_issue  # noqa: E402


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Point the route at a temp project and stub the build start."""
    monkeypatch.setattr(from_issue, "_resolve_project_path", lambda pid: tmp_path)

    started: dict = {}

    async def _fake_start(**kwargs):
        started.update(kwargs)

    monkeypatch.setattr(from_issue, "_start_build", _fake_start)
    return tmp_path, started


def _task_metadata(tmp_path: Path, spec_id: str) -> dict:
    meta = tmp_path / ".aifactory" / "specs" / spec_id / "task_metadata.json"
    return json.loads(meta.read_text())


def _requirements(tmp_path: Path, spec_id: str) -> dict:
    req = tmp_path / ".aifactory" / "specs" / spec_id / "requirements.json"
    return json.loads(req.read_text())


async def test_low_tier_from_payload(project):
    tmp_path, started = project
    req = FromIssueRequest(
        project_id="p",
        payload={
            "number": 11,
            "title": "Tiny fix",
            "body": "do the thing",
            "labels": [{"name": "factory:low"}],
        },
    )
    res = await create_from_issue(req)
    assert res["success"] is True
    assert res["tier"] == "low"
    assert res["issue_number"] == 11
    # haiku or ollama:* — never sonnet/opus for low
    assert res["execution"]["model"] in ("haiku",) or res["execution"][
        "model"
    ].startswith("ollama:")
    assert res["execution"]["review_tier"] == "auto"
    assert res["execution"]["skip_planning"] is True

    meta = _task_metadata(tmp_path, res["spec_id"])
    assert meta["reviewTier"] == "auto"
    assert meta["skipPlanning"] is True
    # correlation: issue number stamped on requirements
    assert _requirements(tmp_path, res["spec_id"])["provenance"]["issue_number"] == 11
    # build was started with force=True (skip-planning fast path)
    assert started["execution"]["skip_planning"] is True


async def test_highest_wins_hard(project):
    _, _ = project
    req = FromIssueRequest(
        project_id="p",
        payload={
            "number": 5,
            "title": "Big",
            "labels": ["factory:low", "factory:hard"],
        },
    )
    res = await create_from_issue(req)
    assert res["tier"] == "hard"
    assert res["execution"]["model"] == "opus"
    assert res["execution"]["skip_planning"] is False
    assert res["execution"]["review_tier"] == "blocking"


async def test_migration_forces_hard(project):
    _, _ = project
    req = FromIssueRequest(
        project_id="p",
        change_mode="migration",
        payload={"number": 9, "title": "Rewrite", "labels": ["factory:low"]},
    )
    res = await create_from_issue(req)
    assert res["tier"] == "hard"
    assert res["execution"]["change_mode"] == "migration"


async def test_unlabelled_defaults_to_medium(project):
    _, _ = project
    req = FromIssueRequest(
        project_id="p",
        payload={"number": 3, "title": "No tier", "labels": ["bug"]},
    )
    res = await create_from_issue(req)
    assert res["tier"] == "medium"
    assert res["execution"]["model"] == "sonnet"


async def test_labels_override_payload_labels(project):
    _, _ = project
    req = FromIssueRequest(
        project_id="p",
        labels=["factory:hard"],
        payload={"number": 4, "title": "x", "labels": ["factory:low"]},
    )
    res = await create_from_issue(req)
    assert res["tier"] == "hard"


async def test_provider_fetch_path(project, monkeypatch):
    _, _ = project

    async def _fake_fetch(project_id, issue_number):
        assert issue_number == 42
        return {
            "number": 42,
            "title": "From provider",
            "body": "b",
            "url": "u",
            "state": "open",
            "labels": ["factory:medium"],
        }

    monkeypatch.setattr(from_issue, "_fetch_issue_via_provider", _fake_fetch)
    req = FromIssueRequest(project_id="p", provider="gitlab", issue_number=42)
    res = await create_from_issue(req)
    assert res["tier"] == "medium"
    assert res["issue_number"] == 42


async def test_redelivered_issue_is_deduplicated(project):
    """#878: re-delivering the same issue must not mint a duplicate spec/build."""
    tmp_path, started = project
    req = FromIssueRequest(
        project_id="p",
        payload={"number": 42, "title": "Once only", "labels": ["factory:low"]},
    )
    first = await create_from_issue(req)
    started.clear()

    second = await create_from_issue(req)
    assert second["success"] is True
    assert second["deduplicated"] is True
    assert second["spec_id"] == first["spec_id"]
    assert second["task_id"] == first["task_id"]
    # no second build started, no second spec dir minted
    assert started == {}
    specs = [d for d in (tmp_path / ".aifactory" / "specs").iterdir() if d.is_dir()]
    assert len(specs) == 1

    # a different issue still creates a fresh spec
    other = await create_from_issue(
        FromIssueRequest(
            project_id="p",
            payload={"number": 43, "title": "Different", "labels": ["factory:low"]},
        )
    )
    assert other.get("deduplicated") is None
    assert other["spec_id"] != first["spec_id"]


async def test_missing_issue_is_422(project):
    _, _ = project
    req = FromIssueRequest(project_id="p")
    with pytest.raises(HTTPException) as ei:
        await create_from_issue(req)
    assert ei.value.status_code == 422


async def test_unknown_project_is_404(monkeypatch):
    monkeypatch.setattr(from_issue, "_resolve_project_path", lambda pid: None)
    req = FromIssueRequest(project_id="nope", payload={"number": 1, "title": "x"})
    with pytest.raises(HTTPException) as ei:
        await create_from_issue(req)
    assert ei.value.status_code == 404


async def test_write_spec_creates_spec_md(project):
    """#806: find_spec requires spec.md; without it every tier dies at spawn."""
    tmp_path, _started = project
    req = FromIssueRequest(
        project_id="p",
        payload={
            "number": 12,
            "title": "Add Min/Max printer",
            "body": "support Min and Max in pycode",
            "labels": [{"name": "tier:medium"}],
        },
    )
    res = await create_from_issue(req)
    spec_md = tmp_path / ".aifactory" / "specs" / res["spec_id"] / "spec.md"
    assert spec_md.exists()
    content = spec_md.read_text()
    assert "Add Min/Max printer" in content
    assert "support Min and Max in pycode" in content


async def test_intake_opts_into_tfactory_handoff_by_default(project, monkeypatch):
    """An intake build opts into TFactory auto-handoff so the finished build is
    independently verified (the handoff itself no-ops unless TFACTORY_BASE_URL is
    set, so this is safe when TFactory is absent)."""
    tmp_path, _ = project
    monkeypatch.delenv("AIFACTORY_INTAKE_AUTO_HANDOFF", raising=False)
    req = FromIssueRequest(
        project_id="p",
        payload={
            "number": 30,
            "title": "x",
            "body": "y",
            "labels": [{"name": "factory:low"}],
        },
    )
    res = await create_from_issue(req)
    meta = _task_metadata(tmp_path, res["spec_id"])
    assert meta.get("auto_handover_tfactory") is True


async def test_intake_handoff_can_be_disabled(project, monkeypatch):
    tmp_path, _ = project
    monkeypatch.setenv("AIFACTORY_INTAKE_AUTO_HANDOFF", "off")
    req = FromIssueRequest(
        project_id="p",
        payload={
            "number": 31,
            "title": "x",
            "body": "y",
            "labels": [{"name": "factory:low"}],
        },
    )
    res = await create_from_issue(req)
    meta = _task_metadata(tmp_path, res["spec_id"])
    assert not meta.get("auto_handover_tfactory")


# ── parallel control: label -> task_metadata round-trip ────────────────────


async def _ingest(labels: list[str]) -> tuple:
    """Ingest an issue carrying ``labels``; return (result, spec_id)."""
    res = await create_from_issue(
        FromIssueRequest(
            project_id="p",
            payload={
                "number": 77,
                "title": "Parallel me",
                "body": "several independent files",
                "labels": [{"name": n} for n in labels],
            },
        )
    )
    return res, res["spec_id"]


@pytest.fixture(autouse=True)
def _no_ambient_parallel_env(monkeypatch):
    monkeypatch.delenv("AIFACTORY_INTAKE_PARALLEL", raising=False)
    monkeypatch.delenv("AIFACTORY_INTAKE_WORKERS", raising=False)


async def test_parallel_label_reaches_task_metadata(project):
    tmp_path, _ = project
    res, spec_id = await _ingest(["factory:medium", "factory:parallel"])
    assert res["execution"]["parallel"] is True
    # The payoff: the agent service reads THIS file back when it starts the
    # build, so the harness actually runs concurrently.
    assert _task_metadata(tmp_path, spec_id)["parallel"] is True


async def test_workers_label_reaches_task_metadata(project):
    tmp_path, _ = project
    _, spec_id = await _ingest(["factory:parallel", "factory:workers=4"])
    meta = _task_metadata(tmp_path, spec_id)
    assert meta["parallel"] is True
    assert meta["workers"] == 4


async def test_unlabelled_intake_stays_serial(project):
    tmp_path, _ = project
    res, spec_id = await _ingest(["factory:medium"])
    assert res["execution"]["parallel"] is False
    meta = _task_metadata(tmp_path, spec_id)
    assert meta["parallel"] is False
    assert "workers" not in meta


async def test_serial_label_overrides_deployment_default(project, monkeypatch):
    tmp_path, _ = project
    monkeypatch.setenv("AIFACTORY_INTAKE_PARALLEL", "true")
    _, spec_id = await _ingest(["factory:serial"])
    assert _task_metadata(tmp_path, spec_id)["parallel"] is False


async def test_deployment_default_applies_when_unlabelled(project, monkeypatch):
    tmp_path, _ = project
    monkeypatch.setenv("AIFACTORY_INTAKE_PARALLEL", "1")
    _, spec_id = await _ingest(["factory:medium"])
    assert _task_metadata(tmp_path, spec_id)["parallel"] is True
