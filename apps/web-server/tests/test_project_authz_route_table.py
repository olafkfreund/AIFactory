"""Generated route-table authz walker and require_project_role matrix tests (#2181)."""

from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from server.database.engine import get_db
from server.database.models import (
    Base,
    Organization,
    OrgMember,
    Team,
    TeamMembership,
    User,
)
from server.main import create_app
from server.mcp_remote import tools as mcp_remote_tools
from server.mcp_stdio.router import investigation_router
from server.rmux import console_router
from server.routes import context, copilot_mcp, investigations, project_authz
from server.routes.project_github import project_github_token
from server.utils.roles import ROLE_LEVELS
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_DUMMY_HASH = "x"
_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

# Symbols that enforce or delegate project-level authorization.
PROJECT_AUTHZ_PRIMITIVES: frozenset[str] = frozenset(
    {
        "require_project_role",
        "require_project_access",
        "require_task_access",
        "ProjectAccessChecker",
        "TaskAccessChecker",
        "accessible_project_ids",
        "accessible_org_ids",
        "resolve_owner_org_id",
        "require_acw_access",
        "authorize_project_for_user",
        "_project_authorized",
        "_org_lookup_ok",
        "may_use",
        "_owned",
        "_authorize_path",
        "TaskSocketAccess",
        "AllTaskSocketAccess",
        "_pod_auth",
        "_service_only",
    }
)

