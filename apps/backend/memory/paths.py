#!/usr/bin/env python3
"""
Memory Directory Management
============================

Functions for managing memory directory structure.
"""

from pathlib import Path


def get_memory_dir(spec_dir: Path) -> Path:
    """
    Get the memory directory for a spec, creating it if needed.

    Args:
        spec_dir: Path to spec directory (e.g., .aifactory/specs/001-feature/)

    Returns:
        Path to memory directory
    """
    memory_dir = spec_dir / "memory"
    memory_dir.mkdir(exist_ok=True)
    return memory_dir


def get_session_insights_dir(spec_dir: Path) -> Path:
    """
    Get the session insights directory, creating it if needed.

    Args:
        spec_dir: Path to spec directory

    Returns:
        Path to session_insights directory
    """
    insights_dir = get_memory_dir(spec_dir) / "session_insights"
    insights_dir.mkdir(parents=True, exist_ok=True)
    return insights_dir


def clear_memory(spec_dir: Path) -> None:
    """
    Clear all memory for a spec.

    WARNING: This deletes all session insights, codebase map, patterns, and gotchas.
    Use with caution - typically only needed when starting completely fresh.

    Args:
        spec_dir: Path to spec directory
    """
    memory_dir = get_memory_dir(spec_dir)

    if memory_dir.exists():
        import shutil

        shutil.rmtree(memory_dir)


# ── project-scoped durable memory (RFC-0021 Phase 0) ─────────────────────────


def project_memory_dir(project_dir: Path) -> Path:
    """The project's durable memory store — where lessons actually compound.

    **Why memory cannot live only under a spec.** On the live fleet a project
    holds many specs (86 in `aifactory-demo`, 19 in the TFactory workspace) and
    each spec is built roughly once; the task list even shows the same work
    rebuilt under a new id (`032-xnode-add-shout`, `033-xnode-add-shout`). So a
    spec-scoped store survives worktree teardown and then has almost nothing to
    be read by: the next build is a different spec with a different directory.
    It compounds across sessions within one task — which already worked — and
    nowhere else.

    Memory is only worth keeping if a lesson from spec 034 can reach spec 041,
    and that requires a store owned by the PROJECT.

    This directory is never handed to the agent. The agent's filesystem is
    confined to its worktree, so this store is seeded INTO a worktree at setup
    and synced back OUT after each session — the same copy-in/copy-out shape the
    spec dir already uses.
    """
    memory_dir = project_dir / ".aifactory" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    return memory_dir


def project_memory_dir_from_aifactory(aifactory_dir: Path) -> Path:
    """The project store, given the project's ``.aifactory/`` directory.

    Exists because the two callers reach the project from different directions,
    and only one of them has a durable answer:

    * workspace setup runs in the CONTROL PLANE and holds the real project root;
    * a build session may run inside a JOB, where ``project_dir`` is the
      ephemeral clone under the pod's emptyDir. Pooling there writes to a
      filesystem that is destroyed with the pod.

    So the session path derives the store from ``source_spec_dir`` — which is
    ``<project>/.aifactory/specs/<id>`` on the co-mounted durable volume — and
    passes its grandparent here. Verified live: a Job build wrote 6 files via
    that path and 0 via ``project_dir``.
    """
    memory_dir = aifactory_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    return memory_dir
