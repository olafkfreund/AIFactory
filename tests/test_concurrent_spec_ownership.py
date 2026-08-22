"""Concurrent tasks in one project must each resolve their OWN spec (#1395).

The defect this covers: spec ownership was decided by "whichever spec directory
has the newest mtime". That is correct for exactly one task at a time, so a
single-task test passes against the broken code and proves nothing. Every test
here therefore has at least TWO tasks in flight, which is the only arrangement
that can tell the fix from the bug.

Observed before the fix: three concurrent tasks all resolved to the same spec,
built it, pushed to its branch, and reported success -- two specs were never
built at all.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "apps" / "web-server"))


class _FakeService:
    """Just the attribute the resolver reads: the per-task ownership record."""

    def __init__(self, spec_dirs: dict[str, Path]) -> None:
        self._spec_dirs = spec_dirs


def _make_specs(root: Path, *names: str) -> dict[str, Path]:
    """Create spec dirs with strictly increasing mtimes, oldest name first."""
    made: dict[str, Path] = {}
    for i, name in enumerate(names):
        d = root / name
        d.mkdir(parents=True)
        # Explicit mtimes: relying on creation order makes the test depend on
        # filesystem timestamp granularity, which is coarse enough on some
        # filesystems that all three land on the same value.
        os.utime(d, (1_000_000 + i, 1_000_000 + i))
        made[name] = d
    return made


def test_each_concurrent_task_resolves_its_own_spec(tmp_path: Path) -> None:
    """Three tasks, three specs, one project. Each must get its own.

    `c-newest` is deliberately the newest on disk, so the pre-fix code returned
    it for all three.
    """
    from server.services.process_monitor import resolve_created_spec_dir

    specs = tmp_path / "specs"
    made = _make_specs(specs, "a-oldest", "b-middle", "c-newest")
    service = _FakeService({f"task-{n}": p for n, p in made.items()})

    for name, expected in made.items():
        got = resolve_created_spec_dir(service, f"task-{name}", specs)
        assert got == expected, (
            f"task-{name} resolved to {got.name if got else None}, not its own {name}"
        )


def test_the_newest_spec_does_not_capture_another_task(tmp_path: Path) -> None:
    """The single case that fails loudest before the fix.

    A task whose own spec is the OLDEST on disk, while another task is finishing
    a newer one. Pre-fix this returned the newer spec -- the task then built
    someone else's work.
    """
    from server.services.process_monitor import resolve_created_spec_dir

    specs = tmp_path / "specs"
    made = _make_specs(specs, "mine", "someone-elses-newer")
    service = _FakeService({"my-task": made["mine"]})

    assert resolve_created_spec_dir(service, "my-task", specs) == made["mine"]


def test_falls_back_to_newest_only_when_the_task_has_no_record(
    tmp_path: Path,
) -> None:
    """The agent-named-it-itself case keeps working.

    Not every spec id is known up front, so the scan stays for a task with no
    record. Asserting it is what keeps the fix from being a silent behaviour
    change for that path.
    """
    from server.services.process_monitor import resolve_created_spec_dir

    specs = tmp_path / "specs"
    made = _make_specs(specs, "older", "newest")
    service = _FakeService({})

    assert resolve_created_spec_dir(service, "unknown-task", specs) == made["newest"]


def test_a_recorded_dir_that_vanished_does_not_return_a_dead_path(
    tmp_path: Path,
) -> None:
    """A stale record must not resolve to a path that is not there.

    Returning it would push the failure downstream into the build, where it
    reads as a missing spec rather than a stale record.
    """
    from server.services.process_monitor import resolve_created_spec_dir

    specs = tmp_path / "specs"
    made = _make_specs(specs, "still-here")
    service = _FakeService({"stale-task": specs / "deleted-since"})

    got = resolve_created_spec_dir(service, "stale-task", specs)
    assert got == made["still-here"]
    assert got is not None and got.is_dir()


def test_no_specs_at_all_resolves_to_none(tmp_path: Path) -> None:
    """An empty project resolves to nothing rather than raising."""
    from server.services.process_monitor import resolve_created_spec_dir

    specs = tmp_path / "specs"
    specs.mkdir()

    assert resolve_created_spec_dir(_FakeService({}), "t", specs) is None


def test_the_monitor_actually_calls_the_resolver(tmp_path: Path) -> None:
    """Wiring, not just the helper.

    A resolver that is correct but unreferenced leaves the bug in place, and a
    helper-only test stays green through exactly that. This asserts the
    spec-creation branch in ``monitor_process`` reaches the resolver rather than
    re-deriving the spec itself.
    """
    import inspect

    from server.services import process_monitor

    src = inspect.getsource(process_monitor.monitor_process)
    assert "resolve_created_spec_dir(" in src, (
        "monitor_process no longer calls the resolver -- ownership is being "
        "decided somewhere else again (#1395)"
    )
    assert "st_mtime" not in src, (
        "monitor_process re-derives the spec by mtime; that is the #1395 defect"
    )


@pytest.mark.parametrize("count", [2, 5])
def test_scales_to_more_concurrent_tasks(tmp_path: Path, count: int) -> None:
    """Same property with more tasks, since the real failure had three."""
    from server.services.process_monitor import resolve_created_spec_dir

    specs = tmp_path / "specs"
    made = _make_specs(specs, *[f"spec-{i}" for i in range(count)])
    service = _FakeService({f"t{i}": made[f"spec-{i}"] for i in range(count)})

    resolved = {
        f"t{i}": resolve_created_spec_dir(service, f"t{i}", specs) for i in range(count)
    }
    assert len(set(resolved.values())) == count, (
        f"{count} tasks collapsed onto {len(set(resolved.values()))} spec(s)"
    )
