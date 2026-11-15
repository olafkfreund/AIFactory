"""Org installation credentials reach only their project's autonomous work."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

WEB_SERVER = Path(__file__).resolve().parents[1]
BACKEND = WEB_SERVER.parent / "backend"
for root in (WEB_SERVER, BACKEND):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

from core.git_credentials import github_token  # noqa: E402
from server import project_registry  # noqa: E402
from server.routes import project_authz, projects  # noqa: E402
from server.services import (  # noqa: E402
    completion_orchestration as completion,
)
from server.services import (
    github_app,
    merger,
    pr_endgame,
    project_settings_store,
    project_workspace_service,
)


@pytest.mark.parametrize(
    "org,stored,expected",
    [
        ("acme", False, "ghs_acme_1700"),
        ("acme", True, "ghs_acme_1700"),
        ("other", False, ""),
        ("off", False, "ambient-1700"),
    ],
)
def test_project_clone_uses_owner_org(
    org: str,
    stored: bool,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "repo"
    path.mkdir()
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-1700")
    monkeypatch.setattr(
        github_app, "_org_installations", {} if org == "off" else {"acme": "12"}
    )
    monkeypatch.setattr(
        github_app, "_tokens", {"12": ("ghs_acme_1700", time.time() + 3600)}
    )
    monkeypatch.setattr(projects, "load_projects", lambda: {})
    monkeypatch.setattr(projects, "save_projects", lambda _items: None)
    monkeypatch.setattr(projects, "project_to_response", lambda _id, data: data)
    monkeypatch.setattr(projects, "multi_tenant_enabled", lambda: False)
    monkeypatch.setattr(project_workspace_service, "workspace_root", lambda: tmp_path)

    async def owner(_request: object, _db: AsyncSession) -> str:
        return org

    async def credential(_id: str) -> tuple[str, str]:
        return "user", "stored-token-1700"

    seen: list[tuple[str, tuple[str, str] | None]] = []

    async def clone_or_update(
        *, git_url: str, branch: str | None, credential: tuple[str, str] | None
    ) -> Path:
        seen.append((github_token(), credential))
        return path

    monkeypatch.setattr(project_authz, "resolve_owner_org_id", owner)
    monkeypatch.setattr(projects, "_resolve_git_credential", credential)
    monkeypatch.setattr(project_workspace_service, "clone_or_update", clone_or_update)
    request = projects.ProjectCreate(
        gitUrl="https://github.com/acme/repo.git",
        gitCredentialId="stored" if stored else None,
    )

    async def run() -> None:
        async with AsyncSession() as db:
            await projects.add_project(request, request=object(), db=db)

    asyncio.run(run())
    assert seen == [(expected, ("user", "stored-token-1700") if stored else None)]


def test_project_clone_mint_error_returns_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_app, "_org_installations", {"acme": "12"})
    monkeypatch.setattr(github_app, "_tokens", {})

    def fail(_org: str) -> None:
        raise RuntimeError("mint failed")

    monkeypatch.setattr(github_app, "token_for_org", fail)

    async def owner(_request: object, _db: AsyncSession) -> str:
        return "acme"

    monkeypatch.setattr(project_authz, "resolve_owner_org_id", owner)

    async def run() -> None:
        async with AsyncSession() as db:
            with pytest.raises(HTTPException) as exc:
                await projects.add_project(
                    projects.ProjectCreate(gitUrl="https://github.com/acme/repo.git"),
                    request=object(),
                    db=db,
                )
            assert exc.value.status_code == 400

    asyncio.run(run())


@pytest.mark.parametrize("org,expected", [("acme", "ghs_acme_1700"), ("other", "")])
def test_terminal_endgame_uses_project_org(
    org: str,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    spec = tmp_path / "spec"
    spec.mkdir()
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"p": {"path": str(project), "org_id": org}}))
    monkeypatch.setattr(project_registry, "get_projects_file", lambda: registry)
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-1700")
    monkeypatch.setattr(github_app, "_org_installations", {"acme": "12"})
    monkeypatch.setattr(
        github_app, "_tokens", {"12": ("ghs_acme_1700", time.time() + 3600)}
    )
    monkeypatch.setattr(completion, "_build_wrote_nothing", lambda *_args: False)
    monkeypatch.setattr(completion, "sync_spec_file_from_worktree", lambda *_args: None)
    monkeypatch.setattr(completion, "handoff_contract", lambda *_args: None)

    async def no_settings(_path: Path) -> dict[str, str]:
        return {}

    async def no_trust(_path: Path) -> None:
        return None

    monkeypatch.setattr(project_settings_store, "get_settings", no_settings)
    monkeypatch.setattr(completion, "lookup", no_trust)
    monkeypatch.setattr(
        pr_endgame, "apply_path_risk_floor", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(pr_endgame, "is_auto_pr_enabled", lambda _path: True)
    monkeypatch.setattr(pr_endgame, "is_auto_merge_enabled", lambda _path: False)
    monkeypatch.setattr(pr_endgame, "resolve_pr_reviewer", lambda _path: "copilot")
    seen: list[str] = []

    def gather(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        seen.append(github_token())
        return {
            "worktree": project,
            "branch": "feature",
            "base": "main",
            "repo": "acme/repo",
        }

    async def endgame(**_kwargs: Any) -> dict[str, bool]:
        seen.append(github_token())
        return {"ok": True}

    monkeypatch.setattr(pr_endgame, "gather_pr_context", gather)
    monkeypatch.setattr(pr_endgame, "run_pr_endgame", endgame)
    monkeypatch.setattr(merger, "process_one", lambda *_args: {})
    asyncio.run(
        completion.run_terminal_completion(
            spec_dir=spec,
            project_path=project,
            spec_id="1",
            task_id="p:1",
            backend_path=BACKEND,
            is_terminal=False,
            is_completed=True,
            terminal_status="completed",
            logger=logging.getLogger(__name__),
        )
    )
    assert seen == [expected, expected]


def test_background_pr_watcher_keeps_org_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-1700")
    monkeypatch.setattr(github_app, "_org_installations", {"acme": "12"})
    monkeypatch.setattr(
        github_app, "_tokens", {"12": ("ghs_acme_1700", time.time() + 3600)}
    )
    monkeypatch.setattr(pr_endgame, "_pr_title_body", lambda *_args: ("title", "body"))
    monkeypatch.setattr(pr_endgame, "review_tier_note", lambda *_args, **_kw: "")
    monkeypatch.setattr(pr_endgame, "trailer_block", lambda *_args: "")
    monkeypatch.setattr(pr_endgame, "create_pr", lambda **_kw: 1)
    observed: list[str] = []

    async def run() -> None:
        ready = asyncio.Event()

        async def watch(**_kw: Any) -> dict[str, bool]:
            await ready.wait()
            observed.append(github_token())
            return {"ok": True}

        monkeypatch.setattr(pr_endgame, "watch_and_finish", watch)
        with github_app.org_github_token("acme"):
            result = await pr_endgame.run_pr_endgame(
                spec_dir=tmp_path,
                spec_id="1",
                trusted=None,
                worktree=tmp_path,
                branch="feature",
                base="main",
                repo="acme/repo",
                auto_merge=False,
            )
        assert result["watching"] is True
        ready.set()
        await asyncio.wait_for(asyncio.gather(*pr_endgame._BACKGROUND_TASKS), 1)

    asyncio.run(run())
    assert observed == ["ghs_acme_1700"]
