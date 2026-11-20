"""End-to-end W2 regression suite for approved branch/PR handoff (#2172).

Covers the four W2 lifecycle checkpoints:
1. ``_route_hard`` forwards ``project_id`` and ``base_branch`` to PFactory
   (also unit-tested in ``tests/test_intake_poller_service.py::test_route_hard_posts_the_pfactory_ingest_contract``).
2. ``POST /api/tasks/from-plan`` adopts ``factory/2172-slug`` at ``approved_head_sha``
   into the task worktree (keeping ``intent/``, ``spec/``, ``plan/``), rejects a
   tampered or drifted remote head with 422, and allows retry when the remote
   head descends from ``approved_head_sha`` with recorded build commits (see also
   ``tests/test_trusted_plan.py``, ``apps/web-server/tests/test_from_plan_route.py``,
   and ``tests/test_worktree_fresh_base.py``).
3. ``run_pr_endgame`` with ``adopted_pr`` never auto-merges even when
   ``AIFACTORY_AUTO_MERGE=true``, waits for ``tfactory_verdict == "pass"`` at the
   exact PR head SHA, marks the draft PR ready for review, and transitions issue
   labels (see also ``apps/web-server/tests/test_pr_endgame.py::test_adopted_pr_ready_after_exact_head_verdict``).
4. ``POST /api/tasks/{task_id}/apply-correction`` -> ``send_fix_completed`` posts
   only to ``<TFACTORY_BASE_URL>/api/handback/aifactory-complete`` (ignoring
   ``tfactory_callback_url``) and drops duplicate or stale SHAs (see also
   ``tests/test_handback_contract.py::test_fix_completed_posts_configured_url_once``
   and ``apps/web-server/tests/test_correction_job_backend.py::test_route_carries_task_id_but_ignores_callback_url``).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

# Match tests/conftest.py when apps/web-server/tests is invoked directly without
# loading the root conftest first.
os.environ.setdefault("APP_DISABLE_AUTH", "true")

_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from core.worktree import WorktreeError, WorktreeManager  # noqa: E402
from pfactory import tfactory_client  # noqa: E402
from pfactory.tiers import Tier  # noqa: E402
from server.routes import execution, github  # noqa: E402
from server.services import intake_poller as ip  # noqa: E402
from server.services import pr_endgame as pe  # noqa: E402
from server.services import project_settings_store as pss  # noqa: E402
from server.services.task_branch import recorded_branch  # noqa: E402
from test_from_plan_route import (  # noqa: E402
    KEY,
    _git,
    _post_from_plan,
    _signed_plan,
    handoff_project,
)
from test_pr_endgame import CmdResult, FakeRunner  # noqa: E402
from trusted_plan import APPROVAL_KEY, check_baseline_drift, sign_plan  # noqa: E402

__all__ = ["handoff_project"]

BRANCH = "factory/2172-slug"


def test_w2_route_hard_includes_project_id_and_base_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenario 1: _route_hard forwards project_id and base_branch to PFactory.

    Complements tests/test_intake_poller_service.py::test_route_hard_posts_the_pfactory_ingest_contract
    by asserting a non-None configured base_branch is preserved alongside project_id.
    """
    monkeypatch.setenv(
        "PFACTORY_INGEST_URL", "https://pfactory.example/api/plan/sessions/from-issue"
    )
    monkeypatch.setenv("PFACTORY_TOKEN", "pf-secret")
    captured: dict[str, Any] = {}

    def fake_post(
        url: str,
        payload: dict[str, Any],
        timeout: float = 10.0,
        token: str | None = None,
    ) -> None:
        captured.update(url=url, payload=payload, timeout=timeout, token=token)

    monkeypatch.setattr(ip, "_post_json", fake_post)
    cfg = ip.RepoConfig(
        provider="github",
        repo="olafkfreund/AIFactory",
        project_id="aifactory-proj",
        base_branch="dev",
        change_mode="existing",
    )
    issue = ip.IntakeIssue(
        number=2172,
        labels=["factory:hard"],
        title="Adopt approved branch handoff",
        body="## Acceptance Criteria\n- Adopt existing branch and PR\n",
    )

    ip._route_hard(cfg, issue, Tier.HARD)

    assert captured["url"] == "https://pfactory.example/api/plan/sessions/from-issue"
    assert captured["payload"]["project_id"] == "aifactory-proj"
    assert captured["payload"]["base_branch"] == "dev"
    assert captured["payload"]["issue_number"] == 2172


