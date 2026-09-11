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

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "backend"))

import agents.tools_pkg.tools.qa as qa_mod
from agents.tools_pkg.tools.qa import (
    _claim_gate_refresh,
    _release_gate_refresh_claim,
)
from core.workspace.setup import copy_spec_to_worktree


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


# =============================================================================
# Stale evidence gets ONE re-run attempt, never a silent pass (#1546+1)
#
# Repro: the coder's trailing-gate step writes a marker bound to the sha it
# ran gates against. A later commit -- QA's own flake.nix touch-up, a fixer
# iteration -- moves HEAD, and #1545 correctly stops treating that marker as
# evidence for the NEW tree. Before this fix nothing ever re-ran the gate, so
# the build sat unapprovable forever even though the toolchain plainly works
# here (the ORIGINAL run proves that). These tests stub the coder's own
# trailing-gate runner (the exact function the refresh delegates to) so no
# real subprocess/Nix Job is required to prove the wiring.
# =============================================================================


def _head_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write_marker_for_sha(spec: Path, sha: str, evidence: str) -> None:
    """A marker bound to an EXPLICIT (possibly stale) sha, not current HEAD."""
    (spec / ".trailing_gates_done").write_text(f"{sha}\n{evidence}\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_stale_marker_is_refreshed_and_then_approved(
    tmp_path: Path, built_clone: Path, real_sdk, monkeypatch
) -> None:
    """A stale-but-once-passing marker gets refreshed, then approval succeeds."""
    spec = tmp_path / "spec"
    _plan(spec)
    stale_sha = _head_sha(built_clone)
    _write_marker_for_sha(spec, stale_sha, "pytest: passed")

    # The trivial post-gate commit that invalidated the marker (#1545).
    (built_clone / "flake.nix").write_text("{ }\n")
    _git(built_clone, "add", "flake.nix")
    _git(built_clone, "commit", "-qm", "add flake.nix")

    calls = []

    async def fake_rerun(spec_dir: Path, project_dir: Path) -> None:
        calls.append((spec_dir, project_dir))
        _write_marker_for_sha(spec_dir, _head_sha(project_dir), "pytest: passed")

    monkeypatch.setattr(
        "agents.coder._run_trailing_gates_if_build_complete", fake_rerun
    )

    handler = real_sdk.create_qa_tools(spec, built_clone)[0].handler
    result = await handler(
        {"status": "approved", "issues": "[]", "tests_passed": '{"unit": "3/3"}'}
    )

    assert len(calls) == 1, (
        f"the stale marker must trigger EXACTLY one refresh attempt, got {len(calls)}"
    )
    text = result["content"][0]["text"]
    assert "Refusing" not in text, text
    plan = json.loads((spec / "implementation_plan.json").read_text())
    assert plan["qa_signoff"]["status"] == "approved"


@pytest.mark.asyncio
async def test_stale_marker_refresh_that_fails_is_refused(
    tmp_path: Path, built_clone: Path, real_sdk, monkeypatch
) -> None:
    """A refreshed run that fails is a real refusal, not a silent pass."""
    spec = tmp_path / "spec"
    _plan(spec)
    stale_sha = _head_sha(built_clone)
    _write_marker_for_sha(spec, stale_sha, "pytest: passed")

    (built_clone / "flake.nix").write_text("{ }\n")
    _git(built_clone, "add", "flake.nix")
    _git(built_clone, "commit", "-qm", "add flake.nix")

    async def fake_rerun(spec_dir: Path, project_dir: Path) -> None:
        _write_marker_for_sha(spec_dir, _head_sha(project_dir), "pytest: failed")

    monkeypatch.setattr(
        "agents.coder._run_trailing_gates_if_build_complete", fake_rerun
    )

    handler = real_sdk.create_qa_tools(spec, built_clone)[0].handler
    result = await handler(
        {"status": "approved", "issues": "[]", "tests_passed": '{"unit": "2/3"}'}
    )

    text = result["content"][0]["text"]
    assert "Refusing to approve" in text
    assert "did not all pass" in text
    plan = json.loads((spec / "implementation_plan.json").read_text())
    assert "qa_signoff" not in plan


