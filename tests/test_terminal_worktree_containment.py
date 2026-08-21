"""``TerminalWorktreeService`` confines its project path (#1336).

The service joins a caller-supplied ``project_path`` into the paths it hands
to ``git worktree add/remove``. Three routes build it -- ``list_``,
``create_`` and ``remove_terminal_worktree`` -- each from a raw query
parameter or request body field, so the guard belongs in the constructor they
all route through rather than in each route.

These tests drive the CONSTRUCTOR, not the routes: a route-level test would
still pass if someone added a fourth caller. Each case asserts on
``InputRejectedError`` specifically, because ``client_error`` hands that type's
message back verbatim while a bare ``ValueError`` becomes a reference id.

Mutation check: deleting the ``contained_path`` call from the constructor
makes ``test_rejects_traversal_out_of_registered_root``,
``test_rejects_symlink_escape`` and ``test_rejects_unregistered_directory``
fail.
"""

import sys
from pathlib import Path

import pytest

# Same prelude as tests/test_argv_safety.py: `server` lives under
# apps/web-server, which is not on the path this suite runs with.
sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))

from server.error_ref import InputRejectedError  # noqa: E402
from server.services.terminal_worktree_service import (  # noqa: E402
    TerminalWorktreeService,
)


@pytest.fixture
def registered(monkeypatch, tmp_path):
    """Register ``tmp_path/project`` as the one project the server knows."""
    project = tmp_path / "project"
    project.mkdir()
    # Patch the REGISTRY loader, not `registered_project_roots`: the service
    # imports that name at module load, so patching it on `specpath` would not
    # be seen. `specpath` resolves `load_projects` at call time for exactly
    # this reason.
    monkeypatch.setattr(
        "server.project_registry.load_projects",
        lambda: {"p1": {"path": str(project)}},
    )
    return project


def test_accepts_a_registered_project(registered):
    service = TerminalWorktreeService(str(registered))
    root = registered.resolve()
    assert service.project_path == root
    assert service.worktrees_dir == root / ".aifactory" / "worktrees" / "terminal"


def test_rejects_traversal_out_of_registered_root(registered):
    """`<root>/../../etc` resolves outside the root, so it is refused.

    `Path` joins collapse `..` silently, which is why the containment test has
    to happen on the RESOLVED form rather than on the string.
    """
    with pytest.raises(InputRejectedError):
        TerminalWorktreeService(f"{registered}/../../etc")


def test_rejects_symlink_escape(registered, tmp_path):
    """A symlink INSIDE the root that points outside it is still outside.

    A string-prefix containment check passes this and a `resolve()`-then-test
    check does not, so this is the case that distinguishes the two.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    link = registered / "escape"
    link.symlink_to(outside)

    with pytest.raises(InputRejectedError):
        TerminalWorktreeService(str(link))


def test_rejects_unregistered_directory(registered, tmp_path):
    """An existing directory is not thereby an allowed one."""
    other = tmp_path / "not-registered"
    other.mkdir()
    with pytest.raises(InputRejectedError):
        TerminalWorktreeService(str(other))


def test_rejects_a_missing_path_inside_the_root(registered):
    """Contained but absent -> still refused, and still the typed rejection."""
    with pytest.raises(InputRejectedError):
        TerminalWorktreeService(str(registered / "nope"))