# Explicit allowlist of non-project routes (auth, orgs, keys, settings, host utilities).
NON_PROJECT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/"),
        ("GET", "/.well-known/agent-skills/index.json"),
        ("GET", "/api/admin/access-review"),
        ("GET", "/api/audit/export"),
        ("GET", "/api/auth/identity-providers"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/logout"),
        ("GET", "/api/auth/me"),
        ("GET", "/api/auth/oidc/callback"),
        ("GET", "/api/auth/oidc/enabled"),
        ("GET", "/api/auth/oidc/login"),
        ("POST", "/api/auth/oidc/logout"),
        ("POST", "/api/auth/oidc/refresh"),
        ("POST", "/api/auth/refresh"),
        ("POST", "/api/auth/register"),
        ("GET", "/api/capabilities"),
        ("POST", "/api/claude-code/install"),
        ("GET", "/api/claude-code/version"),
        ("GET", "/api/email/accounts"),
        ("DELETE", "/api/email/accounts/{account_id}"),
        ("GET", "/api/email/auth/gmail/callback"),
        ("GET", "/api/email/auth/gmail/start"),
        ("GET", "/api/email/auth/outlook/callback"),
        ("GET", "/api/email/auth/outlook/start"),
        ("GET", "/api/email/credentials-status"),
        ("POST", "/api/email/test/{account_id}"),
        ("GET", "/api/git-credentials"),
        ("POST", "/api/git-credentials"),
        ("DELETE", "/api/git-credentials/{cred_id}"),
        ("GET", "/api/github/auth/check"),
        ("POST", "/api/github/auth/start"),
        ("GET", "/api/github/auth/status"),
        ("POST", "/api/github/cli/install"),
        ("GET", "/api/github/features"),
        ("GET", "/api/github/models"),
        ("GET", "/api/github/orgs"),
        ("GET", "/api/github/token"),
        ("GET", "/api/github/user"),
        ("GET", "/api/health"),
        ("GET", "/api/keys"),
        ("POST", "/api/keys"),
        ("DELETE", "/api/keys/{key_id}"),
        ("GET", "/api/llm-endpoints"),
        ("POST", "/api/llm-endpoints"),
        ("POST", "/api/llm-endpoints/test"),
        ("DELETE", "/api/llm-endpoints/{endpoint_id}"),
        ("GET", "/api/llm-endpoints/{endpoint_id}"),
        ("PUT", "/api/llm-endpoints/{endpoint_id}"),
        ("POST", "/api/llm-endpoints/{endpoint_id}/test"),
        ("DELETE", "/api/logs/"),
        ("GET", "/api/logs/"),
        ("POST", "/api/logs/frontend"),
        ("DELETE", "/api/logs/{log_type}"),
        ("GET", "/api/logs/{log_type}"),
        ("GET", "/api/logs/{log_type}/download"),
        ("GET", "/api/logs/{log_type}/raw"),
        ("GET", "/api/mcp/detect"),
        ("POST", "/api/mcp/health"),
        ("POST", "/api/mcp/test-connection"),
        ("GET", "/api/memory/databases"),
        ("GET", "/api/memory/infrastructure"),
        ("POST", "/api/memory/test-connection"),
        ("POST", "/api/memory/test-graphiti"),
        ("POST", "/api/memory/validate-api-key"),
        ("GET", "/api/needs-you/count"),
        ("GET", "/api/notifications"),
        ("POST", "/api/notifications/read-all"),
        ("GET", "/api/notifications/unread-count"),
        ("POST", "/api/notifications/{notification_id}/read"),
        ("GET", "/api/ollama/embedding-models"),
        ("POST", "/api/ollama/install"),
        ("GET", "/api/ollama/installed"),
        ("GET", "/api/ollama/models"),
        ("POST", "/api/ollama/pull"),
        ("GET", "/api/ollama/status"),
        ("GET", "/api/projects/default-location"),
        ("GET", "/api/orgs"),
        ("POST", "/api/orgs"),
        ("DELETE", "/api/orgs/{org_id}"),
        ("GET", "/api/orgs/{org_id}"),
        ("PUT", "/api/orgs/{org_id}"),
        ("GET", "/api/orgs/{org_id}/audit"),
        ("GET", "/api/orgs/{org_id}/members"),
        ("POST", "/api/orgs/{org_id}/members/invite"),
        ("DELETE", "/api/orgs/{org_id}/members/{user_id}"),
        ("PUT", "/api/orgs/{org_id}/members/{user_id}"),
        ("GET", "/api/search"),
        ("GET", "/api/settings"),
        ("PUT", "/api/settings"),
        ("POST", "/api/settings/api-key"),
        ("GET", "/api/settings/api-profiles"),
        ("POST", "/api/settings/api-profiles"),
        ("POST", "/api/settings/api-profiles/active"),
        ("POST", "/api/settings/api-profiles/discover-models"),
        ("POST", "/api/settings/api-profiles/test"),
        ("DELETE", "/api/settings/api-profiles/{profile_id}"),
        ("PUT", "/api/settings/api-profiles/{profile_id}"),
        ("GET", "/api/settings/auth-status"),
        ("GET", "/api/settings/auto-switch"),
        ("PATCH", "/api/settings/auto-switch"),
        ("GET", "/api/settings/claude-credentials-exist"),
        ("GET", "/api/settings/claude-profiles"),
        ("POST", "/api/settings/claude-profiles"),
        ("POST", "/api/settings/claude-profiles/active"),
        ("GET", "/api/settings/claude-profiles/best"),
        ("DELETE", "/api/settings/claude-profiles/{profile_id}"),
        ("PATCH", "/api/settings/claude-profiles/{profile_id}"),
        ("POST", "/api/settings/claude-profiles/{profile_id}/complete-oauth"),
        ("POST", "/api/settings/claude-profiles/{profile_id}/initialize"),
        ("POST", "/api/settings/claude-profiles/{profile_id}/start-oauth"),
        ("POST", "/api/settings/claude-profiles/{profile_id}/token"),
        ("GET", "/api/settings/cli-accounts/detect"),
        ("DELETE", "/api/settings/cli-accounts/{cli}"),
        ("POST", "/api/settings/cli-accounts/{cli}/api-key"),
        ("POST", "/api/settings/cli-accounts/{cli}/import"),
        ("POST", "/api/settings/cli-accounts/{cli}/install"),
        ("POST", "/api/settings/cli-accounts/{cli}/start-login"),
        ("POST", "/api/settings/cli-accounts/{cli}/start-login-terminal"),
        ("GET", "/api/settings/cli-accounts/{cli}/status"),
        ("POST", "/api/settings/import-claude-credentials"),
        ("GET", "/api/settings/local-llm/detect"),
        ("GET", "/api/settings/ollama/models"),
        ("POST", "/api/settings/ollama/pull"),
        ("POST", "/api/settings/ollama/test"),
        ("GET", "/api/settings/openai-compat/models"),
        ("POST", "/api/settings/openai-compat/test"),
        ("POST", "/api/settings/reset"),
        ("POST", "/api/settings/retry-with-profile"),
        ("GET", "/api/settings/source-env"),
        ("PATCH", "/api/settings/source-env"),
        ("GET", "/api/settings/source-token-check"),
        ("GET", "/api/settings/tab-state"),
        ("PUT", "/api/settings/tab-state"),
        ("GET", "/api/settings/token"),
        ("POST", "/api/settings/token/regenerate"),
        ("POST", "/api/settings/usage-update"),
        ("GET", "/api/skills/categories"),
        ("GET", "/api/skills/list"),
        ("GET", "/api/skills/search"),
        ("GET", "/api/skills/suggest"),
        ("GET", "/api/skills/{category}/{skill_name}"),
        ("POST", "/api/tasks/worktree/detect-tools"),
        ("POST", "/api/terminals/generate-name"),
        ("POST", "/api/terminals/restore"),
        ("POST", "/api/terminals/restore-date"),
        ("GET", "/api/terminals/session-dates"),
        ("GET", "/api/terminals/sessions/{date}"),
        ("POST", "/api/terminals/{terminal_id}/invoke-claude"),
        ("POST", "/api/terminals/{terminal_id}/resume-claude"),
        ("GET", "/api/updates/source/check"),
        ("POST", "/api/updates/source/download"),
        ("GET", "/api/updates/source/version"),
        ("POST", "/api/users/{user_id}/gdpr-erasure"),
        ("GET", "/metrics"),
    }
)