def test_w2_from_plan_adopts_branch_rejects_drift_and_allows_descendant_retry(
    handoff_project: tuple[str, Path, str, str],
) -> None:
    """Scenario 2: from-plan adopts factory/2172-slug at approved_head_sha,
    rejects tampered or drifted remote head with 422, and allows retry when
    remote head descends from approved_head_sha.
    """
    project_id, project_path, base_sha, _ = handoff_project

    # Seed origin/factory/2172-slug with stage artifacts at approved_sha.
    _git(project_path, "checkout", "-q", "-B", BRANCH, base_sha)
    for stage_dir in ("intent", "spec", "plan"):
        (project_path / stage_dir).mkdir(exist_ok=True)
        (project_path / stage_dir / "2172.md").write_text(f"# {stage_dir}\n")
    _git(project_path, "add", "intent", "spec", "plan")
    _git(
        project_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "approved artifacts",
    )
    approved_sha = _git(project_path, "rev-parse", "HEAD")
    _git(project_path, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    _git(project_path, "checkout", "-q", "dev")
    _git(project_path, "reset", "--hard", base_sha)
    _git(project_path, "branch", "-D", BRANCH)

    # 2a. Tampered plan (mutated approved_head_sha after signing) -> 422.
    tampered = _signed_plan(
        base_sha, approved_sha, source_branch=BRANCH, base_ref="dev"
    )
    tampered["provenance"]["approved_head_sha"] = "f" * 40
    resp_tampered, svc_tampered = _post_from_plan(
        project_id, project_path, {"plan": tampered}
    )
    assert resp_tampered.status_code == 422
    svc_tampered.start_task_execution.assert_not_awaited()

    # 2b. Valid signed plan at approved_head_sha -> 200, adopts branch & worktree.
    plan = _signed_plan(base_sha, approved_sha, source_branch=BRANCH, base_ref="dev")
    resp_ok, svc_ok = _post_from_plan(project_id, project_path, {"plan": plan})
    assert resp_ok.status_code == 200, resp_ok.text
    svc_ok.start_task_execution.assert_awaited_once()
    spec_id = resp_ok.json()["task_id"].split(":", 1)[1]
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    assert recorded_branch(project_path, spec_id) == BRANCH

    manager = WorktreeManager(project_path, base_branch="dev")
    wt_info = manager.create_worktree(spec_id)
    assert wt_info.branch == BRANCH
    assert _git(wt_info.path, "rev-parse", "HEAD") == approved_sha
    for stage_dir in ("intent", "spec", "plan"):
        assert (wt_info.path / stage_dir / "2172.md").is_file()

    # 2c. Advance origin/factory/2172-slug past approved_head_sha:
    # fresh from-plan POST and unrecorded worktree creation both reject drift.
    (wt_info.path / "impl.py").write_text("x = 1\n")
    _git(wt_info.path, "add", "impl.py")
    _git(
        wt_info.path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "coder commit",
    )
    descendant_sha = _git(wt_info.path, "rev-parse", "HEAD")
    _git(wt_info.path, "push", "origin", f"HEAD:refs/heads/{BRANCH}")

    resp_drifted, svc_drifted = _post_from_plan(
        project_id, project_path, {"plan": plan}
    )
    assert resp_drifted.status_code == 422, resp_drifted.text
    assert (
        resp_drifted.json()["detail"]["baseline_drift"]["status"]
        == "adopted_head_mismatch"
    )
    svc_drifted.start_task_execution.assert_not_awaited()
    with pytest.raises(WorktreeError, match="no longer matches approved head"):
        manager.create_worktree(spec_id)

    # 2d. Once build_commits.json records the build commit on the spec,
    # retry passes both check_baseline_drift and WorktreeManager.create_worktree.
    (spec_dir / "memory").mkdir(parents=True, exist_ok=True)
    (spec_dir / "memory" / "build_commits.json").write_text(
        json.dumps({"commits": [{"sha": descendant_sha}]})
    )
    assert check_baseline_drift(plan, project_path, spec_dir).status == "current"
    resumed = manager.create_worktree(spec_id)
    assert resumed.branch == BRANCH
    assert _git(resumed.path, "rev-parse", "HEAD") == descendant_sha


async def test_w2_adopted_pr_endgame_never_auto_merges_and_marks_ready_on_exact_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 3: run_pr_endgame with adopted_pr never auto-merges even when
    AIFACTORY_AUTO_MERGE=true, and marks the PR ready for review on TFactory
    pass at the exact head SHA.
    """
    monkeypatch.setenv("AIFACTORY_AUTO_PR", "true")
    monkeypatch.setenv("AIFACTORY_AUTO_MERGE", "true")
    monkeypatch.setenv("TFACTORY_BASE_URL", "https://tfactory.example")
    monkeypatch.setenv("TFACTORY_PROJECT_ID", "p1")
    pss._SNAPSHOT[pss.project_key(tmp_path)] = {
        "AIFACTORY_AUTO_PR": "true",
        "AIFACTORY_AUTO_MERGE": "true",
    }
    assert pe.is_auto_merge_enabled(tmp_path) is True

    spec_id = "001-w2"
    spec_dir = tmp_path / ".aifactory" / "specs" / spec_id
    spec_dir.mkdir(parents=True)
    head_sha = "c" * 40
    (spec_dir / "task_metadata.json").write_text(
        json.dumps(
            {
                "project_id": "p1",
                "base_branch": "dev",
                "source_branch": BRANCH,
                "approved_head_sha": "a" * 40,
                "pr_number": 88,
                "github_repo": "olafkfreund/AIFactory",
            }
        )
    )
    (spec_dir / "requirements.json").write_text(
        json.dumps({"provenance": {"issue_number": 2172, "pr_number": 88}})
    )

    # Drive the real _build_merge_gate -> tfactory_client.tfactory_verdict check
    # so the exact head SHA is verified against TFactory's task + verdicts response.
    monkeypatch.setattr(pe, "_build_merge_gate", pe._build_merge_gate)
    getter_urls: list[str] = []

    def fake_getter(url: str, _headers: dict[str, str]) -> tuple[int, object]:
        getter_urls.append(url)
        if url.endswith("/verdicts.json"):
            return 200, {
                "verdicts": [{"verdict": "accept"}],
                "verdict_vote": {"passed": 1, "total": 1},
            }
        return 200, {
            "project_id": "p1",
            "source_sha": head_sha,
            "status_json": {"status": "triaged"},
        }

    monkeypatch.setattr(tfactory_client, "_httpx_get", fake_getter)

    attached: list[tuple[str, int, str | None]] = []

    async def fake_attach(
        _spec_dir: Path, s_id: str, pr_num: int, repo_slug: str | None
    ) -> dict[str, Any]:
        attached.append((s_id, pr_num, repo_slug))
        return {"sent": True}

    monkeypatch.setattr(tfactory_client, "send_pr_attach", fake_attach)

    applied_labels: list[tuple[int, list[str]]] = []

    async def _remove_labels(_num: int, _labels: list[str]) -> None:
        return None

    async def _apply_labels(num: int, labels: list[str]) -> None:
        applied_labels.append((num, labels))

    monkeypatch.setattr(
        github,
        "_get_project_provider",
        lambda *_a, **_k: SimpleNamespace(
            remove_labels=_remove_labels,
            apply_labels=_apply_labels,
        ),
    )

    runner = FakeRunner(
        {
            "git push": CmdResult(0, "", ""),
            ".head.sha": CmdResult(0, head_sha, ""),
            "pr ready": CmdResult(0, "", ""),
        }
    )

    result = await pe.run_pr_endgame(
        spec_dir=spec_dir,
        spec_id=spec_id,
        trusted=None,
        worktree=tmp_path,
        branch=BRANCH,
        base="dev",
        repo="olafkfreund/AIFactory",
        auto_merge=pe.is_auto_merge_enabled(tmp_path),
        project_id="p1",
        runner=runner,
        background=False,
    )

    assert result["ok"] is True
    assert result["pr"] == 88
    assert result["ready"] is True
    assert result["merged"] is False
    assert attached == [(spec_id, 88, "olafkfreund/AIFactory")]
    assert getter_urls, "tfactory_verdict must be queried for the PR head"
    assert runner.saw("git push --force-with-lease origin factory/2172-slug")
    assert runner.saw("gh pr ready 88 --repo olafkfreund/AIFactory")
    assert not runner.saw("pr create")
    assert not runner.saw("pr merge")
    assert applied_labels == [
        (2172, ["factory:verifying"]),
        (2172, ["factory:done"]),
    ]


async def test_w2_apply_correction_to_send_fix_completed_ignores_caller_url_and_dedups_shas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 4: apply-correction -> send_fix_completed posts only to
    TFACTORY_BASE_URL/api/handback/aifactory-complete (ignoring tfactory_callback_url)
    and drops duplicate/stale SHAs.
    """
    monkeypatch.setenv("TFACTORY_BASE_URL", "https://tfactory.internal")
    monkeypatch.setenv("TFACTORY_HANDBACK_SECRET", "hb-secret-token")
    monkeypatch.delenv("AIFACTORY_FORBID_INPOD_AGENTS", raising=False)
    monkeypatch.delenv("AIFACTORY_CORRECTION_BACKEND", raising=False)
    monkeypatch.setattr(
        execution,
        "get_settings",
        lambda: SimpleNamespace(REQUIRE_CORRECTION_TRIAGE=False),
    )

    project_path = tmp_path / "proj"
    spec_id = "001-w2"
    spec_dir = project_path / ".aifactory" / "specs" / spec_id
    (spec_dir / "context").mkdir(parents=True)
    pre_fix_sha = "1" * 40
    fix_sha_1 = "2" * 40
    fix_sha_2 = "3" * 40

    (spec_dir / "task_metadata.json").write_text(
        json.dumps(
            {
                "project_id": "p1",
                "source_branch": BRANCH,
                "pr_number": 88,
            }
        )
    )
    plan = _signed_plan("0" * 40, pre_fix_sha, source_branch=BRANCH, base_ref="dev")
    plan["provenance"]["project_id"] = "p1"
    plan[APPROVAL_KEY] = sign_plan(
        plan,
        key=KEY,
        approved_by="pfactory",
        approval_timestamp="2026-11-20T12:00:00Z",
    )
    (spec_dir / "context" / "task_contract.json").write_text(json.dumps(plan))

    monkeypatch.setattr(
        execution,
        "_resolve_task",
        lambda _tid: ("p1", spec_id, project_path, spec_dir),
    )
    monkeypatch.setattr(execution, "record_handback_receipt", AsyncMock())
    monkeypatch.setattr(execution, "audit_task_action", AsyncMock())
    monkeypatch.setattr(
        tfactory_client,
        "_locate_build_commit",
        lambda *_a, **_k: (project_path, pre_fix_sha),
    )

    fixer_runs: list[Path] = []

    async def fake_fixer(s_dir: Path) -> dict[str, Any]:
        fixer_runs.append(s_dir)
        return {"status": "qa_fixing", "scheduled": True}

    monkeypatch.setattr("qa.correction._default_fixer", fake_fixer)

    req = execution.ApplyCorrectionRequest(
        fix_request_md="# Fix\nResolve failing check",
        confirm=True,
        source="tfactory",
        correlation_key="2172",
        manifest_hash="mh-2172",
        tfactory_task_id="tf-task-99",
        tfactory_callback_url="https://attacker.example/steal-callback",
        triage={
            "source": "triage",
            "failing_tests": [{"test_id": "t1", "reason": "expected 200"}],
            "manifest_hash": "mh-2172",
            "correlation_key": "2172",
        },
    )

    corr_res = await execution.apply_task_correction(
        f"p1:{spec_id}", req, _access={"sub": "tester"}
    )
    assert corr_res["success"] is True and corr_res["started"] is True
    assert fixer_runs == [spec_dir]

    receipt = json.loads((spec_dir / "handback_received.json").read_text())
    assert receipt["tfactory_task_id"] == "tf-task-99"
    assert receipt["pre_fix_sha"] == pre_fix_sha
    assert "tfactory_callback_url" not in receipt
    assert "attacker.example" not in (spec_dir / "handback_received.json").read_text()

    # Duplicate handback receipt for the same (tfactory_task_id, manifest_hash, pre_fix_sha)
    # does not re-run the fixer.
    dup_res = await execution.apply_task_correction(
        f"p1:{spec_id}", req, _access={"sub": "tester"}
    )
    assert dup_res["duplicate"] is True and dup_res["started"] is False
    assert len(fixer_runs) == 1

    posted: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def fake_poster(
        url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        posted.append((url, payload, headers))
        return {"ok": True, "status": 200}

    # Stale SHA (equal to pre_fix_sha) is dropped.
    stale = await tfactory_client.send_fix_completed(
        spec_dir, spec_id, poster=fake_poster, head_sha=pre_fix_sha
    )
    assert stale == {"sent": False, "reason": "unchanged"}
    assert posted == []

    # First new fix SHA posts to configured TFACTORY_BASE_URL (never caller callback URL).
    first = await tfactory_client.send_fix_completed(
        spec_dir, spec_id, poster=fake_poster, head_sha=fix_sha_1
    )
    assert first == {"sent": True, "status": 200, "head_sha": fix_sha_1}
    assert len(posted) == 1
    url, payload, headers = posted[0]
    assert url == "https://tfactory.internal/api/handback/aifactory-complete"
    assert "attacker.example" not in url
    assert headers["X-TFactory-Handback-Token"] == "hb-secret-token"
    assert payload == {
        "tfactory_task_id": "tf-task-99",
        "project_id": "p1",
        "spec_id": spec_id,
        "correlation_key": "2172",
        "source_branch": BRANCH,
        "head_sha": fix_sha_1,
        "pr_number": 88,
        "status": "complete",
    }

    # Duplicate fix_sha_1 is dropped via handback_sent.json.
    dup_sha = await tfactory_client.send_fix_completed(
        spec_dir, spec_id, poster=fake_poster, head_sha=fix_sha_1
    )
    assert dup_sha == {"sent": False, "reason": "unchanged"}
    assert len(posted) == 1

    # A subsequent distinct fix commit (fix_sha_2) posts once and is then recorded.
    second = await tfactory_client.send_fix_completed(
        spec_dir, spec_id, poster=fake_poster, head_sha=fix_sha_2
    )
    assert second == {"sent": True, "status": 200, "head_sha": fix_sha_2}
    assert len(posted) == 2
    assert json.loads((spec_dir / "handback_sent.json").read_text()) == {
        "sent_shas": [fix_sha_1, fix_sha_2]
    }
