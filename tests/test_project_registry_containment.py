"""The project registry is a trust boundary (#1278).

Every service in this codebase reads a path back out of ``projects.json`` and
uses it unchecked::

    projects = load_projects()
    if project_id not in projects:
        raise ValueError(...)
    project_path = Path(projects[project_id]["path"])

That is only safe if the registry cannot be made to hold an arbitrary host
path. Before this change it could: ``POST /api/projects`` took a free-form
``path``, ``mkdir -p``'d it if absent, and registered it. These tests pin the
constraint that makes the pattern above true, and — just as importantly — pin
that it did not strand the projects an operator already had.

The two halves, and why both are here:

* **Write side.** A path outside every browsable root is refused at the four
  routes that can put one into the registry or reach the filesystem with one.
* **Read side (migration).** Entries already in ``projects.json`` are NOT
  re-validated. Validate-on-write-only is the deliberate choice: an operator
  whose projects live on ``/mnt/data`` must not find them gone after an
  upgrade. Those projects stay registered, stay readable, and — because a
  registered project is itself a root — stay fully usable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.routes.git import (  # noqa: E402
    InitGitRequest,
    check_git_status,
    initialize_git,
)
from server.routes.projects import (  # noqa: E402
    ProjectCreate,
    ScanProjectsRequest,
    add_project,
    scan_for_projects,
    update_project,
)
from server.routes.terminal import (  # noqa: E402
    CreateTerminalRequest,
    clear_terminal_sessions,
    create_terminal,
)
from server.routes.worktree_tools import resolve_launch_dir  # noqa: E402
from server.specpath import browse_roots, registered_project_roots  # noqa: E402

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A realistic pre-change ``projects.json`` on disk, parsed for real.

    TWO entries, not one, and both **outside** ``$HOME`` — that is the whole
    point of the fixture. The code under test iterates the registry to build
    its root list, and a one-entry fixture cannot tell "iterates the registry"
    apart from "uses the first entry". Two entries on different parents also
    catch a root list that accidentally collapses to a common prefix.

    ``get_projects_file`` is redirected rather than ``load_projects`` mocked,
    so the JSON is really read and really written back.
    """
    legacy_a = tmp_path / "mnt" / "data" / "code" / "alpha"
    legacy_b = tmp_path / "srv" / "repos" / "beta"
    for p in (legacy_a, legacy_b):
        (p / "src").mkdir(parents=True)
        (p / "src" / "main.py").write_text("print('hi')\n")

    projects_file = tmp_path / "projects.json"
    projects_file.write_text(
        json.dumps(
            {
                "proj-alpha": {
                    "path": str(legacy_a),
                    "name": "alpha",
                    "org_id": "default",
                    "created_at": "2026-01-02T03:04:05",
                    "updated_at": "2026-01-02T03:04:05",
                },
                "proj-beta": {
                    "path": str(legacy_b),
                    "name": "beta",
                    "org_id": "default",
                    "created_at": "2026-02-03T04:05:06",
                    "updated_at": "2026-02-03T04:05:06",
                },
            },
            indent=2,
        )
    )
    monkeypatch.setattr(
        "server.project_registry.get_projects_file", lambda: projects_file
    )
    return type(
        "Registry",
        (),
        {"file": projects_file, "alpha": legacy_a, "beta": legacy_b},
    )


@pytest.fixture
def outside(tmp_path):
    """A directory no root covers — the stand-in for /etc, /root, ~/.ssh."""
    d = tmp_path / "elsewhere" / "not-a-project"
    d.mkdir(parents=True)
    return d


# --------------------------------------------------------------------------
# Migration: the projects an operator already has keep working
# --------------------------------------------------------------------------


def test_existing_entries_outside_home_are_not_revalidated(registry):
    """The regression this change could plausibly have caused.

    Both fixture projects sit outside ``$HOME`` and outside
    ``APP_FILE_BROWSE_ROOTS``, i.e. they could NOT be registered today. They
    were registered yesterday, so they must still be there.
    """
    roots = registered_project_roots()

    assert registry.alpha.resolve() in roots
    assert registry.beta.resolve() in roots


