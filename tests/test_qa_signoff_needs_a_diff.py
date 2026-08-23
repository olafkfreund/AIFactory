"""QA cannot approve a build that produced nothing (#1396).

The incident: a build wrote no files anywhere -- zero commits on its branch, a
clean worktree, nothing in any commit -- and was signed off `approved` with
`tests_passed: {"unit": "1/1"}` and `verified_by: "qa_agent"`. The Job reported
SuccessCriteriaMet and the progress bar read 2/2. Every layer that reports
upward agreed it was done and tested.

That is worse than a failed build, which is visible and gets retried. This one
is indistinguishable from a real success everywhere except the branch contents.

These tests build actual git repositories rather than mocking subprocess,
because the thing under test IS what git reports. A mocked `rev-list` would
pass against a check that asked git the wrong question.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

from agents.tools_pkg.tools.qa import _nothing_was_built  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """A clone whose base commit is on origin -- the build worktree's shape."""
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
    _git(clone, "checkout", "-qb", "aifactory/007-bench")
    return origin, clone


def test_a_build_that_produced_nothing_is_refused(origin_and_clone) -> None:
    """The observed #1396 state exactly: branch at base, clean tree."""
    _, clone = origin_and_clone

    reason = _nothing_was_built(clone)

    assert reason, "a branch at its base with a clean tree must read as unbuilt"
    assert "no commits beyond its base" in reason


def test_a_commit_counts_as_output(origin_and_clone) -> None:
    _, clone = origin_and_clone
    (clone / "bench.py").write_text("def add(a, b):\n    return a + b\n")
    _git(clone, "add", "bench.py")
    _git(clone, "commit", "-qm", "add bench")

    assert _nothing_was_built(clone) is None


def test_an_uncommitted_change_counts_as_output(origin_and_clone) -> None:
    """Work in progress is still work; only both-empty is 'nothing'."""
    _, clone = origin_and_clone
    (clone / "bench.py").write_text("x = 1\n")

    assert _nothing_was_built(clone) is None


def test_an_untracked_file_counts_as_output(origin_and_clone) -> None:
    """`--porcelain` reports untracked files; a new file is the usual output."""
    _, clone = origin_and_clone
    (clone / "brand_new.py").write_text("y = 2\n")

    assert _nothing_was_built(clone) is None


def test_base_commit_alone_is_not_counted_as_output(origin_and_clone) -> None:
    """The base commit must not be mistaken for this build's work.

    This is what `--not --remotes=origin` buys: the worktree always has history,
    so a plain commit count would read every empty build as productive.
    """
    _, clone = origin_and_clone
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=clone, capture_output=True, text=True
    )

    assert log.stdout.strip(), "the fixture must have history for this to mean anything"
    assert _nothing_was_built(clone), "history alone is not build output"


def test_a_non_git_directory_does_not_block_signoff(tmp_path: Path) -> None:
    """Fail toward allowing: git being unavailable is not evidence of no build.

    Blocking every sign-off when the check cannot run would trade a false pass
    for a false failure, and a QA gate nobody can satisfy gets disabled.
    """
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert _nothing_was_built(plain) is None


@pytest.fixture
def real_sdk():
    """Undo conftest's blanket `claude_agent_sdk` mock for these tests.

    tests/conftest.py installs a MagicMock whenever the module is not already
    in sys.modules, which is always true at collection time -- so it mocks even
    though the real package IS installed. Under that mock `@tool` returns a
    MagicMock and `.handler` cannot be awaited, which would leave the tool
    itself untested and only the helper covered. That is the exact gap this
    file exists to close, so the mock is swapped out and restored rather than
    worked around.
    """
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


class TestTheToolItselfRefuses:
    """End-to-end through the real tool handler, not just the helper.

    A correct guard that nothing calls leaves the bug exactly where it was, and
    a helper-only suite stays green through that. These drive
    `update_qa_status` itself and assert on the file it writes.
    """

    def _plan(self, spec: Path) -> None:
        spec.mkdir(parents=True, exist_ok=True)
        (spec / "implementation_plan.json").write_text(
            json.dumps({"status": "in_progress", "phases": []})
        )

    def _tool(self, spec: Path, project: Path):
        from agents.tools_pkg.tools import qa as qa_mod

        tools = qa_mod.create_qa_tools(spec, project)
        assert tools, "the SDK tools must be available for this test to mean anything"
        return tools[0].handler

    @pytest.mark.asyncio
    async def test_approved_is_refused_and_nothing_is_written(
        self, tmp_path: Path, origin_and_clone, real_sdk
    ) -> None:
        _, clone = origin_and_clone
        spec = tmp_path / "spec"
        self._plan(spec)
        handler = self._tool(spec, clone)

        result = await handler(
            {
                "status": "approved",
                "issues": "[]",
                "tests_passed": '{"unit": "1/1"}',
            }
        )

        text = result["content"][0]["text"]
        assert "Refusing to approve" in text, text

        plan = json.loads((spec / "implementation_plan.json").read_text())
        assert "qa_signoff" not in plan, (
            "the refusal must not still write the sign-off -- the artifact "
            "downstream consumers read is the whole problem"
        )
        assert plan["status"] == "in_progress", "status must not flip to human_review"

    @pytest.mark.asyncio
    async def test_approved_is_allowed_once_there_is_a_commit(
        self, tmp_path: Path, origin_and_clone, real_sdk
    ) -> None:
        """The guard must not block real work."""
        _, clone = origin_and_clone
        (clone / "bench.py").write_text("def add(a, b):\n    return a + b\n")
        _git(clone, "add", "bench.py")
        _git(clone, "commit", "-qm", "add bench")

        spec = tmp_path / "spec"
        self._plan(spec)
        handler = self._tool(spec, clone)

        result = await handler(
            {"status": "approved", "issues": "[]", "tests_passed": '{"unit": "1/1"}'}
        )

        assert "Refusing" not in result["content"][0]["text"]
        plan = json.loads((spec / "implementation_plan.json").read_text())
        assert plan["qa_signoff"]["status"] == "approved"
        assert plan["status"] == "human_review"

    @pytest.mark.asyncio
    async def test_rejected_is_never_blocked(
        self, tmp_path: Path, origin_and_clone, real_sdk
    ) -> None:
        """Only `approved` is gated.

        Reporting a failure on an empty build is exactly what SHOULD happen, so
        blocking it would remove the honest path and leave only silence.
        """
        _, clone = origin_and_clone
        spec = tmp_path / "spec"
        self._plan(spec)
        handler = self._tool(spec, clone)

        result = await handler(
            {
                "status": "rejected",
                "issues": '[{"description": "nothing built"}]',
                "tests_passed": "{}",
            }
        )

        assert "Refusing" not in result["content"][0]["text"]
        plan = json.loads((spec / "implementation_plan.json").read_text())
        assert plan["qa_signoff"]["status"] == "rejected"
