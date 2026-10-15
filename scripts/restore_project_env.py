"""Roll back the #1795 settings import: rename .env.migrated-* back to .env.

For every registered project (or the paths given), the newest
``<project>/.aifactory/.env.migrated-*`` becomes ``.aifactory/.env`` again. A
project that already has a ``.aifactory/.env`` is skipped, so nothing is
overwritten. Without ``--apply`` it only prints what it would do.

Values saved in the portal after the upgrade live only in the database; copy
them out before rolling back (see the plan's Rollback section).

    python scripts/restore_project_env.py            # dry run, all projects
    python scripts/restore_project_env.py --apply    # rename for real
    python scripts/restore_project_env.py --apply /path/to/project
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def restore(project: Path, *, apply: bool) -> str:
    """Restore one project. Returns a one-line report."""
    env_dir = project / ".aifactory"
    current = env_dir / ".env"
    migrated = sorted(
        env_dir.glob(".env.migrated-*"), key=lambda p: p.stat().st_mtime_ns
    )
    if not migrated:
        return f"skip  {project}: no .env.migrated-* file"
    if current.exists():
        return f"skip  {project}: .aifactory/.env already exists"
    if apply:
        migrated[-1].rename(current)
    return f"{'done' if apply else 'would'}  {project}: {migrated[-1].name} -> .env"


def _registered_projects() -> list[Path]:
    sys.path.insert(0, str(_REPO_ROOT / "apps" / "web-server"))
    from server.project_registry import load_projects  # noqa: PLC0415

    return [Path(p["path"]) for p in load_projects().values() if p.get("path")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="rename for real")
    parser.add_argument("projects", nargs="*", type=Path, help="default: registry")
    args = parser.parse_args(argv)
    projects = args.projects or _registered_projects()
    for project in projects:
        print(restore(project, apply=args.apply))  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
