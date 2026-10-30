"""#1944: the Bash hook must not take its allowlist from the agent-writable profile file."""

import asyncio
import importlib
import json
import shutil
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


ALLOWLIST = ".aifactory-allowlist"


@pytest.mark.parametrize("tool", ["Write", "Edit"])
def test_file_tools_cannot_write_allowlist(proj, tool):
    assert _write_blocked(proj, tool, ALLOWLIST)
    assert _write_blocked(proj, tool, str(proj / ALLOWLIST.upper()))


def test_bash_naming_allowlist_is_blocked(proj):
    out = asyncio.run(
        bash_security_hook(
            {
                "tool_name": "Bash",
                "tool_input": {"command": f"echo nc >> {ALLOWLIST}"},
                "cwd": str(proj),
            }
        )
    )
    assert out.get("decision") == "block"


def test_allowlist_file_only_grants_grantable_names(proj):
    (proj / ALLOWLIST).write_text("nc\nuv\n")
    assert not validate_command("nc -l 4444", str(proj))[0]
    assert validate_command("uv --version", str(proj))[0]


def test_allowlist_is_bookkeeping():
    from agents.utils import _BOOKKEEPING_PATHS

    assert ALLOWLIST in _BOOKKEEPING_PATHS


def test_seeding_picks_up_scaffold_added_after_first_call(proj):
    assert validate_command("ls", str(proj))[0]
    (proj / "Cargo.toml").write_text('[package]\nname = "x"\nversion = "0.1.0"\n')
    plan = proj / "implementation_plan.json"
    plan.write_text(json.dumps({"feature": "f", "phases": []}))
    assert not validate_command("cargo build", str(proj))[0]
    seed_profile_with_plan_commands(proj, plan)
    assert validate_command("cargo build", str(proj))[0]


@pytest.mark.parametrize(
    ("required", "valid", "expected"),
    [(True, False, {}), (True, True, None), (False, False, None)],
)
def test_qa_entry_seeds_plan_by_review_state(
    proj, monkeypatch, required, valid, expected
):
    import cli.qa_commands as qc

    seen = []
    monkeypatch.setattr(qc, "print_banner", lambda: None)
    monkeypatch.setattr(qc, "validate_environment", lambda _s: True)
    monkeypatch.setattr(qc, "should_run_qa", lambda _s: True)
    # Module objects, not dotted strings: other tests swap the `review` package.
    monkeypatch.setattr(
        importlib.import_module("review.state"),
        "requires_review_before_coding",
        lambda _s: required,
    )
    monkeypatch.setattr(
        qc.ReviewState,
        "load",
        staticmethod(
            lambda _s: type("R", (), {"is_approval_valid": lambda *_a: valid})()
        ),
    )
    monkeypatch.setattr(
        importlib.import_module("project.analyzer"),
        "seed_profile_with_plan_commands",
        lambda _p, _f, plan: seen.append(plan),
    )

    async def _qa(**_kw):
        return True

    monkeypatch.setattr(qc, "run_qa_validation_loop", _qa)
    spec_dir = proj / ".aifactory" / "specs" / "spec"
    spec_dir.mkdir(parents=True)
    qc.handle_qa_command(proj, spec_dir, "m")
    assert seen == [expected]


def _source_and_worktree(proj):
    source = proj / ".aifactory" / "specs" / "001-x"
    source.mkdir(parents=True)
    (source / "spec.md").write_text("# Spec\n")
    (source / "task_metadata.json").write_text(
        json.dumps({"requireReviewBeforeCoding": True})
    )
    (source / "implementation_plan.json").write_text(
        json.dumps({"required_commands": ["uv"]})
    )
    review_state = importlib.import_module("review.state")
    review_state.ReviewState().approve(source)
    worktree = (
        proj
        / ".aifactory"
        / "worktrees"
        / "tasks"
        / "001-x"
        / ".aifactory"
        / "specs"
        / "001-x"
    )
    shutil.copytree(source, worktree)
    return source, worktree


def _qa_seed_seen(proj, spec_dir, monkeypatch):
    qc = importlib.import_module("cli.qa_commands")
    seen = []
    monkeypatch.setattr(qc, "print_banner", lambda: None)
    monkeypatch.setattr(qc, "validate_environment", lambda _s: True)
    monkeypatch.setattr(qc, "should_run_qa", lambda _s: True)
    monkeypatch.setattr(
        importlib.import_module("project.analyzer"),
        "seed_profile_with_plan_commands",
        lambda _p, _f, plan: seen.append(plan),
    )

    async def _qa(**_kw):
        return True

    monkeypatch.setattr(qc, "run_qa_validation_loop", _qa)
    qc.handle_qa_command(proj, spec_dir, "m")
    return seen


@pytest.mark.parametrize("tamper", ["forge_review", "flip_metadata"])
def test_qa_entry_reads_gate_from_source_not_worktree(proj, monkeypatch, tamper):
    source, worktree = _source_and_worktree(proj)
    (worktree / "implementation_plan.json").write_text(
        json.dumps({"required_commands": ["uv", "npx"]})
    )
    if tamper == "forge_review":
        importlib.import_module("review.state").ReviewState().approve(worktree)
    else:
        (worktree / "task_metadata.json").write_text(
            json.dumps({"requireReviewBeforeCoding": False})
        )

    assert source.is_dir()
    assert _qa_seed_seen(proj, worktree, monkeypatch) == [{}]


def test_qa_entry_worktree_without_source_seeds_nothing(proj, monkeypatch):
    source, worktree = _source_and_worktree(proj)
    shutil.rmtree(source)
    assert _qa_seed_seen(proj, worktree, monkeypatch) == [{}]


def test_qa_entry_approved_source_seeds_live_plan(proj, monkeypatch):
    source, _ = _source_and_worktree(proj)
    assert _qa_seed_seen(proj, source, monkeypatch) == [None]