def _is_server_callable(obj: object) -> bool:
    return callable(obj) and str(getattr(obj, "__module__", "")).startswith("server.")


def _extract_ast_symbols(
    fn: object,
    depth: int = 3,
    stack: frozenset[int] = frozenset(),
) -> set[str]:
    """Collect identifier & attribute names referenced by ``fn`` and its local helpers."""
    fn = inspect.unwrap(fn)  # type: ignore[arg-type]
    obj_id = id(fn)
    if obj_id in stack or depth < 0:
        return set()
    next_stack = stack | {obj_id}

    try:
        src = textwrap.dedent(inspect.getsource(fn))  # type: ignore[arg-type]
        tree = ast.parse(src)
    except (OSError, TypeError, SyntaxError):
        return set()

    names: set[str] = set()
    attrs: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
            if isinstance(node.value, ast.Name):
                attrs.append((node.value.id, node.attr))

    if names & PROJECT_AUTHZ_PRIMITIVES or depth == 0:
        return names

    globals_dict = getattr(fn, "__globals__", {}) or {}
    for name in list(names):
        target = globals_dict.get(name)
        if _is_server_callable(target):
            names |= _extract_ast_symbols(target, depth - 1, next_stack)
    for mod_name, attr_name in attrs:
        mod_obj = globals_dict.get(mod_name)
        target = getattr(mod_obj, attr_name, None) if mod_obj is not None else None
        if _is_server_callable(target):
            names |= _extract_ast_symbols(target, depth - 1, next_stack)
    return names


