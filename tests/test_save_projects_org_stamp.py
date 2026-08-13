"""save_projects() stamps the default org on unowned project entries.

Regression guard: a programmatically-registered project (cross-factory
handoff, build dispatch, agent project_create) must be portal-visible
immediately, not only after the next startup backfill. An unowned project
(org_id is None) is admin-only / hidden by project_authz.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server import project_registry as projects_mod  # noqa: E402
from server.database.engine import DEFAULT_ORG_ID  # noqa: E402


def _point_registry_at(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        projects_mod, "get_projects_file", lambda: tmp_path / "projects.json"
    )


def test_unowned_project_gets_default_org(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)
    projects_mod.save_projects(
        {"hello": {"id": "hello", "name": "hello", "path": "/x"}}
    )
    loaded = projects_mod.load_projects()
    assert loaded["hello"]["org_id"] == DEFAULT_ORG_ID


def test_explicit_org_id_is_preserved(tmp_path, monkeypatch):
    _point_registry_at(tmp_path, monkeypatch)
    projects_mod.save_projects(
        {"hello": {"id": "hello", "name": "hello", "path": "/x", "org_id": "acme"}}
    )
    assert projects_mod.load_projects()["hello"]["org_id"] == "acme"
