#!/usr/bin/env python3
"""
Tests for solo mode (issue #276).

Solo mode is a streamlined single-agent build path: instead of the full
planner -> coder -> QA pipeline, one self-directed agent authors its own
``implementation_plan.json``, implements it, and verifies its own work. It is
opt-in (default OFF) and backward compatible.

Coverage:
- Flag resolution: env var / per-spec task_metadata.json / global config / default
- Orchestration seam: with solo ON the first session uses the solo prompt and
  the coder toolset; with solo OFF the dedicated planner prompt + planner
  toolset are used (backward compatible).
- Self-management: the solo prompt instructs the agent to author its own plan
  and keep subtask statuses current via update_subtask_status.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).parent.parent / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ---------------------------------------------------------------------------
# Flag resolution
# ---------------------------------------------------------------------------
class TestSoloModeFlag:
    """Resolution order: env var > task_metadata.json > global config > default."""

    def test_default_disabled(self, tmp_path: Path, monkeypatch):
        """Solo mode is OFF by default (no env, no metadata, no global config)."""
        monkeypatch.delenv("AIFACTORY_SOLO_MODE", raising=False)
        import solo_mode

        # Point the global config lookup at an empty home so the real user
        # config can't leak into the test.
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        assert solo_mode.is_solo_mode_enabled() is False
        assert solo_mode.is_solo_mode_enabled_for_spec(tmp_path) is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "On"])
    def test_env_enables(self, tmp_path: Path, monkeypatch, value):
        monkeypatch.setenv("AIFACTORY_SOLO_MODE", value)
        import solo_mode

        assert solo_mode.is_solo_mode_enabled() is True
        assert solo_mode.is_solo_mode_enabled_for_spec(tmp_path) is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "FALSE"])
    def test_env_disables(self, tmp_path: Path, monkeypatch, value):
        monkeypatch.setenv("AIFACTORY_SOLO_MODE", value)
        import solo_mode

        assert solo_mode.is_solo_mode_enabled() is False
        assert solo_mode.is_solo_mode_enabled_for_spec(tmp_path) is False

    def test_env_overrides_metadata(self, tmp_path: Path, monkeypatch):
        """An explicit env value wins over the per-spec metadata flag."""
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        (spec_dir / "task_metadata.json").write_text(json.dumps({"soloMode": True}))

        import solo_mode

        # Env OFF beats metadata ON.
        monkeypatch.setenv("AIFACTORY_SOLO_MODE", "off")
        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is False

        # Env ON beats metadata OFF.
        (spec_dir / "task_metadata.json").write_text(json.dumps({"soloMode": False}))
        monkeypatch.setenv("AIFACTORY_SOLO_MODE", "on")
        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is True

    def test_metadata_enables_when_env_absent(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("AIFACTORY_SOLO_MODE", raising=False)
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        (spec_dir / "task_metadata.json").write_text(json.dumps({"soloMode": True}))

        import solo_mode

        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is True

    def test_global_config_enables(self, tmp_path: Path, monkeypatch):
        """A global ~/.aifactory/config.json with solo.enabled=true turns it on."""
        monkeypatch.delenv("AIFACTORY_SOLO_MODE", raising=False)
        fake_home = tmp_path / "home"
        (fake_home / ".aifactory").mkdir(parents=True)
        (fake_home / ".aifactory" / "config.json").write_text(
            json.dumps({"solo": {"enabled": True}})
        )
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        import solo_mode

        assert solo_mode.is_solo_mode_enabled() is True
        # A spec with no metadata falls back to the global setting.
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is True

    def test_malformed_metadata_is_safe(self, tmp_path: Path, monkeypatch):
        """A corrupt task_metadata.json must not raise; falls back to default OFF."""
        monkeypatch.delenv("AIFACTORY_SOLO_MODE", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        (spec_dir / "task_metadata.json").write_text("{ not valid json")

        import solo_mode

        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is False


# ---------------------------------------------------------------------------
# skipPlanning -> solo (#1078)
# ---------------------------------------------------------------------------
class TestSkipPlanningEnablesSolo:
    """The RFC-0011 intake tier writes ``skipPlanning``; the build must read it.

    ``build_execution_block`` sets ``skip_planning=True`` for factory:low and
    factory:medium and ``execution_profile_to_metadata`` maps it to
    ``skipPlanning`` in task_metadata.json. Before #1078 nothing in apps/ read
    that key, so a low-tier run spent its budget on a planner session and
    stopped there. "Too small to plan" is solo mode's contract, so this is where
    the flag lands.
    """

    @staticmethod
    def _spec(tmp_path: Path, metadata: dict) -> Path:
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        (spec_dir / "task_metadata.json").write_text(json.dumps(metadata))
        return spec_dir

    def test_skip_planning_true_enables_solo(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("AIFACTORY_SOLO_MODE", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        import solo_mode

        spec_dir = self._spec(tmp_path, {"skipPlanning": True})
        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is True

    def test_skip_planning_false_disables_solo(self, tmp_path: Path, monkeypatch):
        """The hard tier's explicit ``false`` beats a global solo default."""
        monkeypatch.delenv("AIFACTORY_SOLO_MODE", raising=False)
        fake_home = tmp_path / "home"
        (fake_home / ".aifactory").mkdir(parents=True)
        (fake_home / ".aifactory" / "config.json").write_text(
            json.dumps({"solo": {"enabled": True}})
        )
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        import solo_mode

        spec_dir = self._spec(tmp_path, {"skipPlanning": False})
        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is False

    def test_solo_mode_key_wins_over_skip_planning(self, tmp_path: Path, monkeypatch):
        """``soloMode`` is the explicit toggle and wins when both are present."""
        monkeypatch.delenv("AIFACTORY_SOLO_MODE", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        import solo_mode

        spec_dir = self._spec(tmp_path, {"soloMode": False, "skipPlanning": True})
        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is False

    def test_env_overrides_skip_planning(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AIFACTORY_SOLO_MODE", "off")
        import solo_mode

        spec_dir = self._spec(tmp_path, {"skipPlanning": True})
        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is False

    def test_low_tier_execution_block_reaches_solo_end_to_end(
        self, tmp_path: Path, monkeypatch
    ):
        """intake tier -> execution block -> task_metadata.json -> solo ON.

        The seam the ticket is really about: run the real producers rather than
        hand-writing the key, so a rename on either side fails here.
        """
        monkeypatch.delenv("AIFACTORY_SOLO_MODE", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        import solo_mode
        from intake.execution_block import build_execution_block
        from pfactory.tiers import Tier
        from trusted_plan import execution_profile_to_metadata

        block = build_execution_block(Tier.LOW, low_model_resolver=lambda: "haiku")
        meta = execution_profile_to_metadata(block)
        assert meta["skipPlanning"] is True

        spec_dir = self._spec(tmp_path, meta)
        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is True

        # ...and the hard tier still gets the full planner.
        hard = execution_profile_to_metadata(build_execution_block(Tier.HARD))
        hard_dir = tmp_path / "hard"
        hard_dir.mkdir()
        (hard_dir / "task_metadata.json").write_text(json.dumps(hard))
        assert solo_mode.is_solo_mode_enabled_for_spec(hard_dir) is False


# ---------------------------------------------------------------------------
# Solo prompt (self-management contract)
# ---------------------------------------------------------------------------
class TestSoloPrompt:
    def test_solo_prompt_loads_and_self_directs(self, tmp_path: Path):
        from prompts import get_solo_prompt

        spec_dir = tmp_path / "001-spec"
        spec_dir.mkdir()
        prompt = get_solo_prompt(spec_dir)

        # The agent is told to author its own plan AND to keep statuses current
        # via the subtask tool — that is what makes the orchestrator loop
        # terminate without a separate planner/QA agent.
        assert "implementation_plan.json" in prompt
        assert "update_subtask_status" in prompt
        # Spec path is injected so the agent knows where to write artifacts.
        assert str(spec_dir) in prompt


# ---------------------------------------------------------------------------
# Orchestration seam: solo ON vs OFF selects the right path
# ---------------------------------------------------------------------------
def _make_spec(tmp_path: Path) -> Path:
    spec_dir = tmp_path / "001-spec"
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text("# Feature\nDo a small thing.\n")
    return spec_dir


async def _run_one_turn(spec_dir: Path, project_dir: Path):
    """Drive run_autonomous_agent for a single (mocked) session.

    All heavy collaborators are mocked. ``run_session_guarded`` returns
    ``complete`` so the loop exits after the first turn, letting us assert
    which prompt + agent_type the seam selected.
    """
    import contextlib

    import agents.coder as coder

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    create_client_mock = MagicMock(return_value=fake_client)
    solo_p = MagicMock(return_value="SOLO_PROMPT")
    plan_p = MagicMock(return_value="PLANNER_PROMPT")

    # Mock every heavy collaborator so a single turn runs without an LLM. The
    # session returns "complete" so the loop exits after the first turn, which
    # is enough to observe which prompt + agent_type the seam selected.
    patchers = {
        "create_client": create_client_mock,
        "get_solo_prompt": solo_p,
        "generate_planner_prompt": plan_p,
        "run_session_guarded": AsyncMock(return_value=("complete", "", {})),
        "get_graphiti_context": AsyncMock(return_value=""),
        "RecoveryManager": MagicMock(),
        "StatusManager": MagicMock(),
        "get_task_logger": MagicMock(return_value=None),
        "debug_memory_system_status": MagicMock(),
        "record_turn": MagicMock(),
        "CompactionDetector": MagicMock(),
        "build_operational_context": MagicMock(return_value=""),
        "drain_inbox": MagicMock(return_value=[]),
        "is_build_complete": MagicMock(return_value=True),
        "print_build_complete_banner": MagicMock(),
        "print_progress_summary": MagicMock(),
        "print_session_header": MagicMock(),
        "count_subtasks": MagicMock(return_value=(0, 0)),
        "count_subtasks_detailed": MagicMock(
            return_value={"completed": 0, "total": 0, "in_progress": 0}
        ),
        "get_current_phase": MagicMock(return_value=None),
        "get_next_subtask": MagicMock(return_value=None),
        "get_latest_commit": MagicMock(return_value="abc"),
        "get_commit_count": MagicMock(return_value=0),
        "infer_provider_from_model": MagicMock(return_value="claude"),
        "get_phase_model": MagicMock(return_value="claude-sonnet-4-5"),
        "get_phase_thinking_budget": MagicMock(return_value=None),
    }

    with contextlib.ExitStack() as stack:
        for name, replacement in patchers.items():
            stack.enter_context(patch.object(coder, name, replacement))
        await coder.run_autonomous_agent(
            project_dir=project_dir,
            spec_dir=spec_dir,
            model="claude-sonnet-4-5",
            max_iterations=1,
        )

    return create_client_mock, solo_p, plan_p


@pytest.mark.asyncio
class TestSoloSeam:
    async def test_solo_on_uses_solo_prompt_and_coder_tools(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("AIFACTORY_SOLO_MODE", "true")
        spec_dir = _make_spec(tmp_path)

        create_client_mock, solo_p, plan_p = await _run_one_turn(spec_dir, tmp_path)

        # Solo prompt used; planner prompt not.
        assert solo_p.called, "solo prompt should be generated in solo mode"
        assert not plan_p.called, "planner prompt must NOT be used in solo mode"

        # First session is created with the coder toolset (so the single agent
        # has update_subtask_status to track its own plan).
        assert create_client_mock.called
        _, kwargs = create_client_mock.call_args
        assert kwargs.get("agent_type") == "coder", (
            f"solo first session must use coder toolset, got {kwargs.get('agent_type')!r}"
        )

    async def test_solo_off_uses_planner_prompt_and_planner_tools(
        self, tmp_path: Path, monkeypatch
    ):
        """Backward compatibility: with solo OFF the dedicated planner path runs."""
        monkeypatch.setenv("AIFACTORY_SOLO_MODE", "off")
        spec_dir = _make_spec(tmp_path)

        create_client_mock, solo_p, plan_p = await _run_one_turn(spec_dir, tmp_path)

        assert plan_p.called, "planner prompt should be used when solo is OFF"
        assert not solo_p.called, "solo prompt must NOT be used when solo is OFF"

        assert create_client_mock.called
        _, kwargs = create_client_mock.call_args
        assert kwargs.get("agent_type") == "planner", (
            f"non-solo first session must use planner toolset, got {kwargs.get('agent_type')!r}"
        )


# ---------------------------------------------------------------------------
# QA skip wiring (solo agent is its own QA)
# ---------------------------------------------------------------------------
class TestSoloSkipsQA:
    def test_solo_forces_skip_qa_in_build_command(self, tmp_path: Path, monkeypatch):
        """With solo ON, handle_build_command must set skip_qa=True before QA.

        We stop the build right after run_autonomous_agent by raising, and
        capture the resolved ``skip_qa`` via the should_run_qa probe — which is
        only consulted when skip_qa is False. In solo mode it must never be
        consulted.
        """
        import contextlib

        monkeypatch.setenv("AIFACTORY_SOLO_MODE", "true")
        spec_dir = _make_spec(tmp_path)

        from cli import build_commands

        should_run_qa_mock = MagicMock(return_value=True)
        review_state = MagicMock()
        review_state.is_approval_valid.return_value = True
        review_state_cls = MagicMock()
        review_state_cls.load.return_value = review_state

        # Make run_autonomous_agent a no-op so we reach the QA gate quickly.
        # We only care that should_run_qa is never consulted (skip_qa
        # short-circuits the `not skip_qa and ...` guard in solo mode).
        patches = [
            patch("agent.run_autonomous_agent", new=AsyncMock(return_value=None)),
            patch("agent.sync_plan_to_source", MagicMock(return_value=False)),
            patch("qa_loop.should_run_qa", should_run_qa_mock),
            patch("qa_loop.is_qa_approved", MagicMock(return_value=False)),
            patch("qa_loop.run_qa_validation_loop", new=AsyncMock(return_value=True)),
            # validate_environment / print_banner are lazy-imported from
            # cli.utils inside the function, so patch them at the source.
            patch("cli.utils.validate_environment", MagicMock(return_value=True)),
            patch("cli.utils.print_banner", MagicMock()),
            patch.object(
                build_commands,
                "choose_workspace",
                MagicMock(return_value=build_commands.WorkspaceMode.DIRECT),
            ),
            patch.object(
                build_commands,
                "get_existing_build_worktree",
                MagicMock(return_value=None),
            ),
            # ReviewState is bound at module import time in build_commands.
            patch.object(build_commands, "ReviewState", review_state_cls),
        ]

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            build_commands.handle_build_command(
                project_dir=tmp_path,
                spec_dir=spec_dir,
                model="claude-sonnet-4-5",
                max_iterations=1,
                verbose=False,
                force_isolated=False,
                force_direct=True,
                auto_continue=True,
                skip_qa=False,  # caller did NOT skip; solo mode must force it
                force_bypass_approval=False,
            )

        assert not should_run_qa_mock.called, (
            "solo mode must skip QA: should_run_qa was consulted, meaning "
            "skip_qa was not forced True"
        )


# ---------------------------------------------------------------------------
# Web-server wiring (#281): the saved soloMode setting must actually drive
# solo_mode.py. The seam is task creation in projects.create_project_task,
# which stamps soloMode into the new spec's task_metadata.json (read by
# is_solo_mode_enabled_for_spec) from the saved app settings. A settings save
# also mirrors the flag into the global ~/.aifactory/config.json solo.enabled.
# ---------------------------------------------------------------------------
_WEB_SERVER = Path(__file__).parent.parent / "apps" / "web-server"
if str(_WEB_SERVER) not in sys.path:
    sys.path.insert(0, str(_WEB_SERVER))


def _make_project(tmp_path: Path) -> tuple[str, Path]:
    """Create a temp project on disk and return (project_id, project_path)."""
    project_path = tmp_path / "proj"
    (project_path / ".aifactory" / "specs").mkdir(parents=True)
    return "proj-1", project_path


@pytest.mark.asyncio
class TestSoloSettingDrivesTaskCreation:
    """With soloMode saved/true, a new task's task_metadata.json makes
    is_solo_mode_enabled_for_spec(spec_dir) return True; false/absent -> False."""

    async def _create_task(self, tmp_path, monkeypatch, *, saved_solo, metadata=None):
        from server.routes import projects

        project_id, project_path = _make_project(tmp_path)

        # Saved app settings: surface the soloMode preference under test.
        fake_settings = MagicMock()
        fake_settings.soloMode = saved_solo
        monkeypatch.setattr(
            "server.routes.settings.load_app_settings",
            lambda: fake_settings,
        )

        # Avoid touching the real projects.json / Task model machinery.
        monkeypatch.setattr(
            projects,
            "load_projects",
            lambda: {project_id: {"path": str(project_path)}},
        )
        import server.routes.tasks as tasks_module

        monkeypatch.setattr(tasks_module, "spec_to_task", lambda pid, sd: sd)
        monkeypatch.setattr(
            tasks_module, "task_to_dict", lambda task: {"spec_dir": str(task)}
        )

        req = projects.TaskCreateRequest(
            title="Add a thing", description="Do a small thing.", metadata=metadata
        )
        result = await projects.create_project_task(project_id, req)
        return Path(result["spec_dir"])

    async def test_saved_solo_true_enables_for_new_spec(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AIFACTORY_SOLO_MODE", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        spec_dir = await self._create_task(tmp_path, monkeypatch, saved_solo=True)

        meta = json.loads((spec_dir / "task_metadata.json").read_text())
        assert meta.get("soloMode") is True

        import solo_mode

        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is True

    async def test_saved_solo_false_leaves_spec_disabled(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AIFACTORY_SOLO_MODE", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        spec_dir = await self._create_task(tmp_path, monkeypatch, saved_solo=False)

        # No metadata at all (no model fields, solo off) -> nothing written.
        meta_file = spec_dir / "task_metadata.json"
        if meta_file.exists():
            assert "soloMode" not in json.loads(meta_file.read_text())

        import solo_mode

        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is False

    async def test_per_task_metadata_overrides_saved_setting(
        self, tmp_path, monkeypatch
    ):
        """An explicit per-task soloMode=False wins over a saved global True."""
        monkeypatch.delenv("AIFACTORY_SOLO_MODE", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        spec_dir = await self._create_task(
            tmp_path, monkeypatch, saved_solo=True, metadata={"soloMode": False}
        )

        import solo_mode

        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is False

    async def test_env_still_overrides_saved_setting(self, tmp_path, monkeypatch):
        """Backend env override beats a stamped soloMode (precedence unchanged)."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

        spec_dir = await self._create_task(tmp_path, monkeypatch, saved_solo=True)
        meta = json.loads((spec_dir / "task_metadata.json").read_text())
        assert meta.get("soloMode") is True  # stamped...

        import solo_mode

        monkeypatch.setenv("AIFACTORY_SOLO_MODE", "off")  # ...but env wins
        assert solo_mode.is_solo_mode_enabled_for_spec(spec_dir) is False


class TestSoloSettingMirrorsToGlobalConfig:
    """Saving settings mirrors soloMode into ~/.aifactory/config.json solo.enabled,
    which solo_mode.py uses as the global fallback."""

    def test_save_true_writes_global_enabled(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AIFACTORY_SOLO_MODE", raising=False)
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        from server.routes import settings as settings_route

        # Point the app-settings file at a temp dir so we don't touch real data.
        monkeypatch.setattr(
            settings_route,
            "get_settings_file",
            lambda: tmp_path / "settings.json",
        )

        settings_route.save_app_settings(settings_route.AppSettings(soloMode=True))

        config = json.loads((fake_home / ".aifactory" / "config.json").read_text())
        assert config["solo"]["enabled"] is True

        import solo_mode

        assert solo_mode.is_solo_mode_enabled() is True

    def test_save_preserves_other_global_keys(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        (fake_home / ".aifactory").mkdir(parents=True)
        (fake_home / ".aifactory" / "config.json").write_text(
            json.dumps({"other": {"keep": 1}, "solo": {"enabled": False}})
        )
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        from server.routes import settings as settings_route

        monkeypatch.setattr(
            settings_route,
            "get_settings_file",
            lambda: tmp_path / "settings.json",
        )

        settings_route.save_app_settings(settings_route.AppSettings(soloMode=True))

        config = json.loads((fake_home / ".aifactory" / "config.json").read_text())
        assert config["other"] == {"keep": 1}  # untouched
        assert config["solo"]["enabled"] is True