def _walk_dependant_guards(dependant: object) -> set[str]:
    found: set[str] = set()
    for dep in getattr(dependant, "dependencies", ()):
        call = getattr(dep, "call", None)
        if call is not None:
            cls_name = type(call).__name__
            fn_name = getattr(call, "__name__", "")
            if cls_name in PROJECT_AUTHZ_PRIMITIVES:
                found.add(cls_name)
            if fn_name in PROJECT_AUTHZ_PRIMITIVES:
                found.add(fn_name)
            found |= _extract_ast_symbols(call) & PROJECT_AUTHZ_PRIMITIVES
        found |= _walk_dependant_guards(dep)
    return found


def _route_authz_guards(route: APIRoute | APIWebSocketRoute) -> set[str]:
    guards = _walk_dependant_guards(route.dependant)
    if route.endpoint is not None:
        guards |= _extract_ast_symbols(route.endpoint) & PROJECT_AUTHZ_PRIMITIVES
    return guards


def _actual_project_checker(route: APIRoute) -> bool:
    def walk(dependant: object) -> bool:
        return any(
            isinstance(getattr(dep, "call", None), project_authz.ProjectAccessChecker)
            or getattr(getattr(dep, "call", None), "project_guard", False)
            or (dep.call is project_github_token and "{projectId}" in route.path)
            or walk(dep)
            for dep in getattr(dependant, "dependencies", ())
        )

    return walk(route.dependant)


def test_unguarded_route_fails_actual_checker() -> None:
    app = FastAPI()

    @app.get("/projects/{project_id}/env")
    async def unguarded(project_id: str):
        return {"projectId": project_id}

    route = next(r for r in app.routes if isinstance(r, APIRoute))
    assert not _actual_project_checker(route)


def test_every_project_route_ws_and_mcp_tool_is_guarded() -> None:
    """Check route dependencies and catalog coverage; pathless guards use source inspection."""
    app = create_app()
    app.include_router(investigations.router)
    app.include_router(investigation_router)
    app.include_router(console_router)

    seen_routes: set[tuple[str, str]] = set()
    unguarded: list[tuple[str, str, str]] = []

    for route in app.routes:
        if not isinstance(route, (APIRoute, APIWebSocketRoute)):
            continue
        methods = (
            sorted(route.methods - {"HEAD", "OPTIONS"})
            if isinstance(route, APIRoute)
            else ["WS"]
        )
        guards = _route_authz_guards(route)
        if isinstance(route, APIRoute) and any(
            param in route.path
            for param in ("{project_id}", "{projectId}", "{task_id}")
        ):
            assert _actual_project_checker(route), (
                f"No executing project dependency on {route.path}"
            )
        ep_name = f"{route.endpoint.__module__}.{route.endpoint.__name__}"
        for method in methods:
            key = (method, route.path)
            seen_routes.add(key)
            if key in NON_PROJECT_ROUTES:
                continue
            if not guards:
                unguarded.append((method, route.path, ep_name))

    assert not unguarded, f"Unguarded project/task/WS routes detected: {unguarded}"

    # Allowlist hygiene: no project-ID or task-ID parametrized route may be allowlisted.
    forbidden_params = ("{project_id}", "{projectId}", "{task_id}")
    bad_allowlisted = [
        entry
        for entry in NON_PROJECT_ROUTES
        if any(p in entry[1] for p in forbidden_params)
    ]
    assert not bad_allowlisted, (
        f"Project/task routes in NON_PROJECT_ROUTES: {bad_allowlisted}"
    )

    stale_allowlist = NON_PROJECT_ROUTES - seen_routes
    assert not stale_allowlist, (
        f"Stale NON_PROJECT_ROUTES entries: {sorted(stale_allowlist)}"
    )

    # Remote MCP tools: every tool must be scope-mapped and authorized in dispatch_tool_call.
    remote_defs = mcp_remote_tools.get_tool_definitions()
    assert len(remote_defs) >= 12
    for tool_def in remote_defs:
        tname = tool_def["name"]
        assert tname in mcp_remote_tools._SCOPE_FOR_TOOL, f"Unscoped MCP tool: {tname}"


