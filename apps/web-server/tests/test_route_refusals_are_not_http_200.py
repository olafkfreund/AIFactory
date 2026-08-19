"""AIFactory#1126: a route that refuses must not answer 200.

Factory#460 fixed two handlers on the cockpit's Approve path. This is the rest
of the class: every route handler in the twelve inventoried modules that can
return a top-level ``{"success": False, ...}`` now carries ``@honest_status``,
so the refusal travels as a 409 with a byte-identical body.

Two kinds of test live here, and they cover different failures.

**The completeness test** (``test_every_refusing_handler_is_honest``) is the one
that does not rot. It re-derives the inventory from the source at test time and
fails when a handler that can return a top-level ``success: False`` is missing
the decorator. A test-per-endpoint would freeze today's list; this one fails on
the *next* handler somebody adds, which is the actual failure mode -- #1126
exists because #460 fixed the two handlers that hurt and the class grew back.

**The executed refusals** below it drive real handlers to a real refusal and
assert ``response.status_code == 409``. They are the mutation check: remove
``@honest_status`` from the handler each one names and it goes red on the
status, not merely on the body. An assertion about the body alone would pass
against the unfixed code -- that is the trap #1126 describes -- so every one of
them asserts the status FIRST and the body second.

Why 409 and not a per-site 400/404/422: see ``services/http_verdict.py``. One
status for every refusal, the precise reason stays in the body.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from server.routes import (
    changelog,
    cli_accounts,
    context,
    files,
    git,
    github,
    projects,
    settings,
    terminal,
    worktree_merge,
    worktree_tools,
)
from server.services.http_verdict import REFUSED_STATUS, honest_status
from verdict_helpers import verdict

# The twelve modules from the #1126 inventory. `pr` is reached through
# `worktree_merge`'s router in the app but is its own module here.
INVENTORY = {
    "changelog": changelog,
    "cli_accounts": cli_accounts,
    "context": context,
    "files": files,
    "git": git,
    "github": github,
    "projects": projects,
    "settings": settings,
    "terminal": terminal,
    "worktree_merge": worktree_merge,
    "worktree_tools": worktree_tools,
}

ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _is_route_decorator(node: ast.expr) -> bool:
    func = node.func if isinstance(node, ast.Call) else node
    return isinstance(func, ast.Attribute) and func.attr in ROUTE_METHODS


def _decorator_names(fn: ast.AST) -> list[str]:
    decorators = getattr(fn, "decorator_list", [])
    return [
        ast.unparse(d.func if isinstance(d, ast.Call) else d) for d in decorators
    ]


def _returns_top_level_success_false(fn: ast.AST) -> bool:
    """Does this handler have a ``return {"success": False, ...}`` of its own?

    Deliberately *top-level* only. The codebase's existing convention for
    "answered: no" -- as opposed to "refused" -- is to nest the negative inside
    a ``success: True`` envelope (``github.py``'s "gh is not installed" is the
    canonical one). ``honest_status`` keys on the outer envelope for exactly
    that reason, so this walk must too, or it would demand the decorator on
    handlers that correctly answer 200.
    """
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)):
            continue
        # `keys` and `values` are the same length by construction on an
        # `ast.Dict`, so strict= is free here and satisfies B905.
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "success"
                and isinstance(value, ast.Constant)
                and value.value is False
            ):
                return True
    return False


def _refusing_handlers(module: Any) -> list[tuple[str, bool]]:
    """``(handler_name, is_decorated)`` for every refusing route in ``module``."""
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        decorators = _decorator_names(node)
        if not any(_is_route_decorator(d) for d in node.decorator_list):
            continue
        if not _returns_top_level_success_false(node):
            continue
        found.append((node.name, "honest_status" in decorators))
    return found


@pytest.mark.parametrize("module_name", sorted(INVENTORY))
def test_every_refusing_handler_is_honest(module_name: str) -> None:
    """No handler may return a top-level refusal without `@honest_status`.

    This is the anti-regression half of #1126. It re-derives the list from the
    source every run, so a handler added tomorrow is covered without anyone
    remembering to add a test for it.
    """
    handlers = _refusing_handlers(INVENTORY[module_name])
    # Guard against a pass-shaped empty measurement: if the AST walk stops
    # matching (a decorator style changes, a module is split) this test would
    # silently gate zero handlers and still read green.
    assert handlers, (
        f"{module_name}: found no refusing route handlers at all. Either the "
        "module genuinely has none -- in which case drop it from INVENTORY -- "
        "or this walk has stopped matching and is gating nothing."
    )
    undecorated = [name for name, decorated in handlers if not decorated]
    assert not undecorated, (
        f"{module_name}: these handlers can return a top-level "
        f'`{{"success": False}}` but lack `@honest_status`, so they answer that '
        f"refusal inside an HTTP 200 (AIFactory#1126): {undecorated}"
    )


def test_the_inventory_is_not_empty() -> None:
    """The parametrisation above must gate a real number of handlers."""
    total = sum(len(_refusing_handlers(m)) for m in INVENTORY.values())
    # 78 converted here + 1 already-honest in `projects` and 1 in
    # `worktree_merge` from #460. A drop means handlers stopped being seen.
    assert total >= 80, f"only {total} refusing handlers found; expected >= 80"


# ---------------------------------------------------------------------------
# Executed refusals: drive the real handler, assert the STATUS.
#
# One or more per module across the sweep. Each is a mutation check for the
# handler it names: remove that handler's `@honest_status` and the FIRST
# assertion -- the status one -- fails. An assertion about the body alone would
# pass against the unfixed code, which is the trap #1126 describes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_files_read_of_a_missing_path_refuses_with_a_status(
    tmp_path: Path,
) -> None:
    """`files.read_file_direct` -- 'File not found', inside a registered root."""
    with patch.object(files, "registered_project_roots", return_value=[tmp_path]):
        result = await files.read_file_direct(path=str(tmp_path / "nope.txt"))
    status, payload = verdict(result)
    assert status == REFUSED_STATUS
    assert payload["error"] == "File not found"


@pytest.mark.asyncio
async def test_files_list_of_a_missing_directory_refuses_with_a_status(
    tmp_path: Path,
) -> None:
    """`files.list_directory_direct` -- 'Directory not found'."""
    with patch.object(files, "browse_roots", return_value=[tmp_path]):
        result = await files.list_directory_direct(path=str(tmp_path / "nope"))
    status, payload = verdict(result)
    assert status == REFUSED_STATUS
    assert payload["error"] == "Directory not found"


@pytest.mark.asyncio
async def test_files_discover_of_a_missing_base_keeps_its_extra_keys(
    tmp_path: Path,
) -> None:
    """`files.discover_projects` -- and `data` survives the translation.

    16 of the sweep's refusal bodies carry keys beyond `error`. The frontend's
    `apiRequest` rebuilds `{success, error}` from a non-2xx and drops the rest,
    so this asserts the server side stays byte-identical; the callers that read
    those keys were checked and none reads them off a *failure* body.
    """
    with patch.object(files, "browse_roots", return_value=[tmp_path]):
        result = await files.discover_projects(base_path=str(tmp_path / "nope"))
    status, payload = verdict(result)
    assert status == REFUSED_STATUS
    assert payload["data"] == []


@pytest.mark.asyncio
async def test_settings_ollama_probe_of_a_refused_url_has_a_status() -> None:
    """`settings.test_ollama_connection` -- an SSRF-refused base URL.

    One of the sweep's judgement calls: a connection *test* that could not
    connect is a refusal, not an answer. Every one of these sites returns an
    `error` and no result, so it takes the same 409 as the rest. See the PR
    body for the full list of calls made this way.
    """
    with patch("httpx.AsyncClient") as client:
        result = await settings.test_ollama_connection("http://169.254.169.254", "m")
    client.assert_not_called()
    status, payload = verdict(result)
    assert status == REFUSED_STATUS
    assert "link-local/metadata" in payload["error"]


@pytest.mark.asyncio
async def test_settings_api_profile_probe_of_a_private_host_has_a_status() -> None:
    """`settings.test_api_connection` -- strict posture refuses a private host."""
    request = settings.TestConnectionRequest(
        baseUrl="http://127.0.0.1:9999", apiKey="sk-secret"
    )
    with patch.object(settings, "build_no_redirect_opener") as opener:
        result = await settings.test_api_connection(request)
    opener.assert_not_called()
    status, payload = verdict(result)
    assert status == REFUSED_STATUS
    assert "non-public" in payload["error"]


@pytest.mark.asyncio
async def test_git_pull_ollama_model_refusal_has_a_status() -> None:
    """`git.pull_ollama_model` -- the metadata address is refused."""
    with patch.object(git, "build_no_redirect_opener") as opener:
        result = await git.pull_ollama_model(
            git.PullModelRequest(modelName="x", baseUrl="http://169.254.169.254")
        )
    opener.assert_not_called()
    status, payload = verdict(result)
    assert status == REFUSED_STATUS
    assert "link-local/metadata" in payload["error"]


@pytest.mark.asyncio
async def test_changelog_commits_preview_rejects_a_bad_ref_with_a_status() -> None:
    """`changelog.get_commits_preview` -- `--output=` is an arbitrary file write."""
    with patch(
        "server.project_registry.load_projects",
        return_value={"p1": {"path": "/nonexistent/project"}},
    ):
        result = await changelog.get_commits_preview(
            projectId="p1",
            request=changelog.CommitsPreviewRequest(
                mode="branch-diff",
                options={"baseBranch": "--output=pwned", "compareBranch": "HEAD"},
            ),
        )
    status, payload = verdict(result)
    assert status == REFUSED_STATUS
    assert payload["error"] == "Invalid baseBranch: must be a plain git ref"


@pytest.mark.asyncio
async def test_worktree_tools_open_ide_refusal_has_a_status() -> None:
    """`worktree_tools.open_worktree_in_ide` on a path outside every root."""
    with patch.object(worktree_tools, "registered_project_roots", return_value=[]):
        result = await worktree_tools.open_worktree_in_ide(
            worktree_tools.OpenInIDERequest(worktreePath="/nonexistent/wt", ide="code")
        )
    status, payload = verdict(result)
    assert status == REFUSED_STATUS
    assert payload["success"] is False


@pytest.mark.asyncio
async def test_github_fork_info_refusal_has_a_status() -> None:
    """`github.get_fork_info` -- `gh` declines, so the handler refuses."""
    with patch.object(
        github,
        "run_gh_command",
        return_value={"success": False, "error": "not a git repository"},
    ):
        result = await github.get_fork_info(project_path="/nonexistent/project")
    status, payload = verdict(result)
    assert status == REFUSED_STATUS
    assert payload["error"] == "not a git repository"


@pytest.mark.asyncio
async def test_context_project_env_refusal_has_a_status() -> None:
    """`context.get_project_env` -- an unknown project id."""
    with patch("server.project_registry.load_projects", return_value={}):
        result = await context.get_project_env(projectId="nope")
    status, payload = verdict(result)
    assert status == REFUSED_STATUS
    assert payload["error"] == "Project nope not found"


@pytest.mark.asyncio
async def test_terminal_remove_worktree_refusal_has_a_status() -> None:
    """`terminal.remove_terminal_worktree`.

    Note this handler's refusal is `{"success": success}` with a *variable* --
    a site no source scan finds, and one `honest_status` still catches because
    it reads the value at runtime rather than the literal in the source.
    """
    service = patch.object(terminal, "TerminalWorktreeService")
    with service as cls:
        cls.return_value.remove_worktree.return_value = False
        result = await terminal.remove_terminal_worktree(
            name="wt", project="/nonexistent/project", deleteBranch=False
        )
    status, payload = verdict(result)
    assert status == REFUSED_STATUS
    assert payload["success"] is False


@pytest.mark.asyncio
async def test_cli_accounts_install_refusal_has_a_status() -> None:
    """`cli_accounts.install_or_update_cli` -- npm is not available.

    Sync handler, so `await`ing it here would fail if `honest_status` had
    wrapped it in a coroutine. See `test_a_sync_handler_stays_sync`.
    """
    with patch.object(cli_accounts.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess([], returncode=1, stdout="", stderr="")
        result = cli_accounts.install_or_update_cli(cli="codex")
    status, payload = verdict(result)
    assert status == REFUSED_STATUS
    assert payload["error"] == "Node.js/npm not found. Please install Node.js first."


def test_a_sync_handler_stays_sync() -> None:
    """FastAPI threadpools a `def` endpoint and loops an `async def` one.

    The two sync handlers in this sweep shell out to a package-manager install
    that runs for tens of seconds. If `honest_status` returned an async wrapper
    for them, FastAPI would run that install on the event loop and block every
    other request for its duration. This is the guard on that.
    """
    for module, name in (
        (cli_accounts, "install_or_update_cli"),
        (github, "install_github_cli"),
    ):
        handler = getattr(module, name)
        assert not inspect.iscoroutinefunction(handler), (
            f"{module.__name__}.{name} is a sync endpoint; `honest_status` must "
            "not turn it into a coroutine or FastAPI will run it on the loop"
        )
        # And it is genuinely decorated, not merely sync-and-unwrapped.
        assert hasattr(handler, "__wrapped__"), f"{name} lost @honest_status"


def test_a_sync_handler_still_gets_the_refusal_status() -> None:
    """The sync branch must translate, not just pass through."""

    @honest_status
    def refusing() -> dict[str, Any]:
        return {"success": False, "error": "nope"}

    status, payload = verdict(refusing())
    assert status == REFUSED_STATUS
    assert payload["error"] == "nope"


@pytest.mark.asyncio
async def test_worktree_merge_preview_refusal_has_a_status() -> None:
    """`worktree_merge.get_worktree_merge_preview` -- an unparseable task id.

    #460 fixed `merge_worktree` in this module and left the other seven
    handlers answering 200. This is one of those seven.
    """
    result = await worktree_merge.get_worktree_merge_preview("nope", _access={})
    status, payload = verdict(result)
    assert status == REFUSED_STATUS
    assert payload["success"] is False


@pytest.mark.asyncio
async def test_a_successful_handler_still_answers_200(tmp_path: Path) -> None:
    """The other half: the decorator must not turn a success into a refusal.

    Without this, a `honest_status` that answered 409 unconditionally would
    pass every test above.

    `list_directory_direct` returns raw data on the happy path -- no `success`
    key at all -- which also covers the third branch of the decorator: a body
    with no verdict in it must be left alone rather than treated as a refusal.
    """
    (tmp_path / "a.txt").write_text("x")
    with patch.object(files, "browse_roots", return_value=[tmp_path]):
        result = await files.list_directory_direct(path=str(tmp_path))
    status, payload = verdict(result)
    assert status == 200
    assert [e["name"] for e in payload["entries"]] == ["a.txt"]


def test_the_body_is_byte_identical_across_the_translation() -> None:
    """#1126's compatibility promise: consumers reading `success` are unaffected.

    `apps/frontend-web/src/lib/api-client.ts` does not throw on `!response.ok`;
    it reads `error` off the body and returns `{success: false, error}` either
    way. That equivalence only holds while the body survives the translation.
    """
    refusal = {"success": False, "error": "nope", "data": None, "extra": [1, 2]}

    async def handler() -> dict[str, Any]:
        return refusal

    response = asyncio.run(honest_status(handler)())
    assert json.loads(bytes(response.body)) == refusal
    assert response.status_code == REFUSED_STATUS