def test_existing_entries_are_their_own_roots(registry):
    """Both legacy projects are usable, not merely listed.

    ``registered_project_roots`` feeding ``browse_roots`` is what keeps a
    legacy project's own subtree reachable: a git status inside it must still
    answer rather than 403.
    """
    for project in (registry.alpha, registry.beta):
        assert project.resolve() in browse_roots()


async def test_git_status_inside_a_legacy_project_still_works(registry):
    res = await check_git_status(path=str(registry.beta / "src"))

    assert res["success"] is True
    assert res["data"]["isGitRepo"] is False  # no .git — but it answered


async def test_a_sibling_of_a_legacy_project_is_still_refused(registry, tmp_path):
    """The documented edge of validate-on-write-only.

    ``/srv/repos/beta`` being registered does not make ``/srv/repos`` browsable.
    An operator who wants to add ``gamma`` next to it sets
    ``APP_FILE_BROWSE_ROOTS`` — see the test below. Asserted so the trade is a
    decision on the record rather than a surprise in a bug report.
    """
    sibling = registry.beta.parent / "gamma"
    sibling.mkdir()

    with pytest.raises(HTTPException) as exc:
        await add_project(ProjectCreate(path=str(sibling)), None, None)
    assert exc.value.status_code == 403


async def test_browse_roots_env_is_the_operator_escape_hatch(
    registry, monkeypatch, tmp_path
):
    monkeypatch.setenv("APP_FILE_BROWSE_ROOTS", str(registry.beta.parent))
    sibling = registry.beta.parent / "gamma"
    sibling.mkdir()

    res = await add_project(ProjectCreate(path=str(sibling)), None, None)

    assert res["path"] == str(sibling.resolve())
    saved = json.loads(registry.file.read_text())
    assert len(saved) == 3, "the two pre-existing entries must survive the write"
    assert {e["path"] for e in saved.values()} == {
        str(registry.alpha),
        str(registry.beta),
        str(sibling.resolve()),
    }


# --------------------------------------------------------------------------
# Write side: what can enter the registry
# --------------------------------------------------------------------------


async def test_add_project_rejects_path_outside_browse_roots(registry, outside):
    """THE test. Remove the containment from ``add_project`` and this goes red.

    Asserts the traversal path is REJECTED — a 403 and an unchanged registry —
    not merely that something raised.
    """
    with pytest.raises(HTTPException) as exc:
        await add_project(ProjectCreate(path=str(outside)), None, None)

    assert exc.value.status_code == 403
    assert json.loads(registry.file.read_text()).keys() == {"proj-alpha", "proj-beta"}


async def test_add_project_rejects_traversal_out_of_a_registered_project(
    registry, outside
):
    """``..`` out of a legitimate root resolves before it is checked."""
    escape = registry.alpha / ".." / ".." / ".." / "elsewhere" / "not-a-project"

    with pytest.raises(HTTPException) as exc:
        await add_project(ProjectCreate(path=str(escape)), None, None)

    assert exc.value.status_code == 403


async def test_add_project_does_not_create_the_rejected_directory(registry, tmp_path):
    """The confinement runs BEFORE the mkdir, not after.

    ``add_project`` creates the directory when it is absent. Confining after
    that would leave the attacker's directory on disk and merely decline to
    register it — a rejected request must leave no trace.
    """
    victim = tmp_path / "elsewhere" / "created-by-attacker"

    with pytest.raises(HTTPException) as exc:
        await add_project(ProjectCreate(path=str(victim)), None, None)

    assert exc.value.status_code == 403
    assert not victim.exists(), "rejected add must not have mkdir'd the target"


async def test_add_project_under_home_still_works(registry, monkeypatch, tmp_path):
    """The first-project case: not registered yet, so the browse tier is what
    makes adding one possible at all."""
    fake_home = tmp_path / "home" / "op"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    new = fake_home / "projects" / "fresh"

    res = await add_project(ProjectCreate(path=str(new)), None, None)

    assert res["path"] == str(new.resolve())
    assert new.is_dir(), "an accepted add still creates the directory"


