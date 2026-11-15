"""Project paths select a single registered org without following symlinks."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

WEB_SERVER = Path(__file__).resolve().parents[1]
if str(WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(WEB_SERVER))

from server import project_registry  # noqa: E402


def test_org_id_for_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = tmp_path / "projects.json"
    monkeypatch.setattr(project_registry, "get_projects_file", lambda: registry)
    acme = tmp_path / "acme"
    globex = tmp_path / "globex"
    acme.mkdir()
    globex.mkdir()
    (acme / "link").symlink_to(globex, target_is_directory=True)
    registry.write_text(
        json.dumps(
            {
                "a": {"path": str(acme), "org_id": "acme"},
                "b": {"path": str(globex), "org_id": "globex"},
                "empty": {"path": "", "org_id": "globex"},
            }
        )
    )
    lookup = project_registry.org_id_for_path
    assert lookup(acme) == "acme"
    assert lookup(acme / ".aifactory" / "worktrees" / "task") == "acme"
    assert lookup(acme / "link" / "file") == "acme"
    assert lookup(tmp_path / "missing") is None
    monkeypatch.chdir(acme)
    assert lookup(None) is None
    assert lookup("") is None
    assert lookup("relative") is None

    entries = json.loads(registry.read_text())
    entries["same"] = {"path": str(acme / "nested"), "org_id": "acme"}
    registry.write_text(json.dumps(entries))
    assert lookup(acme / "nested" / "file") == "acme"
    entries["same"]["org_id"] = "globex"
    registry.write_text(json.dumps(entries))
    assert lookup(acme / "nested" / "file") is None
    entries["same"].pop("org_id")
    registry.write_text(json.dumps(entries))
    assert lookup(acme / "nested" / "file") is None

    registry.unlink()
    assert lookup(acme) is None


def test_malformed_registry_entries_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "projects.json"
    monkeypatch.setattr(project_registry, "get_projects_file", lambda: registry)
    acme = tmp_path / "acme"
    registry.write_text(
        json.dumps(
            {
                "x": "not-an-object",
                "y": {"path": 7},
                "a": {"path": str(acme), "org_id": "acme"},
            }
        )
    )
    assert project_registry.org_id_for_path(acme) == "acme"
    registry.write_text(json.dumps({"a": {"path": str(acme), "org_id": ["acme"]}}))
    assert project_registry.org_id_for_path(acme) is None
