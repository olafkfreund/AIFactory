"""Regression tests for two worktree/route bugs.

1. GET /api/tasks/running was shadowed by tasks.router's catch-all
   GET /api/tasks/{task_id}, returning 400. The execution router must be
   registered before the tasks router.
2. Worktree operations (Create PR, Merge, ...) read the projects file from a
   hardcoded ~/.aifactory path via get_data_file("projects.json") instead of
   get_projects_file() (which honours PROJECTS_DATA_DIR), so they broke under
   any custom data dir with "Project not found".
"""

from pathlib import Path

ROUTES_DIR = Path(__file__).resolve().parents[1] / "server" / "routes"


def test_running_route_registered_before_catchall():
    from server.main import create_app

    app = create_app()
    paths = [getattr(r, "path", "") for r in app.routes]
    assert "/api/tasks/running" in paths, "GET /api/tasks/running route is missing"
    assert "/api/tasks/{task_id}" in paths, "tasks catch-all route is missing"
    assert paths.index("/api/tasks/running") < paths.index("/api/tasks/{task_id}"), (
        "execution.router must be registered before tasks.router, otherwise the "
        "GET /api/tasks/{task_id} catch-all shadows GET /api/tasks/running (400)."
    )


def test_worktree_routes_use_project_data_dir_not_hardcoded_path():
    for fn in ("tasks.py", "terminal.py"):
        src = (ROUTES_DIR / fn).read_text()
        assert 'get_data_file("projects.json")' not in src, (
            f"{fn} reads projects.json via the hardcoded get_data_file path; "
            "use get_projects_file() so PROJECTS_DATA_DIR is honoured."
        )
