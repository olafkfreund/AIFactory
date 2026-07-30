"""Memory compounds at PROJECT level (RFC-0021 Phase 0).

#1030 made memory survive a task. That was necessary and not sufficient: memory
was spec-scoped, and on the live fleet a project holds many specs (86 in
``aifactory-demo``, 19 in the TFactory workspace) with each spec built roughly
once — the task list even shows the same work rebuilt under a new id
(``032-xnode-add-shout``, ``033-xnode-add-shout``).

So a spec-scoped store survived teardown and then had almost nothing to read it:
the next build was a different spec in a different directory. It compounded
across sessions within one task, which already worked, and nowhere else.

The property these tests pin is the one that makes memory worth keeping at all:
**a lesson learned building spec 034 is available when building spec 041.**
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "apps" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from agents.utils import (  # noqa: E402
    seed_memory_from_project,
    sync_memory_to_project,
)
from memory.paths import project_memory_dir  # noqa: E402


def _insight(spec: Path, name: str, body: str) -> None:
    d = spec / "memory" / "session_insights"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)


def _names(d: Path) -> set[str]:
    return {p.name for p in (d / "session_insights").iterdir()} if (d / "session_insights").is_dir() else set()


# ── the property Phase 0 exists for ──────────────────────────────────────────


def test_a_lesson_from_one_spec_reaches_the_next_spec(tmp_path):
    """MUTATION GUARD: without this, memory survives teardown and helps nothing.

    Spec 034 learns something. Spec 041 is a DIFFERENT spec in a different
    directory — the case spec-scoped memory can never serve.
    """
    project = tmp_path / "project"
    # The DURABLE source spec dir — <project>/.aifactory/specs/<id>. The build's
    # own spec_dir lives in a worktree (or, in a Job, an ephemeral clone), which
    # is exactly why the store is anchored on this instead of on project_dir.
    source_034 = project / ".aifactory" / "specs" / "034"
    source_034.mkdir(parents=True)
    spec_034 = project / "wt-a" / ".aifactory" / "specs" / "034"
    _insight(spec_034, "session_001.json", '"the auth middleware rejects empty scopes"')

    # 034 finishes and files what it learned.
    assert sync_memory_to_project(spec_034, source_034)

    # 041 starts: a fresh worktree, seeded from the project.
    spec_041 = project / "wt-b" / ".aifactory" / "specs" / "041"
    spec_041.mkdir(parents=True)
    assert seed_memory_from_project(project, spec_041)

    carried = spec_041 / "memory" / "session_insights" / "session_001.json"
    assert carried.exists(), "spec 041 cannot see what spec 034 learned"
    assert "empty scopes" in carried.read_text()


def test_the_project_store_accumulates_across_many_specs(tmp_path):
    """A project holds dozens of specs; the store is their union, not the last one."""
    project = tmp_path / "project"
    for spec_id, name in (("034", "a.json"), ("041", "b.json"), ("052", "c.json")):
        src = project / ".aifactory" / "specs" / spec_id
        src.mkdir(parents=True, exist_ok=True)
        spec = project / f"wt-{spec_id}" / "specs" / spec_id
        _insight(spec, name, f'"{spec_id}"')
        assert sync_memory_to_project(spec, src)

    assert _names(project_memory_dir(project)) == {"a.json", "b.json", "c.json"}


def test_one_spec_cannot_clear_the_project_store(tmp_path):
    """MUTATION GUARD: replace semantics here would discard every other spec's
    insights — the same data-loss shape as #1033, one scope up."""
    project = tmp_path / "project"
    for sid in ("034", "041"):
        (project / ".aifactory" / "specs" / sid).mkdir(parents=True, exist_ok=True)
    earlier = project / "wt-1" / "specs" / "034"
    _insight(earlier, "kept.json", '"earlier"')
    sync_memory_to_project(earlier, project / ".aifactory" / "specs" / "034")

    later = project / "wt-2" / "specs" / "041"
    _insight(later, "added.json", '"later"')
    sync_memory_to_project(later, project / ".aifactory" / "specs" / "041")

    assert _names(project_memory_dir(project)) == {"kept.json", "added.json"}


def test_a_specs_own_history_is_not_clobbered_by_the_pool(tmp_path):
    """Seeding merges: the spec's own files are written after and win."""
    project = tmp_path / "project"
    (project / ".aifactory" / "specs" / "034").mkdir(parents=True, exist_ok=True)
    donor = project / "wt-1" / "specs" / "034"
    _insight(donor, "shared.json", '"from the pool"')
    sync_memory_to_project(donor, project / ".aifactory" / "specs" / "034")

    target = project / "wt-2" / "specs" / "041"
    _insight(target, "own.json", '"mine"')
    assert seed_memory_from_project(project, target)

    assert _names(target / "memory") == {"own.json", "shared.json"}