@pytest.mark.asyncio
async def test_mcp_dispatchers_call_project_guards(monkeypatch):
    denied = AsyncMock(side_effect=mcp_remote_tools.MCPAuthError("denied"))
    loopback = AsyncMock()
    monkeypatch.setattr(mcp_remote_tools, "_authorize_call", denied)
    monkeypatch.setattr(mcp_remote_tools, "_call_internal", loopback)
    key = mcp_remote_tools.AuthenticatedKey(
        key_id="test",
        user_id="u",
        org_id="org",
        scopes=frozenset({"mcp:read", "mcp:write", "task:merge"}),
    )
    for tool in mcp_remote_tools.get_tool_definitions():
        before = denied.await_count
        result = await mcp_remote_tools.dispatch_tool_call(
            tool["name"], {"task_id": "p:001", "project_id": "p"}, key
        )
        assert result.get("isError") is True
        assert denied.await_count == before + 1
        assert loopback.await_count == 0

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr(copilot_mcp.engine, "async_session_factory", Session)
    monkeypatch.setattr(copilot_mcp, "project_id_for_path", lambda _path: "p")
    monkeypatch.setattr(
        copilot_mcp,
        "_resolve_task",
        AsyncMock(return_value=(Path("/repo/.aifactory/specs/001"), {})),
    )
    project_guard = AsyncMock(side_effect=HTTPException(404, "Project not found"))
    monkeypatch.setattr(copilot_mcp, "require_project_role", project_guard)
    for tool in copilot_mcp.MCP_TOOLS:
        before = project_guard.await_count
        with pytest.raises(HTTPException):
            await copilot_mcp._dispatch_tool(tool["name"], {}, None, "001", {"id": "u"})
        assert project_guard.await_count == before + 1


def test_env_route_authorizes_path_project_before_query(monkeypatch):
    calls = []

    async def authorize(_user, project_id, _role, _db):
        calls.append(project_id)
        if project_id != "owned":
            raise HTTPException(status_code=404, detail="Project not found")

    async def db():
        yield object()

    monkeypatch.setattr(project_authz, "require_project_role", authorize)
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    app = FastAPI()
    app.include_router(context.project_router, prefix="/api/projects/{projectId}")
    app.dependency_overrides[get_db] = db

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.user = {"id": "caller"}
        return await call_next(request)

    response = TestClient(app).get("/api/projects/hidden/env?project_id=owned")
    assert response.status_code == 404
    assert calls == ["hidden"]


