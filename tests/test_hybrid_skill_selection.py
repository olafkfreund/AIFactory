#!/usr/bin/env python3
"""
Tests for hybrid skill auto-selection (#394)
============================================

Propose → confirm → fallback:
- suggest_selected_skills maps the matcher's suggestions to the selectedSkills
  shape the build consumes.
- create-and-run persists them as suggestedSkills when no manual skills are set.
- (_write_skill_context's selectedSkills-or-suggestedSkills fallback is covered
  by the agent_service contract; here we lock the propose + convert halves.)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "apps" / "web-server"))
sys.path.insert(0, str(_ROOT / "apps" / "backend"))

from server.services.skills_service import (  # noqa: E402
    SkillsService,
    SkillSuggestion,
    SkillSummary,
    suggestion_to_selected,
)


def _suggestion(name="clean-code", category="quality", relevance=0.9):
    return SkillSuggestion(
        skill=SkillSummary(
            id=f"{category}/{name}",
            name=name,
            category=category,
            description="d",
            source="https://example.com",
        ),
        relevance_score=relevance,
        reason="Matched: clean, code",
    )


class TestConverter:
    def test_maps_to_selected_shape(self):
        d = suggestion_to_selected(_suggestion())
        assert d["id"] == "quality/clean-code"
        assert d["name"] == "clean-code"
        assert d["category"] == "quality"
        assert d["source"] == "https://example.com"
        assert d["relevance"] == 0.9
        assert "reason" in d


class TestSuggestSelectedSkills:
    def test_returns_selected_shaped_dicts(self, tmp_path, monkeypatch):
        # Empty skills dir → valid (empty) index; monkeypatch the matcher.
        svc = SkillsService(
            skills_base_path=tmp_path / "none", cache_path=tmp_path / "c.pkl"
        )
        monkeypatch.setattr(
            svc,
            "suggest_skills",
            lambda desc, max_results=10: [
                _suggestion("backend-engineering", "eng"),
                _suggestion(),
            ],
        )
        out = svc.suggest_selected_skills("build a backend api", max_results=5)
        assert len(out) == 2
        assert {o["id"] for o in out} == {
            "eng/backend-engineering",
            "quality/clean-code",
        }
        assert all(set(o) >= {"id", "name", "category", "source"} for o in out)

    def test_empty_when_no_matches(self, tmp_path, monkeypatch):
        svc = SkillsService(
            skills_base_path=tmp_path / "none", cache_path=tmp_path / "c.pkl"
        )
        monkeypatch.setattr(svc, "suggest_skills", lambda desc, max_results=10: [])
        assert svc.suggest_selected_skills("xyz") == []


class TestCreateAndRunProposes:
    """create-and-run persists suggestedSkills (propose step)."""

    def _setup(self, tmp_path, monkeypatch):
        from server.routes import execution as exec_mod
        from server.services import skills_service as skills_mod

        project = tmp_path / "proj"
        (project / ".aifactory" / "specs").mkdir(parents=True)
        monkeypatch.setattr(
            exec_mod, "load_projects", lambda: {"p": {"path": str(project)}}
        )

        class _Stub:
            async def start_spec_creation(self, **kw):
                return None

        monkeypatch.setattr(exec_mod, "get_agent_service", lambda: _Stub())

        class _Skills:
            def suggest_selected_skills(self, desc, max_results=5):
                return [
                    {
                        "id": "quality/clean-code",
                        "name": "clean-code",
                        "category": "quality",
                        "source": None,
                    }
                ]

        monkeypatch.setattr(skills_mod, "get_skills_service", lambda: _Skills())
        return project, exec_mod

    async def test_suggested_skills_persisted(self, tmp_path, monkeypatch):
        from server.routes.execution import CreateAndRunRequest

        project, exec_mod = self._setup(tmp_path, monkeypatch)
        result = await exec_mod.create_and_run_task(
            project_id="p",
            title="Clean up the code",
            description="refactor for clarity",
            request=CreateAndRunRequest(),
        )
        spec_id = result["task_id"].split(":", 1)[1]
        meta = json.loads(
            (
                project / ".aifactory" / "specs" / spec_id / "task_metadata.json"
            ).read_text()
        )
        assert meta["suggestedSkills"][0]["id"] == "quality/clean-code"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