# ── no-ops and safety ────────────────────────────────────────────────────────


def test_seeding_an_empty_project_store_is_a_no_op(tmp_path):
    project = tmp_path / "project"
    project_memory_dir(project)  # exists but empty
    spec = tmp_path / "spec"
    spec.mkdir()
    assert seed_memory_from_project(project, spec) is False


def test_no_project_dir_is_a_no_op(tmp_path):
    spec = tmp_path / "spec"
    _insight(spec, "a.json", "{}")
    assert sync_memory_to_project(spec, None) is False
    assert seed_memory_from_project(None, spec) is False


def test_a_spec_with_no_memory_is_a_no_op(tmp_path):
    spec = tmp_path / "spec"
    spec.mkdir()
    assert sync_memory_to_project(spec, tmp_path / "project" / ".aifactory" / "specs" / "x") is False


def test_a_failing_copy_never_raises(tmp_path, monkeypatch):
    """A build that produced working code must not fail over its memory."""
    import agents.utils as utils

    spec = tmp_path / "spec"
    _insight(spec, "a.json", "{}")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(utils.shutil, "copytree", boom)
    assert utils.sync_memory_to_project(spec, tmp_path / "p" / ".aifactory" / "specs" / "x") is False


# ── wiring: a helper nobody calls is decoration ──────────────────────────────


@pytest.mark.parametrize(
    "path,needle",
    [
        ("apps/backend/agents/session.py", "sync_memory_to_project(spec_dir, source_spec_dir)"),
        ("apps/backend/core/workspace/setup.py", "seed_memory_from_project(project_dir"),
    ],
)
def test_both_halves_are_actually_wired(path, needle):
    """Learned on #1033: testing a helper directly leaves the call site free to
    be deleted while the suite stays green. Both halves are pinned at the call.
    """
    src = (_BACKEND.parent.parent / path).read_text()
    assert needle in src, f"{path} no longer calls it — memory stops compounding"


def test_every_session_outcome_pools_its_memory():
    """Dead ends included: RFC-0021 calls them the category most worth keeping."""
    src = (_BACKEND / "agents" / "session.py").read_text()
    assert src.count("sync_memory_to_project(spec_dir, source_spec_dir)") >= 3


# ── the anchor: a live Job build is what caught this ─────────────────────────


def test_the_pool_lands_beside_specs_not_under_the_build_tree(tmp_path):
    """MUTATION GUARD: anchoring on project_dir loses the pool inside a Job.

    In a build Job, `project_dir` is the ephemeral clone under the pod's
    emptyDir, so pooling there writes to a filesystem destroyed with the pod —
    the #1030 bug one level out. A live Job build wrote 6 files via
    source_spec_dir and 0 via project_dir, which is how this was found; reading
    the code could not have shown it.

    The store must therefore sit beside `specs/`, derived from the durable
    source spec dir, and nowhere under the build tree.
    """
    project = tmp_path / "project"
    source = project / ".aifactory" / "specs" / "034"
    source.mkdir(parents=True)

    # The build's own spec dir, deliberately somewhere transient.
    ephemeral = tmp_path / "work" / "clone" / ".aifactory" / "specs" / "034"
    _insight(ephemeral, "s.json", '"learned"')

    assert sync_memory_to_project(ephemeral, source)

    pooled = project / ".aifactory" / "memory" / "session_insights" / "s.json"
    assert pooled.exists(), "the pool did not land on the durable path"
    assert not (tmp_path / "work" / "clone" / ".aifactory" / "memory").exists(), (
        "the pool was written under the ephemeral build tree and would be lost"
    )


# ── the control-plane pool: where it must actually happen ────────────────────


def test_the_control_plane_mirror_pools_at_project_level():
    """MUTATION GUARD: the in-build sync cannot pool for a Job-dispatched build.

    Inside a Job every path — including source_spec_dir — is under /work, the
    pod's emptyDir (the build log says so: "Filesystem restricted to:
    /work/.aifactory/worktrees/..."). So anything the build process writes dies
    with the pod. Two live builds proved it: the project pool stayed at 0 files
    while this mirror carried 6 and 4 files to the PVC.

    Asserted on the source because exercising it needs a running build. The
    property is narrow and the regression is a one-line deletion.
    """
    src = (
        _BACKEND.parent / "web-server" / "server" / "services" / "agent_worktree_sync.py"
    ).read_text()
    body = src.split("async def _sync_worktree_files", 1)[1]

    assert 'project_path / ".aifactory" / "memory"' in body, (
        "the control plane no longer pools memory at project level — a "
        "Job-dispatched build's memory stops compounding"
    )
    pool = body.split('project_path / ".aifactory" / "memory"', 1)[1]
    assert "dirs_exist_ok=True" in pool, "the pool must merge, never replace"
    assert "shutil.rmtree" not in pool.split("except", 1)[0]
