import json
import sys
from pathlib import Path

import pytest

# Ensure local apps/backend is in path
sys.path.insert(0, str(Path(__file__).parents[1] / "apps" / "backend"))

from project.analyzer import ProjectAnalyzer
from security.profile import get_security_profile, reset_profile_cache


@pytest.fixture
def mock_project_dir(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    return project_dir


@pytest.fixture
def mock_profile_path(mock_project_dir):
    magestic_dir = mock_project_dir / ".aifactory"
    magestic_dir.mkdir(exist_ok=True)
    return magestic_dir / ".aifactory-security.json"


def create_valid_profile_json(commands, project_hash=""):
    """Helper to create a valid SecurityProfile JSON structure."""
    return json.dumps(
        {
            "base_commands": commands,
            "stack_commands": [],
            "script_commands": [],
            "custom_commands": [],
            "detected_stack": {
                "languages": [],
                "package_managers": [],
                "frameworks": [],
                "databases": [],
                "infrastructure": [],
                "cloud_providers": [],
                "code_quality_tools": [],
                "version_managers": [],
            },
            "custom_scripts": {
                "npm_scripts": [],
                "make_targets": [],
                "poetry_scripts": [],
                "cargo_aliases": [],
                "shell_scripts": [],
            },
            "project_dir": "",
            "created_at": "",
            "project_hash": project_hash,
        }
    )


def get_dir_hash(project_dir):
    return ProjectAnalyzer(project_dir).compute_project_hash()


def test_planted_file_before_first_load_is_ignored(mock_project_dir, mock_profile_path):
    reset_profile_cache()
    mock_profile_path.write_text(
        create_valid_profile_json(["unique_cmd_A"], get_dir_hash(mock_project_dir))
    )
    assert (
        "unique_cmd_A"
        not in get_security_profile(mock_project_dir).get_all_allowed_commands()
    )


def test_file_edit_after_first_load_is_ignored(mock_project_dir, mock_profile_path):
    reset_profile_cache()
    profile1 = get_security_profile(mock_project_dir)
    mock_profile_path.write_text(
        create_valid_profile_json(["unique_cmd_B"], get_dir_hash(mock_project_dir))
    )
    profile2 = get_security_profile(mock_project_dir)
    assert profile2 is profile1
    assert "unique_cmd_B" not in profile2.get_all_allowed_commands()


def test_reset_profile_cache_forces_rebuild(mock_project_dir):
    reset_profile_cache()
    profile1 = get_security_profile(mock_project_dir)
    reset_profile_cache()
    assert get_security_profile(mock_project_dir) is not profile1
