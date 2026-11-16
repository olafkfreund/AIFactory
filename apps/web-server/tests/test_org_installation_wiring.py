"""Org installation credentials reach only their project's autonomous work."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
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
from server.routes import github, project_authz, projects  # noqa: E402
from server.services import (  # noqa: E402
    completion_orchestration,
    github_app,
    merger,
    pr_endgame,
    project_settings_store,
    project_workspace_service,
)

github_routes = github
completion = completion_orchestration


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
        *, credential: tuple[str, str] | None, **_kw: Any
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
    monkeypatch: pytest.MonkeyPatch,
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

    loop_thread = threading.get_ident()

    def gather(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        assert threading.get_ident() != loop_thread  # reads may mint (#2038)
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


def test_web_login_is_refused_in_multi_installation_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_app, "_org_installations", {"acme": "12"})
    monkeypatch.setattr(github_routes.shutil, "which", lambda _name: "/usr/bin/gh")

    def no_spawn(*_args: Any, **_kw: Any) -> None:
        raise AssertionError("gh must not be started")

    monkeypatch.setattr(github_routes.subprocess, "Popen", no_spawn)
    monkeypatch.setattr(github_routes.asyncio, "create_subprocess_exec", no_spawn)
    result = asyncio.run(github_routes.start_github_auth(object()))
    assert result["data"]["success"] is False


def _two_orgs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    acme = tmp_path / "acme"
    other = tmp_path / "other"
    acme.mkdir()
    other.mkdir()
    registry = tmp_path / "projects.json"
    registry.write_text(
        json.dumps(
            {
                "a": {"path": str(acme), "org_id": "acme"},
                "o": {"path": str(other), "org_id": "other"},
            }
        )
    )
    monkeypatch.setattr(project_registry, "get_projects_file", lambda: registry)
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-1700")
    monkeypatch.setattr(github_app, "_org_installations", {"acme": "12", "other": "13"})
    monkeypatch.setattr(
        github_app,
        "_tokens",
        {
            "12": ("ghs_acme_1700", time.time() + 3600),
            "13": ("ghs_other_1700", time.time() + 3600),
        },
    )
    return acme, other


def test_project_github_token_follows_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme, other = _two_orgs(tmp_path, monkeypatch)
    with github_app.project_github_token(acme / "wt"):
        assert github_token() == "ghs_acme_1700"
        # No path keeps the outer org; an unowned path never does.
        with github_app.project_github_token(None):
            assert github_token() == "ghs_acme_1700"
        with github_app.project_github_token(tmp_path / "unregistered"):
            assert github_token() == ""
        with github_app.project_github_token(other):
            assert github_token() == "ghs_other_1700"
    with github_app.project_github_token(None):
        assert github_token() == ""


def test_project_github_token_is_a_noop_without_the_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-1700")
    monkeypatch.setattr(github_app, "_org_installations", {})
    with github_app.project_github_token(tmp_path):
        assert github_token() == "ambient-1700"


def test_gh_helper_sends_the_project_orgs_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.services import gh  # noqa: PLC0415

    acme, other = _two_orgs(tmp_path, monkeypatch)
    seen: list[str] = []

    def run(*_args: Any, env: dict[str, str], **_kw: Any) -> Any:
        hosts = (
            Path(env["GH_CONFIG_DIR"]) / "hosts.yml" if "GH_CONFIG_DIR" in env else None
        )
        seen.append(
            json.loads(hosts.read_text())["github.com"]["oauth_token"] if hosts else ""
        )
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(gh.subprocess, "run", run)
    gh.run_gh_command(["x"], cwd=str(acme))
    gh.run_gh_command(["x"], cwd=str(other / "wt"))
    gh.run_gh_command(["x"], cwd=str(tmp_path))
    gh.run_gh_command(["x"])
    assert seen == ["ghs_acme_1700", "ghs_other_1700", "", ""]


def test_project_route_dependency_pins_the_authorised_org(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import FastAPI, HTTPException  # noqa: PLC0415
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from server.routes import project_github  # noqa: PLC0415
    from server.services import gh  # noqa: PLC0415

    acme, other = _two_orgs(tmp_path, monkeypatch)

    class Checker:
        async def _authorize(self, pid: str, _request: object, _db: object) -> None:
            if pid != "a":
                raise HTTPException(status_code=403)

    async def db():  # stands in for the get_db generator
        yield object()

    monkeypatch.setattr(project_github, "get_db", db)
    monkeypatch.setattr(project_authz, "require_project_access", lambda _r: Checker())
    seen: list[str] = []

    def run(*_args: Any, env: dict[str, str], **_kw: Any) -> Any:
        hosts = (
            Path(env["GH_CONFIG_DIR"]) / "hosts.yml" if "GH_CONFIG_DIR" in env else None
        )
        seen.append(
            json.loads(hosts.read_text())["github.com"]["oauth_token"] if hosts else ""
        )
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(gh.subprocess, "run", run)
    router = github_routes.APIRouter(
        dependencies=[github_routes.Depends(project_github.project_github_token)]
    )

    @router.get("/p/{projectId}")  # type: ignore[untyped-decorator]
    async def by_path(projectId: str) -> dict[str, str]:  # noqa: ARG001, N803
        gh.run_gh_command(["x"], cwd=str(acme / "wt"))  # same org: kept
        gh.run_gh_command(["x"], cwd=str(other))  # another org's path: no token
        return {"token": github_token()}

    @router.get("/q")  # type: ignore[untyped-decorator]
    async def by_query() -> dict[str, str]:
        return {"token": github_token()}

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get("/p/a").json() == {"token": "ghs_acme_1700"}
    assert seen == ["ghs_acme_1700", ""]
    assert client.get("/q", params={"projectId": "a"}).json() == {
        "token": "ghs_acme_1700"
    }
    assert client.get("/q").json() == {"token": ""}  # no project: no token
    assert client.get("/p/o").status_code == 403  # denied: handler never runs
    assert seen == ["ghs_acme_1700", ""]
    # Writes need the member role: a viewer may read but not POST.
    roles: list[str] = []

    def access(role: str) -> Checker:
        roles.append(role)
        return Checker()

    monkeypatch.setattr(project_authz, "require_project_access", access)
    client.get("/q", params={"projectId": "a"})
    router.post("/w")(by_query)
    app.include_router(router)
    client.post("/w", params={"projectId": "a"})
    assert roles == ["viewer", "member"]
    assert github_token() == "ambient-1700"  # provider reset after the requests


def test_event_loop_thread_never_mints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_orgs(tmp_path, monkeypatch)
    monkeypatch.setattr(github_app, "_tokens", {})  # cold cache
    minted: list[str] = []

    def mint(installation: str, **_kw: Any) -> str:
        minted.append(installation)
        return "ghs_minted"

    monkeypatch.setattr(github_app, "installation_token", mint)

    async def on_loop() -> str:
        with github_app.org_github_token("acme"):
            return str(github_token())

    assert asyncio.run(on_loop()) == ""
    assert minted == []
    with github_app.org_github_token("acme"):  # a worker thread may mint
        assert github_token() == "ghs_minted"


def test_sweep_uses_each_projects_org(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_orgs(tmp_path, monkeypatch)
    seen: dict[str, str] = {}

    def process(project_id: str, *_args: Any, **_kw: Any) -> dict[str, Any]:
        seen[project_id] = github_token()
        return {"action": "skipped", "project": project_id}

    monkeypatch.setattr(merger, "_process_spec", process)
    monkeypatch.setattr(merger, "get_spec_dirs", lambda _path: [Path("spec")])
    monkeypatch.setattr(merger, "_sync_status", lambda *_args: True)
    merger.sweep(dry_run=False)
    assert seen == {"a": "ghs_acme_1700", "o": "ghs_other_1700"}


def test_copilot_dispatch_uses_the_task_projects_org(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.routes import execution  # noqa: PLC0415
    from server.services import copilot_dispatch_service as cds  # noqa: PLC0415

    acme, _other = _two_orgs(tmp_path, monkeypatch)
    spec = tmp_path / "spec"
    spec.mkdir()
    monkeypatch.setenv("AIFACTORY_COPILOT_DISPATCH_ENABLED", "true")
    monkeypatch.setattr(execution, "_resolve_task", lambda _id: ("a", "1", acme, spec))
    seen: list[str] = []

    def dispatch(_self: object, *_args: Any) -> dict[str, str]:
        seen.append(github_token())
        return {"agent_handle": "h", "dispatched_at": "now"}

    async def watch(**_kw: Any) -> None:
        seen.append(github_token())

    async def noop(*_args: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(cds.CopilotDispatchService, "dispatch", dispatch)
    monkeypatch.setattr(cds, "watch_for_copilot_pr", watch)
    monkeypatch.setattr(execution, "emit_task_status", noop)
    monkeypatch.setattr(execution, "audit_task_action", noop)
    monkeypatch.setattr(execution, "_patch_task_metadata", lambda *_args: None)

    async def run() -> None:
        await execution.dispatch_task_to_copilot(
            "a:1",
            execution.CopilotDispatchRequest(repo_full_name="acme/r", issue_number=3),
            _access={},
        )
        await asyncio.sleep(0)  # let the spawned watcher run

    asyncio.run(run())
    assert seen == ["ghs_acme_1700", "ghs_acme_1700"]


def test_installation_token_is_never_persisted_in_the_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github_app, "_org_installations", {"acme": "12"})

    def no_gh(*_args: Any, **_kw: Any) -> None:
        raise AssertionError("gh must not run")

    monkeypatch.setattr(github_routes, "run_gh_command", no_gh)
    assert asyncio.run(github_routes._persist_cli_token_to_project("p")) is False


def test_org_map_errors_name_the_bad_part() -> None:
    with pytest.raises(RuntimeError, match="installation id for org 'acme'") as exc:
        github_app._parse_org_map("acme=12a")
    assert "12a" not in str(exc.value)
    with pytest.raises(RuntimeError, match="duplicate org id 'acme'"):
        github_app._parse_org_map("acme=1,acme=2")
    with pytest.raises(RuntimeError, match="invalid or duplicate org id ''"):
        github_app._parse_org_map("=12")


def test_overlapping_project_is_refused_before_any_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acme, _other = _two_orgs(tmp_path, monkeypatch)
    child = acme / "child"
    monkeypatch.setattr(projects, "browse_roots", lambda: [tmp_path])

    async def owner(_request: object, _db: AsyncSession) -> str:
        return "other"

    monkeypatch.setattr(project_authz, "resolve_owner_org_id", owner)

    class Db(AsyncSession):  # isinstance check only; never used
        def __init__(self) -> None:
            pass

    async def run() -> None:
        with pytest.raises(HTTPException) as exc:
            await projects.add_project(
                projects.ProjectCreate(path=str(child)), request=object(), db=Db()
            )
        assert exc.value.status_code == 409
        assert "acme" not in str(exc.value.detail)

    asyncio.run(run())
    assert not child.exists()  # nothing was created
    monkeypatch.setattr(github_app, "_org_installations", {})


def test_warm_up_waits_for_auto_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    spec = tmp_path / "spec"
    spec.mkdir()
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps({"p": {"path": str(project), "org_id": "acme"}}))
    monkeypatch.setattr(project_registry, "get_projects_file", lambda: registry)
    monkeypatch.setattr(github_app, "_org_installations", {"acme": "12"})
    monkeypatch.setattr(completion, "_build_wrote_nothing", lambda *_args: False)
    monkeypatch.setattr(completion, "sync_spec_file_from_worktree", lambda *_a: None)
    monkeypatch.setattr(completion, "handoff_contract", lambda *_args: None)

    async def no_settings(_path: Path) -> dict[str, str]:
        return {}

    async def no_trust(_path: Path) -> None:
        return None

    monkeypatch.setattr(project_settings_store, "get_settings", no_settings)
    monkeypatch.setattr(completion, "lookup", no_trust)
    monkeypatch.setattr(pr_endgame, "is_auto_pr_enabled", lambda _path: False)
    monkeypatch.setattr(merger, "process_one", lambda *_args: {})
    warmed: list[str | None] = []

    async def warm(org: str | None) -> None:
        warmed.append(org)

    monkeypatch.setattr(github_app, "warm_org_token", warm)
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
    assert warmed == []
