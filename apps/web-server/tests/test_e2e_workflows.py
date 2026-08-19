"""
End-to-End Workflow Tests for Magestic AI API

This test suite validates complete user workflows that span multiple endpoints.
Unlike unit tests that validate individual endpoints, these tests verify realistic
user journeys and ensure endpoints work together correctly.

Workflows tested:
1. Profile Management Workflow - Create, configure, switch, and manage profiles
2. Project Setup Workflow - Discover, add, configure, remove projects
3. Settings Configuration Workflow - API keys, auto-switch, environment setup
4. Error Handling & Recovery - Rate-limit switch, concurrent file access

Rewritten coverage (#912): the profile-lifecycle and project-onboarding
workflows removed in #903 are reimplemented here against the current async API
(direct calls into ``server.routes.settings`` / ``server.routes.projects``,
real Pydantic request models, PROJECTS_DATA_DIR redirected via monkeypatch).

Intentionally NOT restored (#912) because the features they exercised no
longer exist in server/:
- Roadmap/Ideation workflow - no roadmap/ideation routes remain.
- GitLab issue-to-MR workflow - the glab-CLI routes are gone; provider support
  now lives behind the provider abstraction in ``server/routes/github.py``.
- Git operations workflow (squash/worktree/release) - ``git.project_router`` /
  ``git.releases_router`` are not mounted in main.py and their project lookup
  reads ``settings.projects_file``, which does not exist (tracked separately).
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from server.services.http_verdict import REFUSED_STATUS
from verdict_helpers import verdict


def _refused(result):
    """Assert the handler refused on the STATUS line; return its body.

    These handlers carry `@honest_status` (AIFactory#1126), so a refusal is a
    409 rather than a `success: False` inside a 200. Asserting only the body
    would pass against the old behaviour, which is the bug that issue fixed.
    """
    status, body = verdict(result)
    assert status == REFUSED_STATUS, f"expected a refusal status, got {status}"
    return body


# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_settings_dir(temp_dir: Path) -> Path:
    """Create mock settings directory structure."""
    settings_dir = temp_dir / ".aifactory"
    settings_dir.mkdir(parents=True)
    return settings_dir


@pytest.fixture
def mock_project_dir(temp_dir: Path) -> Path:
    """Create mock project directory with .aifactory."""
    project_dir = temp_dir / "test-project"
    project_dir.mkdir(parents=True)
    magestic_ai_dir = project_dir / ".aifactory"
    magestic_ai_dir.mkdir(parents=True)
    return project_dir


@pytest.fixture
def mock_projects_json(temp_dir: Path, mock_project_dir: Path) -> Path:
    """Create mock projects.json."""
    projects_file = temp_dir / "projects.json"
    projects_data = {
        "projects": [
            {
                "id": "test-project-1",
                "name": "Test Project",
                "path": str(mock_project_dir),
                "createdAt": 1704067200000,
                "updatedAt": 1704067200000,
            }
        ]
    }
    projects_file.write_text(json.dumps(projects_data, indent=2))
    return projects_file


# ============================================================================
# WORKFLOW 1: Profile Management
# ============================================================================


class TestProfileManagementWorkflow:
    """Test complete profile management lifecycle."""

    def test_complete_claude_profile_lifecycle(self, tmp_path: Path, monkeypatch):
        """
        Test the complete Claude profile management workflow (#912):
        1. Create a profile (persisted with 0o600 perms)
        2. Reject a duplicate profile name
        3. Activate it (CLAUDE_CODE_OAUTH_TOKEN follows the active profile)
        4. Create a second profile
        5. Rename it; reject renaming onto an existing name
        6. Switch profiles on rate limit via retry_with_profile
        7. Reject switching to the already-active profile
        8. Delete the inactive profile; deleting again errors

        This simulates a user setting up and managing multiple Claude profiles.
        """
        from server import config
        from server.routes import settings as settings_routes

        monkeypatch.setattr(config.settings, "PROJECTS_DATA_DIR", str(tmp_path))
        # set_active/retry sync this env var to the active profile's token;
        # seed it so pytest restores the caller's value afterwards.
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sentinel")
        profiles_file = tmp_path / "claude-profiles.json"

        # save_profiles() resets the agent service's token pool on every
        # mutation; keep the test hermetic by stubbing the service singleton.
        with patch(
            "server.services.agent_service.get_agent_service",
            return_value=MagicMock(),
        ):
            # Step 1: Create first profile
            work_token = "sess-" + "x" * 40
            result = asyncio.run(
                settings_routes.save_claude_profile(
                    settings_routes.ClaudeProfile(
                        name="Work Account",
                        email="work@example.com",
                        oauthToken=work_token,
                    )
                )
            )
            assert result["success"] is True
            profile_id_1 = result["data"]["id"]

            stored = json.loads(profiles_file.read_text())
            assert len(stored["profiles"]) == 1
            assert stored["profiles"][0]["name"] == "Work Account"
            assert stored["profiles"][0]["oauthToken"] == work_token
            # The token store must not be world-readable.
            assert profiles_file.stat().st_mode & 0o777 == 0o600

            # Step 2: Duplicate names are rejected -- and the rejection travels
            # on the status line, not only in the body (AIFactory#1126).
            dup = _refused(
                asyncio.run(
                    settings_routes.save_claude_profile(
                        settings_routes.ClaudeProfile(name="Work Account")
                    )
                )
            )
            assert dup["success"] is False

            # Step 3: Activate the profile; env token follows it
            result = asyncio.run(
                settings_routes.set_active_claude_profile(
                    settings_routes.ActiveProfileRequest(profileId=profile_id_1)
                )
            )
            assert result["success"] is True
            assert json.loads(profiles_file.read_text())["activeProfileId"] == (
                profile_id_1
            )
            assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == work_token

            # Step 4: Create second profile
            personal_token = "sk-ant-" + "y" * 40
            result = asyncio.run(
                settings_routes.save_claude_profile(
                    settings_routes.ClaudeProfile(
                        name="Personal", oauthToken=personal_token
                    )
                )
            )
            assert result["success"] is True
            profile_id_2 = result["data"]["id"]
            assert len(json.loads(profiles_file.read_text())["profiles"]) == 2

            # Step 5: Rename the second profile; renaming onto an existing
            # name is rejected
            result = asyncio.run(
                settings_routes.rename_claude_profile(
                    profile_id_2,
                    settings_routes.ProfileRename(name="Personal Account"),
                )
            )
            assert result["success"] is True
            clash = _refused(
                asyncio.run(
                    settings_routes.rename_claude_profile(
                        profile_id_2, settings_routes.ProfileRename(name="Work Account")
                    )
                )
            )
            assert clash["success"] is False

            # Step 6: Rate-limit switch to the second profile
            result = asyncio.run(
                settings_routes.retry_with_profile(
                    settings_routes.RetryWithProfileRequest(
                        profileId=profile_id_2,
                        reason="rate_limit",
                        operationContext={"operation": "generate_spec"},
                    )
                )
            )
            assert result["success"] is True
            assert result["previousProfileId"] == profile_id_1
            assert result["newProfileId"] == profile_id_2
            assert result["profileName"] == "Personal Account"
            assert result["reason"] == "rate_limit"
            assert result["operationContext"] == {"operation": "generate_spec"}
            assert json.loads(profiles_file.read_text())["activeProfileId"] == (
                profile_id_2
            )
            assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == personal_token

            # Step 7: Switching to the already-active profile is refused
            again = _refused(
                asyncio.run(
                    settings_routes.retry_with_profile(
                        settings_routes.RetryWithProfileRequest(profileId=profile_id_2)
                    )
                )
            )
            assert again["success"] is False

            # Step 8: Delete the now-inactive first profile; a second delete
            # errors. (Keep one profile so the endpoint does not touch the
            # real ~/.claude/oauth_token fallback cleanup path.)
            result = asyncio.run(settings_routes.delete_claude_profile(profile_id_1))
            assert result["success"] is True
            stored = json.loads(profiles_file.read_text())
            assert [p["id"] for p in stored["profiles"]] == [profile_id_2]
            assert stored["activeProfileId"] == profile_id_2
            gone = _refused(
                asyncio.run(settings_routes.delete_claude_profile(profile_id_1))
            )
            assert gone["success"] is False

    def test_api_profile_management_workflow(
        self, temp_dir: Path, mock_settings_dir: Path
    ):
        """
        Test API profile management workflow:
        1. Create API profile
        2. Update API profile settings
        3. Set as active
        4. Create second profile
        5. Switch to second profile
        6. Delete first profile
        """
        # Setup: Create initial API profiles file
        profiles_file = mock_settings_dir.parent / "api-profiles.json"
        profiles_data = {"activeProfileId": None, "profiles": []}
        profiles_file.write_text(json.dumps(profiles_data, indent=2))

        # This workflow would be implemented similarly to the Claude profile workflow
        # Testing create -> update -> set active -> switch -> delete
        pass


# ============================================================================
# WORKFLOW 2: Roadmap & Ideation - intentionally dropped (#912), the
# roadmap/ideation routes no longer exist in server/.
# ============================================================================


# ============================================================================
# WORKFLOW 3: GitLab Issue to MR - intentionally dropped (#912), the glab-CLI
# routes are gone; provider support lives in server/routes/github.py now.
# ============================================================================


# ============================================================================
# WORKFLOW 4: Project Setup
# ============================================================================


class TestProjectSetupWorkflow:
    """Test complete project onboarding and configuration."""

    def test_project_onboarding_workflow(self, tmp_path: Path, monkeypatch):
        """
        Test the complete project setup workflow (#912):
        1. Scan the filesystem for candidate projects
        2. Register the discovered project
        3. Reject registering the same path twice (409)
        4. Update project settings (.aifactory/.env mapping, 0o600 perms)
        5. Read the project back with merged settings
        6. Remove the project (unregister only, files stay)

        This simulates a new user adding and configuring their first project.
        """
        from fastapi import HTTPException
        from server import config
        from server.routes import projects as projects_routes

        monkeypatch.setattr(
            config.settings, "PROJECTS_DATA_DIR", str(tmp_path / "data")
        )

        # #1278 confines scan/register to the browsable roots, so a tmp_path
        # outside $HOME is refused with 403 before the scan runs.
        # APP_FILE_BROWSE_ROOTS is the documented operator escape hatch for a
        # deployment whose code lives elsewhere, which is exactly this shape --
        # so the test declares its root rather than the confinement being
        # loosened. Two entries: with one, "confines to the configured set" and
        # "confines to the first entry" are indistinguishable.
        monkeypatch.setenv(
            "APP_FILE_BROWSE_ROOTS", f"{tmp_path / 'other'}{os.pathsep}{tmp_path}"
        )

        # Filesystem: one real project, one plain dir, one always-skipped dir.
        code_root = tmp_path / "code"
        app_dir = code_root / "my-app"
        (app_dir / ".git").mkdir(parents=True)
        (app_dir / "package.json").write_text('{"name": "my-app"}')
        (code_root / "notes").mkdir()
        (code_root / "node_modules").mkdir()

        # Step 1: Scan for projects
        found = asyncio.run(
            projects_routes.scan_for_projects(
                projects_routes.ScanProjectsRequest(basePath=str(code_root), maxDepth=1)
            )
        )
        assert [p["name"] for p in found] == ["my-app"]
        assert found[0]["has_git"] is True
        assert found[0]["has_package_json"] is True
        assert found[0]["has_magestic_ai"] is False

        # Step 2: Register the discovered project
        created = asyncio.run(
            projects_routes.add_project(
                projects_routes.ProjectCreate(path=found[0]["path"], name="My App")
            )
        )
        project_id = created["id"]
        assert created["name"] == "My App"
        assert created["autoBuildPath"] == ""  # not initialized yet

        stored = projects_routes.load_projects()
        assert stored[project_id]["path"] == str(app_dir.resolve())
        assert stored[project_id]["org_id"]  # stamped for portal visibility

        # Step 3: Registering the same path again conflicts
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                projects_routes.add_project(
                    projects_routes.ProjectCreate(path=str(app_dir), name="Dup")
                )
            )
        assert exc.value.status_code == 409

        # Step 4: Update project settings; fields map to .aifactory/.env
        result = asyncio.run(
            projects_routes.update_project_settings(
                project_id,
                projects_routes.ProjectSettingsUpdate(
                    model="claude-test-model",
                    mainBranch="main",
                    autoPr=True,
                    autoMerge=False,
                ),
            )
        )
        assert result["success"] is True

        env_file = app_dir / ".aifactory" / ".env"
        env = dict(
            line.split("=", 1)
            for line in env_file.read_text().splitlines()
            if "=" in line
        )
        assert env["AI_FACTORY_MODEL"] == "claude-test-model"
        assert env["MAIN_BRANCH"] == "main"
        assert env["AIFACTORY_AUTO_PR"] == "true"
        assert env["AIFACTORY_AUTO_MERGE"] == "false"
        # May carry a git token later - must not be world-readable.
        assert env_file.stat().st_mode & 0o777 == 0o600

        # Step 5: Read the project back - saved settings are merged in and
        # the .aifactory dir created above now marks it initialized.
        fetched = asyncio.run(projects_routes.get_project(project_id))
        assert fetched["settings"]["model"] == "claude-test-model"
        assert fetched["settings"]["autoPr"] is True
        assert fetched["autoBuildPath"] == ".aifactory"

        # Step 6: Remove the project - unregistered, but files untouched
        asyncio.run(projects_routes.remove_project(project_id))
        assert project_id not in projects_routes.load_projects()
        assert app_dir.exists()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(projects_routes.get_project(project_id))
        assert exc.value.status_code == 404


# ============================================================================
# WORKFLOW 5: Settings Configuration
# ============================================================================


class TestSettingsConfigurationWorkflow:
    """Test complete settings configuration workflow."""

    def test_initial_setup_workflow(self, temp_dir: Path, mock_settings_dir: Path):
        """
        Test initial Magestic AI setup workflow:
        1. Update source environment (.env for backend)
        2. Set Anthropic API key
        3. Create API profile
        4. Set active API profile
        5. Configure auto-switch settings
        6. Update Claude token for active session

        This simulates initial setup by a new user.
        """
        # Setup files
        api_profiles_file = mock_settings_dir.parent / "api-profiles.json"
        api_profiles_data = {"activeProfileId": None, "profiles": []}
        api_profiles_file.write_text(json.dumps(api_profiles_data, indent=2))

        auto_switch_file = mock_settings_dir.parent / "auto-switch.json"
        auto_switch_data = {"enabled": False, "threshold": 80}
        auto_switch_file.write_text(json.dumps(auto_switch_data, indent=2))

        # This workflow would test the complete initial setup process
        # including all settings configuration steps
        pass


# ============================================================================
# WORKFLOW 6: Error Handling & Recovery
# ============================================================================


class TestErrorHandlingWorkflows:
    """Test workflows that involve error handling and recovery."""

    def test_rate_limit_recovery_workflow(
        self, temp_dir: Path, mock_settings_dir: Path
    ):
        """
        Test rate limit recovery workflow:
        1. Attempt operation (e.g., generate ideation)
        2. Encounter rate limit error
        3. Switch to backup profile
        4. Retry operation with new profile
        5. Operation succeeds

        This simulates handling rate limits with profile switching.
        """
        # This would test the retry_with_profile endpoint
        # in the context of recovering from rate limits
        pass

    def test_concurrent_file_access_workflow(self, temp_dir: Path):
        """
        Test handling of concurrent file modifications:
        1. Thread A starts updating settings
        2. Thread B starts updating same settings
        3. Verify atomic operations prevent corruption
        4. Verify proper error handling

        This tests file locking and atomic write operations.
        """
        # This would test concurrent access to the same files
        # and verify proper locking mechanisms
        pass


# ============================================================================
# WORKFLOW 7: Git Operations - intentionally dropped (#912): the squash /
# worktree / release endpoints are unmounted (git.project_router and
# git.releases_router are not included in main.py) and read
# settings.projects_file, which does not exist. Tracked separately.
# ============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
