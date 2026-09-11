"""QA approval must be backed by an executed verification command (#1496).

The incident: a coder agent stated plainly, twice, that it could not run the
project's suite -- no Gradle/JVM/toolchain on PATH -- and the very same build
was still signed off `update_qa_status(status="approved")`. `build_report.json`
showed `gates: null`: no gate ran at all. "APPROVED ✓" read identically to a
build whose tests actually ran and passed, because the status field is a bare
string with no record of HOW it was verified.

`trailing_gate_evidence` (agents/gate_runner.py) is the one OBJECTIVE record:
the marker the coder's own trailing-gate step writes to the spec_dir, written
by a different code path than the one asking to approve. These tests drive
the real `update_qa_status` tool handler end-to-end and assert on the plan
file it does (or refuses to) write, matching the style of
`test_qa_signoff_needs_a_diff.py`'s `TestTheToolItselfRefuses`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def built_clone(tmp_path: Path) -> Path:
    """A worktree with a real commit beyond its base -- so #1396 never fires."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("base\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-qm", "base")
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(origin)],
        check=True,
        capture_output=True,
    )

    clone = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True
    )
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    _git(clone, "checkout", "-qb", "aifactory/002-blank-display-name")
    (clone / "profile.py").write_text("def save(name):\n    return name.strip()\n")
    _git(clone, "add", "profile.py")
    _git(clone, "commit", "-qm", "implement AC-PROF-001-02")
    return clone


@pytest.fixture
def real_sdk():
    """Undo conftest's blanket `claude_agent_sdk` mock (see test_qa_signoff_needs_a_diff.py)."""
    import importlib

    from agents.tools_pkg.tools import qa as qa_mod

    saved = {
        k: sys.modules[k] for k in list(sys.modules) if k.startswith("claude_agent_sdk")
    }
    for k in saved:
        del sys.modules[k]
    try:
        importlib.import_module("claude_agent_sdk")
    except ImportError:  # pragma: no cover - real SDK absent in this env
        sys.modules.update(saved)
        pytest.skip("the real claude_agent_sdk is not installed")
    importlib.reload(qa_mod)
    assert qa_mod.SDK_TOOLS_AVAILABLE, "reload must pick up the real SDK"
    yield qa_mod
    for k in [k for k in list(sys.modules) if k.startswith("claude_agent_sdk")]:
        del sys.modules[k]
    sys.modules.update(saved)
    importlib.reload(qa_mod)


def _plan(spec: Path) -> None:
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "implementation_plan.json").write_text(
        json.dumps({"status": "in_progress", "phases": []})
    )


def _tool(spec: Path, project: Path):
    from agents.tools_pkg.tools import qa as qa_mod

    tools = qa_mod.create_qa_tools(spec, project)
    assert tools, "the SDK tools must be available for this test to mean anything"
    return tools[0].handler


def _write_marker(spec: Path, project: Path, evidence: str) -> None:
    """Write `.trailing_gates_done` the way the real writer does (#1545): bound
    to `project`'s current git HEAD, so `trailing_gate_evidence` accepts it as
    describing THIS tree rather than a stale/foreign one."""
    from agents.gate_runner import gate_dir_for, write_trailing_gate_marker

    write_trailing_gate_marker(spec, gate_dir_for(spec, project), evidence)


@pytest.mark.asyncio
async def test_approval_with_no_gate_marker_is_refused(
    tmp_path: Path, built_clone: Path, real_sdk
) -> None:
    """The exact #1496 repro: real code, but no gate ever ran."""
    spec = tmp_path / "spec"
    _plan(spec)
    handler = _tool(spec, built_clone)

    result = await handler(
        {"status": "approved", "issues": "[]", "tests_passed": '{"unit": "1/1"}'}
    )

    text = result["content"][0]["text"]
    assert "Refusing to approve" in text, text
    assert "no verification" in text.lower()

    plan = json.loads((spec / "implementation_plan.json").read_text())
    assert "qa_signoff" not in plan, "a refusal must not still record 'approved'"
    assert plan["status"] == "in_progress"


@pytest.mark.asyncio
async def test_approval_when_every_gate_was_skipped_is_refused(
    tmp_path: Path, built_clone: Path, real_sdk
) -> None:
    """A toolchain absence must reach the verdict, not just prose (#1496)."""
    spec = tmp_path / "spec"
    _plan(spec)
    _write_marker(spec, built_clone, "kotlin-unit: skipped, swift-unit: skipped")
    handler = _tool(spec, built_clone)

    result = await handler(
        {"status": "approved", "issues": "[]", "tests_passed": '{"unit": "N/A"}'}
    )

    assert "Refusing to approve" in result["content"][0]["text"]
    plan = json.loads((spec / "implementation_plan.json").read_text())
    assert "qa_signoff" not in plan


@pytest.mark.asyncio
async def test_approval_when_no_gates_were_detected_is_refused(
    tmp_path: Path, built_clone: Path, real_sdk
) -> None:
    spec = tmp_path / "spec"
    _plan(spec)
    _write_marker(spec, built_clone, f"no gates detected in {built_clone}")
    handler = _tool(spec, built_clone)

    result = await handler({"status": "approved", "issues": "[]", "tests_passed": "{}"})

    assert "Refusing to approve" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_approval_when_a_gate_failed_is_refused(
    tmp_path: Path, built_clone: Path, real_sdk
) -> None:
    """A failed gate is not an approval, even if the agent says otherwise."""
    spec = tmp_path / "spec"
    _plan(spec)
    _write_marker(spec, built_clone, "pytest: failed")
    handler = _tool(spec, built_clone)

    result = await handler(
        {"status": "approved", "issues": "[]", "tests_passed": '{"unit": "0/1"}'}
    )

    text = result["content"][0]["text"]
    assert "Refusing to approve" in text
    assert "did not all pass" in text
    plan = json.loads((spec / "implementation_plan.json").read_text())
    assert "qa_signoff" not in plan


@pytest.mark.asyncio
async def test_approval_with_a_real_passing_gate_is_allowed(
    tmp_path: Path, built_clone: Path, real_sdk
) -> None:
    """The guard must not block a build that was genuinely verified."""
    spec = tmp_path / "spec"
    _plan(spec)
    _write_marker(spec, built_clone, "pytest: passed")
    handler = _tool(spec, built_clone)

    result = await handler(
        {"status": "approved", "issues": "[]", "tests_passed": '{"unit": "3/3"}'}
    )

    assert "Refusing" not in result["content"][0]["text"]
    plan = json.loads((spec / "implementation_plan.json").read_text())
    assert plan["qa_signoff"]["status"] == "approved"
    assert plan["status"] == "human_review"


@pytest.mark.asyncio
async def test_rejected_is_never_gated_on_evidence(
    tmp_path: Path, built_clone: Path, real_sdk
) -> None:
    """Only `approved` needs evidence -- reporting a failure never does."""
    spec = tmp_path / "spec"
    _plan(spec)
    handler = _tool(spec, built_clone)

    result = await handler(
        {
            "status": "rejected",
            "issues": '[{"description": "no toolchain to verify with"}]',
            "tests_passed": "{}",
        }
    )

    assert "Refusing" not in result["content"][0]["text"]
    plan = json.loads((spec / "implementation_plan.json").read_text())
    assert plan["qa_signoff"]["status"] == "rejected"
