"""#1944: the Bash hook must not take its allowlist from the agent-writable profile file."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "backend"))

from project.analyzer import seed_profile_with_plan_commands  # noqa: E402
from security.hooks import (  # noqa: E402
    bash_security_hook,
    git_dir_write_hook,
    validate_command,
)
from security.profile import reset_profile_cache  # noqa: E402

PROFILE = ".aifactory/.aifactory-security.json"


@pytest.fixture
def proj(tmp_path):
    reset_profile_cache()
    (tmp_path / ".aifactory").mkdir()
    return tmp_path


def test_forged_profile_file_is_ignored(proj):
    assert validate_command("ls", str(proj))[0]
    forged = ["nc", "ncat", "socat"]  # curl/wget are base commands
    # Edit the file detection just wrote, so it stays complete and hash-matching
    # and the old mtime reload would accept it.
    forged_profile = json.loads((proj / PROFILE).read_text())
    forged_profile["base_commands"] = forged_profile["custom_commands"] = forged
    (proj / PROFILE).write_text(json.dumps(forged_profile))
    for cmd in forged:
        assert not validate_command(cmd, str(proj))[0], cmd
    assert validate_command("ls", str(proj))[0]


def _write_blocked(proj, tool, path):
    out = asyncio.run(
        git_dir_write_hook(
            {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": str(proj)}
        )
    )
    return out.get("decision") == "block"


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_file_tools_cannot_write_profile(proj, tool):
    (proj / PROFILE).write_text("{}")
    (proj / "x.json").symlink_to(proj / PROFILE)
    for path in (
        PROFILE,
        str(proj / PROFILE),
        ".aifactory/.AIFACTORY-SECURITY.JSON",
        "x.json",
    ):
        assert _write_blocked(proj, tool, path), path
    assert not _write_blocked(proj, tool, "src/app.py")


def test_bash_naming_profile_is_blocked(proj):
    def run(cmd):
        return asyncio.run(
            bash_security_hook(
                {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(proj)}
            )
        )

    assert run(f"echo '{{}}' > {PROFILE}").get("decision") == "block"
    assert run(f"cp x {PROFILE}").get("decision") == "block"
    assert run("ls").get("decision") != "block"


def test_plan_seeding_is_live_without_cache_reset(proj):
    plan = proj / "implementation_plan.json"
    plan.write_text(
        json.dumps({"feature": "f", "required_commands": ["uv"], "phases": []})
    )
    assert not validate_command("uv --version", str(proj))[0]
    assert "uv" in seed_profile_with_plan_commands(proj, plan)
    assert validate_command("uv --version", str(proj))[0]
