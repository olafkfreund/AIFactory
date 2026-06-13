"""Seam regression for the worktree external-tool sub-router extraction (#556).

The "open in IDE / open in terminal / detect tools" handlers were lifted out of
the routes/tasks.py god-file into routes/worktree_tools.py and mounted back onto
the tasks router via ``include_router``. This must be a pure no-op for the public
HTTP surface: same paths, same methods, mounted on the same ``tasks.router``.

These assertions introspect ``tasks.router`` directly (no full-app import), so
they stay runnable even when unrelated routers cannot be imported under a given
FastAPI version.
"""

# The three endpoints owned by the extracted sub-router, with their methods.
WORKTREE_TOOL_ENDPOINTS = {
    ("/worktree/open-in-ide", "POST"),
    ("/worktree/open-in-terminal", "POST"),
    ("/worktree/detect-tools", "POST"),
}


def _route_set(router):
    out = set()
    for r in router.routes:
        for method in getattr(r, "methods", set()) or set():
            out.add((r.path, method))
    return out


def test_worktree_tool_routes_still_mounted_on_tasks_router():
    from server.routes import tasks

    routes = _route_set(tasks.router)
    missing = WORKTREE_TOOL_ENDPOINTS - routes
    assert not missing, (
        f"worktree-tool endpoints missing from tasks.router after extraction: {missing}"
    )


def test_worktree_tool_handlers_live_in_worktree_tools_module():
    from server.routes import tasks

    for route in tasks.router.routes:
        if route.path in {p for p, _ in WORKTREE_TOOL_ENDPOINTS}:
            assert route.endpoint.__module__ == "server.routes.worktree_tools", (
                f"{route.path} handler should be defined in worktree_tools, "
                f"got {route.endpoint.__module__}"
            )


def test_sub_router_owns_exactly_the_three_endpoints():
    from server.routes import worktree_tools

    assert _route_set(worktree_tools.router) == WORKTREE_TOOL_ENDPOINTS


def test_backward_compatible_reexports_resolve():
    """Names that historically lived in routes.tasks stay importable from it."""
    from server.routes.tasks import (  # noqa: F401
        OpenInIDERequest,
        OpenInTerminalRequest,
        detect_worktree_tools,
        get_ide_command,
        get_terminal_command,
        open_worktree_in_ide,
        open_worktree_in_terminal,
    )

    # Helpers are pure and behavior-preserving.
    assert get_ide_command("vscode", "/tmp/x") == ["code", "/tmp/x"]
    assert get_ide_command("unknown-ide", "/tmp/x") == ["code", "/tmp/x"]