@pytest.mark.asyncio
async def test_absent_marker_is_never_refreshed(
    tmp_path: Path, built_clone: Path, real_sdk, monkeypatch
) -> None:
    """No marker at all must stay a hard refusal -- never a free re-run.

    A build that never ran a gate is the #1496 case this guard exists for.
    Refreshing here too would spend a full Nix Job retrying a toolchain that
    may genuinely be absent, on every single approval attempt.
    """

    async def fail_if_called(spec_dir: Path, project_dir: Path) -> None:
        raise AssertionError(
            f"must not attempt a gate run when no marker exists "
            f"(spec_dir={spec_dir}, project_dir={project_dir})"
        )

    monkeypatch.setattr(
        "agents.coder._run_trailing_gates_if_build_complete", fail_if_called
    )

    spec = tmp_path / "spec"
    _plan(spec)
    handler = real_sdk.create_qa_tools(spec, built_clone)[0].handler

    result = await handler(
        {"status": "approved", "issues": "[]", "tests_passed": '{"unit": "1/1"}'}
    )

    text = result["content"][0]["text"]
    assert "Refusing to approve" in text
    assert "no verification" in text.lower()


# =============================================================================
# The refresh-dispatch race (Copilot review of #1547, now #1548)
#
# The stale check and the coder-runner dispatch are two separate operations
# with a window between them. Two concurrent `update_qa_status(approved)`
# calls for the SAME spec_dir can both see "stale" before either writes a
# fresh marker, and both would dispatch a full gate Job for the same HEAD.
# `_claim_gate_refresh` / `_release_gate_refresh_claim` are the atomic
# filesystem claim that closes that window; these tests exercise the claim
# primitive directly and the actual concurrent-approval path end-to-end.
# =============================================================================


def test_claim_gate_refresh_is_exclusive(tmp_path: Path) -> None:
    """A second claim attempt must fail while the first is still held."""
    spec = tmp_path / "spec"
    spec.mkdir()

    assert _claim_gate_refresh(spec) is True
    assert _claim_gate_refresh(spec) is False, (
        "a live claim must block a second claimant"
    )

    _release_gate_refresh_claim(spec)
    assert _claim_gate_refresh(spec) is True, "release must free the claim for reuse"


def test_stale_claim_past_ttl_is_reclaimed(tmp_path: Path, monkeypatch) -> None:
    """A claim left behind by a crashed refresh must not block forever."""
    monkeypatch.setattr(qa_mod, "_STALE_CLAIM_TTL_SECONDS", 0)

    spec = tmp_path / "spec"
    spec.mkdir()
    assert (
        qa_mod._claim_gate_refresh(spec) is True
    )  # never released -- simulates a crash

    # TTL is 0, so any age at all counts as expired.
    assert qa_mod._claim_gate_refresh(spec) is True, (
        "an expired claim must be reclaimable"
    )


@pytest.mark.asyncio
async def test_concurrent_stale_refresh_dispatches_gate_run_once(
    tmp_path: Path, built_clone: Path, real_sdk, monkeypatch
) -> None:
    """Two simultaneous approval attempts must dispatch at most ONE gate run.

    This is the actual race Copilot flagged: without the claim, both tasks
    read the marker as stale before either writes a fresh one, and both
    reach the coder's gate runner. The stub below sleeps mid-"run" to widen
    that window as far as possible under `asyncio`'s cooperative scheduling.
    """
    spec = tmp_path / "spec"
    _plan(spec)
    stale_sha = _head_sha(built_clone)
    _write_marker_for_sha(spec, stale_sha, "pytest: passed")

    (built_clone / "flake.nix").write_text("{ }\n")
    _git(built_clone, "add", "flake.nix")
    _git(built_clone, "commit", "-qm", "add flake.nix")

    calls = []

    async def fake_rerun(spec_dir: Path, project_dir: Path) -> None:
        calls.append((spec_dir, project_dir))
        await asyncio.sleep(0.05)  # yield control so the second task can race in
        _write_marker_for_sha(spec_dir, _head_sha(project_dir), "pytest: passed")

    monkeypatch.setattr(
        "agents.coder._run_trailing_gates_if_build_complete", fake_rerun
    )

    handler = real_sdk.create_qa_tools(spec, built_clone)[0].handler
    approve_args = {
        "status": "approved",
        "issues": "[]",
        "tests_passed": '{"unit": "3/3"}',
    }
    results = await asyncio.gather(handler(approve_args), handler(approve_args))

    assert len(calls) == 1, f"expected exactly one gate re-run, got {len(calls)}"
    texts = [r["content"][0]["text"] for r in results]
    assert any("Refusing" not in t for t in texts), (
        f"at least one of the two concurrent approvals must succeed: {texts}"
    )