@pytest.fixture
def authz_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Seeded SQLite session factory with two orgs and two teams in org-1."""
    monkeypatch.setattr(project_authz, "_auth_disabled", lambda: False)
    monkeypatch.setattr(project_authz, "_audit_authz_denial", AsyncMock())
    monkeypatch.setattr(
        "server.project_registry.aload_projects",
        AsyncMock(
            return_value={
                "p1": {"org_id": "org-1", "team_id": "team-1", "name": "p1"},
                "p_team2": {
                    "org_id": "org-1",
                    "team_id": "team-2",
                    "name": "p_team2",
                },
                "p_org2": {
                    "org_id": "org-2",
                    "team_id": "team-org2",
                    "name": "p_org2",
                },
            }
        ),
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'matrix.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_factory() as db:
            db.add_all(
                [
                    User(id="u-viewer", email="v@x.test", password_hash=_DUMMY_HASH),
                    User(id="u-member", email="m@x.test", password_hash=_DUMMY_HASH),
                    User(id="u-admin", email="a@x.test", password_hash=_DUMMY_HASH),
                    User(id="u-owner", email="o@x.test", password_hash=_DUMMY_HASH),
                    User(id="u-org2", email="o2@x.test", password_hash=_DUMMY_HASH),
                    Organization(
                        id="org-1", name="Org 1", slug="org-1", owner_id="u-owner"
                    ),
                    Organization(
                        id="org-2", name="Org 2", slug="org-2", owner_id="u-org2"
                    ),
                    Team(
                        id="team-1",
                        org_id="org-1",
                        name="Default",
                        slug="default",
                        is_default=True,
                    ),
                    Team(
                        id="team-2",
                        org_id="org-1",
                        name="Platform",
                        slug="platform",
                        is_default=False,
                    ),
                    Team(
                        id="team-org2",
                        org_id="org-2",
                        name="Default",
                        slug="default",
                        is_default=True,
                    ),
                    OrgMember(org_id="org-1", user_id="u-viewer", role="viewer"),
                    OrgMember(org_id="org-1", user_id="u-member", role="member"),
                    OrgMember(org_id="org-1", user_id="u-admin", role="admin"),
                    OrgMember(org_id="org-1", user_id="u-owner", role="owner"),
                    OrgMember(org_id="org-2", user_id="u-org2", role="owner"),
                    TeamMembership(team_id="team-1", user_id="u-viewer", role="viewer"),
                    TeamMembership(team_id="team-1", user_id="u-member", role="member"),
                    TeamMembership(team_id="team-1", user_id="u-admin", role="admin"),
                    TeamMembership(team_id="team-1", user_id="u-owner", role="owner"),
                    TeamMembership(team_id="team-2", user_id="u-admin", role="admin"),
                    TeamMembership(team_id="team-org2", user_id="u-org2", role="owner"),
                ]
            )
            await db.commit()

    asyncio.run(_seed())
    try:
        yield session_factory
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("principal", "project_id"),
    [
        ({"id": "u-owner"}, "unknown-project"),
        ({"id": "default", "is_legacy_shared_token": True}, "unknown-project"),
        ({"id": "u-owner"}, "p_org2"),  # cross-org -> 404
        ({"id": "u-member"}, "p_team2"),  # cross-team in same org -> 404
        ({"id": "u-owner", "scopes": []}, "p1"),  # empty scopes -> 404
        ({"id": "u-owner", "scopes": ["unrecognized:scope"]}, "p1"),
        ({"id": "u-owner", "org_id": "org-2", "scopes": ["api"]}, "p1"),
    ],
)
async def test_unknown_and_invisible_projects_return_404(
    authz_db: async_sessionmaker[AsyncSession],
    principal: dict[str, object],
    project_id: str,
) -> None:
    async with authz_db() as db:
        with pytest.raises(HTTPException) as exc:
            await project_authz.require_project_role(
                principal, project_id, "viewer", db
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("principal", "min_role"),
    [
        ({"id": "u-viewer"}, "member"),
        ({"id": "u-viewer"}, "admin"),
        ({"id": "u-viewer"}, "owner"),
        ({"id": "u-member"}, "admin"),
        ({"id": "u-member"}, "owner"),
        ({"id": "u-admin"}, "owner"),
        ({"id": "u-owner", "scopes": ["mcp:read"]}, "member"),
        ({"id": "u-owner", "scopes": ["project:read"]}, "admin"),
        ({"id": "u-owner", "scopes": ["project:write"]}, "admin"),
        ({"id": "u-owner", "scopes": ["api"]}, "admin"),
        ({"id": "u-owner", "scopes": ["task:merge"]}, "owner"),
    ],
)
async def test_visible_project_with_low_role_or_scope_returns_403(
    authz_db: async_sessionmaker[AsyncSession],
    principal: dict[str, object],
    min_role: str,
) -> None:
    async with authz_db() as db:
        with pytest.raises(HTTPException) as exc:
            await project_authz.require_project_role(principal, "p1", min_role, db)
        assert exc.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "team_role"),
    [
        ("u-viewer", "viewer"),
        ("u-member", "member"),
        ("u-admin", "admin"),
        ("u-owner", "owner"),
    ],
)
@pytest.mark.parametrize(
    ("scopes", "ceiling"),
    [
        (None, "owner"),
        (["mcp:read"], "viewer"),
        (["*:read"], "viewer"),
        (["project:write"], "member"),
        (["task:write"], "member"),
        (["plan:write"], "member"),
        (["mcp:write"], "member"),
        (["api"], "member"),
        (["task:merge"], "admin"),
        (["project:admin"], "admin"),
        (["*:admin"], "admin"),
        (["admin"], "admin"),
    ],
)
async def test_team_grant_intersect_scope_ceiling_matrix(
    authz_db: async_sessionmaker[AsyncSession],
    user_id: str,
    team_role: str,
    scopes: list[str] | None,
    ceiling: str,
) -> None:
    principal: dict[str, object] = {"id": user_id}
    if scopes is not None:
        principal["scopes"] = scopes
    expected = min((team_role, ceiling), key=lambda r: ROLE_LEVELS[r])

    async with authz_db() as db:
        access = await project_authz.require_project_role(principal, "p1", expected, db)
        assert access.role == expected

        higher_roles = [
            r for r, lvl in ROLE_LEVELS.items() if lvl > ROLE_LEVELS[expected]
        ]
        for higher in higher_roles:
            with pytest.raises(HTTPException) as exc:
                await project_authz.require_project_role(principal, "p1", higher, db)
            assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_cross_team_and_cross_org_service_key_denial(
    authz_db: async_sessionmaker[AsyncSession],
) -> None:
    service_merge = {
        "id": None,
        "org_id": "org-1",
        "scoped_service": True,
        "is_service": False,
        "scopes": ["task:merge"],
    }
    async with authz_db() as db:
        # Allowed on org-1 default team project up to admin ceiling.
        access = await project_authz.require_project_role(
            service_merge, "p1", "admin", db
        )
        assert access.role == "admin"

        # Owner requires > admin ceiling -> 403.
        with pytest.raises(HTTPException) as owner_exc:
            await project_authz.require_project_role(service_merge, "p1", "owner", db)
        assert owner_exc.value.status_code == 403

        # An org-scoped service key is limited to its default team.
        with pytest.raises(HTTPException) as cross_team:
            await project_authz.require_project_role(
                service_merge, "p_team2", "viewer", db
            )
        assert cross_team.value.status_code == 404

        # Other org -> 404.
        with pytest.raises(HTTPException) as cross_org:
            await project_authz.require_project_role(
                service_merge, "p_org2", "viewer", db
            )
        assert cross_org.value.status_code == 404

        req = SimpleNamespace(state=SimpleNamespace(user=service_merge))
        assert await project_authz.accessible_project_ids(req, db, "viewer") == {"p1"}


@pytest.mark.asyncio
async def test_legacy_shared_token_full_access_gate(
    authz_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_principal = {
        "id": "default",
        "role": "user",
        "is_service": False,
        "is_legacy_shared_token": True,
    }
    req = SimpleNamespace(state=SimpleNamespace(user=legacy_principal))

    async with authz_db() as db:
        monkeypatch.setattr(
            project_authz,
            "get_settings",
            lambda: SimpleNamespace(LEGACY_SHARED_TOKEN_FULL_ACCESS=True),
        )
        assert (
            await project_authz.require_project_role(
                legacy_principal, "p1", "owner", db
            )
        ).role == "owner"
        assert (
            await project_authz.require_project_role(
                legacy_principal, "p_team2", "owner", db
            )
        ).role == "owner"
        assert await project_authz.accessible_project_ids(req, db) == {
            "p1",
            "p_team2",
            "p_org2",
        }

        # Flip APP_LEGACY_SHARED_TOKEN_FULL_ACCESS=false: no bypass without a team grant.
        monkeypatch.setattr(
            project_authz,
            "get_settings",
            lambda: SimpleNamespace(LEGACY_SHARED_TOKEN_FULL_ACCESS=False),
        )
        with pytest.raises(HTTPException) as denied:
            await project_authz.require_project_role(
                legacy_principal, "p1", "viewer", db
            )
        assert denied.value.status_code == 404
        assert await project_authz.accessible_project_ids(req, db) == set()
