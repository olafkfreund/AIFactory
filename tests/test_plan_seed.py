#!/usr/bin/env python3
"""
Plan-driven allowlist: seeding into the enforced profile (integration)
======================================================================

`seed_profile_with_plan_commands` must write the plan's sanitised command names
into the SAME profile file the Bash hook reads (project_dir/.aifactory/
.aifactory-security.json), so a from-scratch build can run its declared
verification. Proven end-to-end via the real `validate_command`.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from project.analyzer import seed_profile_with_plan_commands  # noqa: E402
from security.hooks import validate_command  # noqa: E402

try:
    from security.main import reset_profile_cache
except Exception:  # pragma: no cover
    def reset_profile_cache():  # type: ignore
        pass


def _write_plan(spec_dir: Path, required: list[str], verify_cmd: str | None = None) -> Path:
    spec_dir.mkdir(parents=True, exist_ok=True)
    plan = {"feature": "gw", "required_commands": required, "phases": []}
    if verify_cmd:
        plan["phases"] = [{"subtasks": [
            {"id": "s1", "verification": {"type": "command", "command": verify_cmd}}]}]
    p = spec_dir / "implementation_plan.json"
    p.write_text(json.dumps(plan))
    return p


def test_seed_writes_granted_into_enforced_profile(tmp_path):
    reset_profile_cache()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    plan_path = _write_plan(project_dir / "spec", ["uv", "pytest", "ruff"])

    granted = seed_profile_with_plan_commands(project_dir, plan_path)
    assert set(granted) == {"uv", "pytest", "ruff"}

    profile_file = project_dir / ".aifactory" / ".aifactory-security.json"
    assert profile_file.exists()
    data = json.loads(profile_file.read_text())
    assert {"uv", "pytest", "ruff"} <= set(data["custom_commands"])


def test_seeded_commands_pass_real_enforcement(tmp_path):
    reset_profile_cache()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    plan_path = _write_plan(project_dir / "spec", ["uv"], verify_cmd="ruff check .")

    seed_profile_with_plan_commands(project_dir, plan_path)
    reset_profile_cache()  # force reload from the freshly-written file

    ok_uv, _ = validate_command("uv run pytest", project_dir)
    ok_ruff, _ = validate_command("ruff check .", project_dir)
    assert ok_uv is True
    assert ok_ruff is True


def test_denylisted_plan_command_is_not_granted(tmp_path):
    reset_profile_cache()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    plan_path = _write_plan(project_dir / "spec", ["sudo", "uv"])

    granted = seed_profile_with_plan_commands(project_dir, plan_path)
    assert "sudo" not in granted
    assert "uv" in granted
    data = json.loads((project_dir / ".aifactory" / ".aifactory-security.json").read_text())
    assert "sudo" not in data["custom_commands"]


def test_non_python_toolchain_granted_without_floor_fix(tmp_path):
    # cargo is not in the python floor fix — proves the plan path stands alone.
    reset_profile_cache()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    plan_path = _write_plan(project_dir / "spec", ["cargo"])

    seed_profile_with_plan_commands(project_dir, plan_path)
    reset_profile_cache()
    ok, _ = validate_command("cargo test", project_dir)
    assert ok is True


def test_seed_is_idempotent(tmp_path):
    reset_profile_cache()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    plan_path = _write_plan(project_dir / "spec", ["uv", "pytest"])

    first = seed_profile_with_plan_commands(project_dir, plan_path)
    assert set(first) == {"uv", "pytest"}
    second = seed_profile_with_plan_commands(project_dir, plan_path)
    assert second == []  # nothing new to grant the second time


def test_missing_plan_returns_empty(tmp_path):
    reset_profile_cache()
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    assert seed_profile_with_plan_commands(project_dir, project_dir / "nope.json") == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