# =============================================================================
# The claim must not leak through spec-dir replication (#1549 review)
#
# `spec_dir` is not a private, single-owner directory -- it is replicated by
# two REAL code paths: `copy_spec_to_worktree` (apps/backend, main -> every
# new worktree, unconditional `shutil.copytree`) and
# `WorktreeSyncMixin._sync_worktree_files` (apps/web-server, worktree -> main,
# a catch-all loop over every file not on a hardcoded allowlist). A claim
# living INSIDE spec_dir would be swept up by either. These tests drive the
# REAL replication functions (no mocking of the copy logic itself) and assert
# the claim never appears on the other side.
# =============================================================================


def test_claim_does_not_leak_into_new_worktree_via_copy_spec_to_worktree(
    tmp_path: Path,
) -> None:
    """`copy_spec_to_worktree` copies the WHOLE source spec dir into every new
    worktree, unconditionally. A claim inside spec_dir would be seeded into
    every future worktree too; as a sibling, it must never appear there."""
    source_spec_dir = tmp_path / "main" / ".aifactory" / "specs" / "001-test"
    source_spec_dir.mkdir(parents=True)
    (source_spec_dir / "spec.md").write_text("hello\n")

    assert _claim_gate_refresh(source_spec_dir) is True
    claim_path = qa_mod._gate_refresh_claim_path(source_spec_dir)
    assert claim_path.exists()

    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    target_spec_dir = copy_spec_to_worktree(source_spec_dir, worktree_path, "001-test")

    assert not (target_spec_dir / claim_path.name).exists(), (
        "the refresh claim leaked into a brand-new worktree"
    )
    assert (target_spec_dir / "spec.md").exists(), "sanity: real spec files still copy"


@pytest.mark.asyncio
async def test_claim_does_not_leak_into_main_spec_via_worktree_sync(
    tmp_path: Path,
) -> None:
    """`_sync_worktree_files`'s "any other file" loop copies every file it
    finds in the worktree spec dir that isn't on the hardcoded allowlist,
    worktree -> main. A claim inside spec_dir would ride along; as a sibling
    of the worktree spec dir, it must never appear in the main spec dir."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))
    from server import specpath  # noqa: PLC0415
    from server.services.agent_worktree_sync import WorktreeSyncMixin  # noqa: PLC0415

    project_path = tmp_path / "project"
    spec_id = "001-test-spec"
    worktree_spec = specpath.spec_dir_for(
        project_path / ".aifactory" / "worktrees" / "tasks" / spec_id, spec_id
    )
    worktree_spec.mkdir(parents=True)

    # A claim the gate refresh left behind against the WORKTREE spec dir.
    assert _claim_gate_refresh(worktree_spec) is True
    claim_path = qa_mod._gate_refresh_claim_path(worktree_spec)
    assert claim_path.exists()

    class _SyncHarness(WorktreeSyncMixin):
        pass

    # No implementation_plan.json and no task_id: this build has nothing else
    # to sync, which keeps the harness to exactly the mixin under test (no
    # websocket/task-tracking attributes needed for those unrelated branches).
    await _SyncHarness()._sync_worktree_files(project_path, spec_id, task_id=None)

    main_spec = specpath.spec_dir_for(project_path, spec_id)
    assert not (main_spec / claim_path.name).exists(), (
        "the refresh claim leaked into the main spec dir via worktree sync"
    )
    assert claim_path.exists(), "the original claim must be untouched by sync"