async def test_add_project_still_expands_tilde(registry, monkeypatch, tmp_path):
    """``~/code/thing`` has always worked here and must keep working.

    The expansion moved INSIDE the containment helper rather than being done by
    the caller: ``Path(request_value).expanduser()`` on the caller's side is a
    path expression built from the raw request value, one line before the check
    that constrains it. The file-browser routes still refuse to expand ``~``
    (see ``test_file_containment``) — the flag is per-caller for that reason.
    """
    fake_home = tmp_path / "home" / "op"
    (fake_home / "code" / "thing").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))

    res = await add_project(ProjectCreate(path="~/code/thing"), None, None)

    assert res["path"] == str((fake_home / "code" / "thing").resolve())


# --------------------------------------------------------------------------
# Write side: clone mode (#1313)
# --------------------------------------------------------------------------
#
# The third `path` writer. Local mode and `update_project` were confined by
# #1306; the clone branch registered `clone_or_update(...).resolve()` raw, and
# the slug it builds that path from let `..` through.


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A workspace root plus a git that clones nothing.

    The point of these tests is which path reaches the registry, not whether
    git works, so `_run_git` just materialises the target directory. The root
    is a tmp_path CHILD, so its parent (the stand-in for `~/.aifactory`, where
    projects.json and the credential store live) is a real, escapable
    directory rather than something the test invented.
    """
    root = tmp_path / "dot-aifactory" / "workspaces"
    root.mkdir(parents=True)
    monkeypatch.setenv("PROJECT_WORKSPACE_ROOT", str(root))

    async def fake_run_git(args, *, cwd, timeout, **_kwargs):
        if args and args[0] == "clone":
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return ""

    monkeypatch.setattr(
        "server.services.project_workspace_service._run_git", fake_run_git
    )
    return root


async def test_add_project_clone_mode_registers_a_normal_repo(registry, workspace):
    """A fix that breaks legitimate cloning is worse than the bug."""
    res = await add_project(
        ProjectCreate(gitUrl="https://example.test/acme/widget.git"), None, None
    )

    assert res["path"] == str(workspace / "acme-widget")
    saved = json.loads(registry.file.read_text())
    assert str(workspace / "acme-widget") in {e["path"] for e in saved.values()}


async def test_add_project_clone_mode_refuses_a_traversal_git_url(registry, workspace):
    """THE slug test. Restore ``.`` to the slug's character class and this goes
    red: the URL path ``..`` survives, and the clone — and the registry entry —
    become ``workspace_root().parent``, the directory holding projects.json.

    Asserts WHERE the path lands, not merely that something raised: the parent
    directory must not be registered and must not have been cloned into.
    """
    with pytest.raises(HTTPException) as exc:
        await add_project(ProjectCreate(gitUrl="https://example.test/.."), None, None)

    assert exc.value.status_code == 400
    saved = json.loads(registry.file.read_text())
    assert saved.keys() == {"proj-alpha", "proj-beta"}
    assert str(workspace.parent) not in {e["path"] for e in saved.values()}
    assert not (workspace.parent / ".git").exists(), "nothing was cloned over it"


async def test_add_project_clone_mode_confines_whatever_the_clone_returns(
    registry, workspace, outside, monkeypatch
):
    """THE write test. Delete the ``within_roots`` call in the clone branch and
    this goes red.

    The registry invariant cannot rest on `slug_from_git_url` staying correct:
    `clone_or_update` also takes a `slug` override and follows the remote's
    redirects. So the route confines the path it got back, whatever produced
    it — here a clone service that hands back a directory outside the
    workspace root entirely.
    """

    async def clone_somewhere_else(**kwargs):
        return outside

    monkeypatch.setattr(
        "server.services.project_workspace_service.clone_or_update",
        clone_somewhere_else,
    )

    with pytest.raises(HTTPException) as exc:
        await add_project(
            ProjectCreate(gitUrl="https://example.test/acme/widget.git"), None, None
        )

    assert exc.value.status_code == 403
    saved = json.loads(registry.file.read_text())
    assert saved.keys() == {"proj-alpha", "proj-beta"}
    assert str(outside) not in {e["path"] for e in saved.values()}


async def test_update_project_cannot_repoint_outside(registry, outside):
    """A PUT is the same trust decision as a POST."""
    with pytest.raises(HTTPException) as exc:
        await update_project("proj-alpha", ProjectCreate(path=str(outside)))

    assert exc.value.status_code == 403
    saved = json.loads(registry.file.read_text())
    assert saved["proj-alpha"]["path"] == str(registry.alpha)


async def test_scan_refuses_to_walk_outside(registry, outside):
    with pytest.raises(HTTPException) as exc:
        await scan_for_projects(ScanProjectsRequest(basePath=str(outside), maxDepth=3))

    assert exc.value.status_code == 403


# --------------------------------------------------------------------------
# The other free-form path sinks named in #1278
# --------------------------------------------------------------------------


async def test_git_init_outside_is_refused_and_writes_nothing(registry, outside):
    with pytest.raises(HTTPException) as exc:
        await initialize_git(InitGitRequest(path=str(outside)))

    assert exc.value.status_code == 403
    assert not (outside / ".gitignore").exists()
    assert not (outside / ".git").exists()


async def test_git_status_outside_is_refused(registry, outside):
    with pytest.raises(HTTPException) as exc:
        await check_git_status(path=str(outside))

    assert exc.value.status_code == 403


async def test_terminal_cwd_outside_is_refused(registry, outside):
    """A free-form shell cwd is worth more than a file read."""
    with pytest.raises(HTTPException) as exc:
        await create_terminal(CreateTerminalRequest(cwd=str(outside)))

    assert exc.value.status_code == 403


async def test_terminal_cwd_in_a_legacy_project_is_allowed_through_containment(
    registry, monkeypatch
):
    """Containment must not be the thing that breaks a legacy project's
    terminal. Stops at the PTY spawn — the containment check is what is under
    test, and a real shell is not."""
    spawned = {}

    def fake_create_session(**kwargs):
        spawned.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(
        "server.routes.terminal.get_pty_manager",
        lambda: type("M", (), {"create_session": staticmethod(fake_create_session)})(),
    )

    with pytest.raises(HTTPException) as exc:
        await create_terminal(CreateTerminalRequest(cwd=str(registry.alpha)))

    assert exc.value.status_code == 503, "got past containment, failed at the PTY"
    assert spawned["cwd"] == str(registry.alpha.resolve())


async def test_clear_sessions_outside_a_registered_project_is_refused(
    registry, outside
):
    """Found while measuring #1278, not listed in it — and this one deletes.

    ``DELETE /api/terminals/sessions?project=`` unlinks every file under
    ``<project>/.aifactory/terminal-sessions``. Guarded by ``.exists()`` alone
    it would empty that directory under any path on the host.
    """
    victim = outside / ".aifactory" / "terminal-sessions"
    victim.mkdir(parents=True)
    # Names that MATCH the route's `terminal_*.json` glob — a file the route
    # would really have unlinked. Two, so the assertion cannot pass by the
    # route stopping after one.
    (victim / "terminal_a.json").write_text("{}")
    (victim / "terminal_b.json").write_text("{}")

    with pytest.raises(HTTPException) as exc:
        await clear_terminal_sessions(project=str(outside))

    assert exc.value.status_code == 403
    assert (victim / "terminal_a.json").exists(), "a refused clear must delete nothing"
    assert (victim / "terminal_b.json").exists()


async def test_clear_sessions_inside_a_registered_project_still_works(registry):
    sessions = registry.alpha / ".aifactory" / "terminal-sessions"
    sessions.mkdir(parents=True)
    (sessions / "terminal_one.json").write_text("{}")
    (sessions / "terminal_two.json").write_text("{}")

    res = await clear_terminal_sessions(project=str(registry.alpha))

    assert res["data"]["cleared"] == 2
    assert not (sessions / "terminal_one.json").exists()
    assert not (sessions / "terminal_two.json").exists()


def test_worktree_launch_outside_a_registered_project_is_refused(registry, outside):
    _, error = resolve_launch_dir(str(outside))

    assert error is not None
    assert error["success"] is False


def test_worktree_launch_inside_a_registered_project_is_allowed(registry):
    worktree = registry.alpha / ".aifactory" / "worktrees" / "tasks" / "t1"
    worktree.mkdir(parents=True)

    resolved, error = resolve_launch_dir(str(worktree))

    assert error is None
    assert resolved == str(worktree.resolve())
