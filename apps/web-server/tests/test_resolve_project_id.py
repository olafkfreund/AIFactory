"""#321: resolve a UUID / name / owner-repo slug to the canonical project id.

External callers (PFactory's plan->AIFactory handoff) address a project by the
repo they planned against, not AIFactory's internal UUID. resolve_project_id
must map slug/name -> id so the handoff reaches the registered project.
"""

from __future__ import annotations

import sys
from pathlib import Path

_WEB_SERVER = Path(__file__).resolve().parents[1]
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))

# The registry helpers live in the leaf module server/project_store.py (#1302).
# Patch them at their OWNER, not at routes.projects (which only re-exports
# them) — otherwise the patch misses every caller that imports the owner.
from server import project_store as projects_mod  # noqa: E402

_FIXTURE = {
    "5d78d4b9-uuid": {
        "name": "aifactory-demo",
        "path": "/home/nonroot/.aifactory/workspaces/olafkfreund-aifactory-demo",
    },
    "de08a1a1-uuid": {
        "name": "tfactory",
        "path": "/home/nonroot/.aifactory/workspaces/olafkfreund-TFactory",
    },
}


def _patch(monkeypatch):
    monkeypatch.setattr(projects_mod, "load_projects", lambda: _FIXTURE)


def test_uuid_passthrough(monkeypatch):
    _patch(monkeypatch)
    assert projects_mod.resolve_project_id("5d78d4b9-uuid") == "5d78d4b9-uuid"


def test_owner_repo_slug_matches_path(monkeypatch):
    # the #321 failure shape: PFactory sends owner/repo, not the UUID
    _patch(monkeypatch)
    assert (
        projects_mod.resolve_project_id("olafkfreund/aifactory-demo") == "5d78d4b9-uuid"
    )


def test_bare_repo_name_matches(monkeypatch):
    _patch(monkeypatch)
    assert projects_mod.resolve_project_id("aifactory-demo") == "5d78d4b9-uuid"


def test_unknown_returns_none(monkeypatch):
    _patch(monkeypatch)
    assert projects_mod.resolve_project_id("olafkfreund/nope") is None


def test_no_cross_repo_false_match(monkeypatch):
    # aifactory-demo must not resolve to the tfactory project
    _patch(monkeypatch)
    assert projects_mod.resolve_project_id("olafkfreund/TFactory") == "de08a1a1-uuid"
